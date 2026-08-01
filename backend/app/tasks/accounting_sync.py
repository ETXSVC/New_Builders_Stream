"""Task 4.12 (design spec Section 5): sync_financial_record — pushes one
Invoice/Expense/Bill to one connected accounting provider (fake, per this
plan's scope), tracking per-record status in integration_sync_records.

Same undecorated-function/decorated-actor split as every other Dramatiq
actor in this codebase (see app/tasks/flag_overdue_financial_records.py's
own docstring for the full rationale).

Runs on the ORDINARY RLS-constrained `app_user` session, not an owner-role
engine. This actor is scoped to ONE connection/company by construction, so
it has no reason to bypass RLS — and the earlier reasoning for doing so
("there is no set_current_tenant() call in a worker process") described a
limitation rather than a requirement. Migration 0020's
get_integration_connection_company_id removes it: the actor resolves its
tenant from the connection id through a narrow SECURITY DEFINER lookup,
calls set_current_tenant, and runs everything after that under RLS. The
explicit connection_id/company_id filters below stay, but they are now
belt-and-braces over an enforced boundary rather than the only thing
standing between this job and another tenant's financial records.

Retry: on any exception from entity lookup, token decryption, or the
push itself, this function marks the integration_sync_records row
status='failed' + last_error, THEN re-raises — Dramatiq's own
max_retries=3/backoff handles the actual retry timing, this code never
implements its own retry loop (design spec Section 5). After max_retries
is exhausted, Dramatiq drops the message but the row stays at
status='failed', remaining visible via GET /integrations/{provider}/
sync-status rather than silently vanishing.

This guarantee covers everything EXCEPT the connection lookup itself:
company_id and connection_id (the two columns integration_sync_records
is keyed on) come from the connection row, so a connection that no
longer exists has nothing to attribute a failed row to — that lookup
alone stays outside the try/except and propagates raw. The entity
lookup (Invoice/Expense/Bill), by contrast, DOES happen inside the
try/except: the enqueuing event handler (Task 4.11) reuses the
triggering request's own, not-yet-committed session, and Dramatiq's
broker is not part of that DB transaction — a request that rolls back
after this actor's message is already enqueued means the entity row
may genuinely not exist yet when this actor runs. That race is real,
not hypothetical, and specifically what this task's own regression
test below exercises.

Double-post safety: a genuinely distinct race from the one above — a
push_* call can SUCCEED and then THIS function's own success-path
_upsert_sync_record/commit can fail for an unrelated reason (a DB
blip, the worker process dying mid-commit). That failure is NOT caught
by the try/except above (it happens after the try block exits cleanly),
so it propagates raw and Dramatiq retries the whole actor — which would
re-run the entity lookup and call push_* again for a push that already
succeeded, double-posting to the provider. `idempotency_key=entity_id`
on every push_* call (added specifically to close this gap; see
app/services/accounting_client.py's own docstring) is what makes that
retry safe: entity_id is stable across every retry of this same logical
sync, so the provider (real or fake) recognizes the repeat and returns
the SAME external_record_id without processing it again, rather than
this module trying to infer "was it already synced?" from its own
possibly-never-written bookkeeping.

Note on the accounting client import: this module imports the
app.services.accounting_client MODULE (not `get_accounting_client`
directly) and calls `accounting_client.get_accounting_client(...)` at
call time. tests/test_accounting_sync.py monkeypatches
`accounting_client.get_accounting_client` (the module attribute) so it
can inspect the exact FakeAccountingProviderClient instance it
constructed via `.pushed_invoices`/`.should_fail`. A `from
app.services.accounting_client import get_accounting_client` here would
bind a local name at import time that the test's
`monkeypatch.setattr(accounting_client, "get_accounting_client", ...)`
would never touch — this module would keep calling the original
function, and the test's assertions against its own fake_client instance
would fail. Importing the module and doing attribute access avoids that
classic monkeypatching pitfall.
"""
from __future__ import annotations

import uuid

from typing import cast

import dramatiq
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionLocal, set_current_tenant
from app.models import (
    Bill,
    Expense,
    IntegrationConnection,
    IntegrationEntityMapping,
    IntegrationSyncRecord,
    Invoice,
    ProjectClient,
    User,
)
from app.services import accounting_client
from app.services.accounting_client import AccountingProviderError
from app.services.token_encryption import decrypt_token, encrypt_token
from app.tasks import broker  # noqa: F401 - import-time side effect

# The ordinary RLS-constrained `app_user` session, NOT an owner-role engine.
#
# This actor is single-tenant by construction: it is handed one
# connection_id and everything it touches belongs to that one company. It
# therefore has no reason to bypass RLS, and running under it turns tenant
# isolation here from "every WHERE company_id clause below is written
# correctly" into something the database enforces. Migration 0020's
# get_integration_connection_company_id resolves the tenant first — reading
# the connection row to LEARN its company is the chicken-and-egg problem RLS
# creates for a job with no caller, and that narrow SECURITY DEFINER lookup
# is the same escape migration 0011 established for the Stripe webhook.
_AppUserSessionLocal = SessionLocal

_ENTITY_MODELS: dict[str, type[Invoice] | type[Expense] | type[Bill]] = {
    "invoice": Invoice,
    "expense": Expense,
    "bill": Bill,
}


async def _customer_name(session: AsyncSession, invoice: Invoice) -> str | None:
    """Who a real accounting provider will bill this invoice to.

    `invoices` has no client column — only `project_id` — so the payee is
    whoever holds a `project_clients` membership on that project. Ordered by
    `created_at` so a project with two clients resolves to the same one on
    every sync rather than whichever row the planner happened to return
    first; a customer that changes between syncs would post the second
    invoice to a different account in the tenant's books.

    None means the project has no client assigned, which a real push cannot
    recover from — see `_resolve_refs`.
    """
    result = await session.execute(
        select(User.full_name)
        .join(ProjectClient, ProjectClient.user_id == User.id)
        .where(ProjectClient.project_id == invoice.project_id)
        .order_by(ProjectClient.created_at)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _serialize(
    session: AsyncSession, entity_type: str, record: Invoice | Expense | Bill
) -> dict:
    # isinstance, not entity_type string matching — the two are 1:1 via
    # _ENTITY_MODELS, and isinstance is the form mypy can narrow the union
    # through. entity_type stays a parameter for the mismatch guard below.
    if isinstance(record, Invoice):
        return {
            "invoice_number": record.invoice_number,
            "amount": str(record.amount),
            "status": record.status,
            "due_date": record.due_date.isoformat() if record.due_date else None,
            # Read by RefSpec(from_payload="customer_name") to resolve the
            # provider's own customer id; the fake ignores it.
            "customer_name": await _customer_name(session, record),
        }
    if isinstance(record, Expense):
        return {"description": record.description, "amount": str(record.amount)}
    if isinstance(record, Bill):
        return {
            "vendor_name": record.vendor_name,
            "amount": str(record.amount),
            "status": record.status,
            "bill_number": record.bill_number,
            "due_date": record.due_date.isoformat() if record.due_date else None,
        }
    raise ValueError(f"Unknown entity_type: {entity_type!r}")


async def _resolve_refs(
    session: AsyncSession,
    client,
    *,
    connection: IntegrationConnection,
    entity_type: str,
    payload: dict,
    access_token: str,
    account_id: str,
) -> None:
    """Fill in the provider ids `payload` cannot be pushed without.

    Each client declares what it needs (`required_refs`) rather than this
    function assuming one provider's shape — a QuickBooks invoice line needs
    an ItemRef, a FreshBooks one does not. The fake declares nothing, so
    this is a no-op for it and every existing test's payload is unchanged.

    Answers are cached in `integration_entity_mappings`. That is not an
    optimization: `resolve_entity` is find-or-CREATE, so forgetting means
    eventually creating a second "Acme Holdings" in somebody's real books.
    The cache is keyed on the connection, so reconnecting to a different
    company file does not reuse the previous file's ids.
    """
    for spec in client.required_refs(entity_type):
        local_key = spec.local_key(payload)
        if not local_key:
            raise AccountingProviderError(
                f"Cannot sync this {entity_type}: no {spec.kind} name to match on "
                f"(expected {spec.from_payload!r}). For an invoice this means the "
                "project has no client assigned."
            )

        cached = (
            await session.execute(
                select(IntegrationEntityMapping.provider_entity_id).where(
                    IntegrationEntityMapping.connection_id == connection.id,
                    IntegrationEntityMapping.entity_kind == spec.kind,
                    IntegrationEntityMapping.local_key == local_key,
                )
            )
        ).scalar_one_or_none()

        if cached is None:
            cached = await client.resolve_entity(
                access_token=access_token,
                account_id=account_id,
                kind=spec.kind,
                name=local_key,
            )
            session.add(
                IntegrationEntityMapping(
                    company_id=connection.company_id,
                    connection_id=connection.id,
                    entity_kind=spec.kind,
                    local_key=local_key,
                    provider_entity_id=cached,
                )
            )
            # Flushed, not committed: a commit here would end the
            # transaction and take `app.current_tenant` with it (it is set
            # with is_local=true), leaving every later query in this
            # function scoped to no tenant at all.
            await session.flush()

        payload[spec.payload_key] = cached


async def _upsert_sync_record(
    session: AsyncSession, *, company_id: uuid.UUID, connection_id: uuid.UUID,
    entity_type: str, entity_id: uuid.UUID, status: str, last_error: str | None,
    external_record_id: str | None = None,
) -> None:
    """One upsert, called exactly once per _sync_financial_record invocation
    (from either the success or the failure branch, never both) —
    attempt_count increments on the SAME write that also records the
    outcome, not as a separate pre-write. This codebase's other worker
    actors (flag_overdue_financial_records.py, report_seat_usage.py) commit
    once, at the end of their function, not mid-function; this actor
    follows that same precedent rather than introducing a new "commit an
    intermediate pending state" pattern — there is no observable moment
    where a client could read a 'pending' row for THIS entity via
    GET /integrations/{provider}/sync-status, since the row is only ever
    created/updated together with its final outcome in one statement."""
    stmt = (
        pg_insert(IntegrationSyncRecord)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            connection_id=connection_id,
            entity_type=entity_type,
            entity_id=entity_id,
            status=status,
            attempt_count=1,
            last_error=last_error,
            external_record_id=external_record_id,
        )
        .on_conflict_do_update(
            index_elements=["connection_id", "entity_type", "entity_id"],
            set_={
                "status": status,
                "attempt_count": IntegrationSyncRecord.__table__.c.attempt_count + 1,
                "last_error": last_error,
                "last_attempted_at": func.now(),
                # COALESCE, not an unconditional overwrite: a retried
                # success (idempotency-key dedup returned the SAME
                # external_id, see accounting_client.py) passes the same
                # value again, but a FAILED attempt passes None here and
                # must never blank out a real external_record_id a prior
                # successful attempt already recorded.
                "external_record_id": func.coalesce(
                    external_record_id, IntegrationSyncRecord.__table__.c.external_record_id
                ),
            },
        )
    )
    await session.execute(stmt)


async def _push(
    client,
    *,
    entity_type: str,
    access_token: str,
    account_id: str,
    payload: dict,
    entity_id: str,
) -> str:
    """One push, by record type.

    entity_id as the idempotency key: stable across every retry of THIS
    logical sync (Dramatiq re-invokes with the same entity_id), which is
    what lets the provider recognize a repeat as "already processed"
    instead of double-posting it — QuickBooks replays the original response
    for a repeated `requestid`. See app/services/accounting_client.py.
    """
    if entity_type == "invoice":
        return await client.push_invoice(
            access_token=access_token,
            account_id=account_id,
            invoice=payload,
            idempotency_key=entity_id,
        )
    if entity_type == "expense":
        return await client.push_expense(
            access_token=access_token,
            account_id=account_id,
            expense=payload,
            idempotency_key=entity_id,
        )
    return await client.push_bill(
        access_token=access_token,
        account_id=account_id,
        bill=payload,
        idempotency_key=entity_id,
    )


async def _refresh_and_persist(
    session: AsyncSession, client, connection: IntegrationConnection
) -> str:
    """Exchange the refresh token for a new pair and SAVE IT, immediately.

    Both providers rotate: the refresh token being spent here stops working
    the moment the new pair is issued. So losing the response — a crash
    during the retried push, a rollback — does not cost a retry, it kills
    the connection permanently and the tenant has to reconnect by hand.
    That is why this commits before returning rather than riding along on
    whatever commit the caller reaches later.

    Committing mid-actor has one consequence that must be undone straight
    away: `set_current_tenant` sets `app.current_tenant` with
    `is_local=true`, which is transaction-scoped, so the commit takes the
    tenant context with it and every subsequent query in this actor would
    be scoped to no tenant at all. Re-setting it is not optional.
    """
    tokens = await client.refresh_access_token(
        refresh_token=decrypt_token(connection.refresh_token_encrypted)
    )
    connection.access_token_encrypted = encrypt_token(tokens.access_token)
    connection.refresh_token_encrypted = encrypt_token(tokens.refresh_token)
    await session.commit()
    await set_current_tenant(session, str(connection.company_id))
    return tokens.access_token


async def _sync_financial_record(
    *,
    connection_id: str,
    entity_type: str,
    entity_id: str,
    session_factory: async_sessionmaker[AsyncSession] = _AppUserSessionLocal,
) -> None:
    async with session_factory() as session:
        # Resolve the tenant BEFORE any RLS-scoped read: without
        # app.current_tenant set, get_all_descendant_ids(NULL) matches
        # nothing and the connection row below is invisible to this
        # session's own role.
        company_id = (
            await session.execute(
                select(func.get_integration_connection_company_id(uuid.UUID(connection_id)))
            )
        ).scalar_one_or_none()
        if company_id is None:
            # The connection was deleted between enqueue and execution.
            # Nothing to sync and nothing to record against — a sync record
            # needs a company_id, and there is no longer one to use.
            return
        await set_current_tenant(session, str(company_id))

        connection = (
            await session.execute(
                select(IntegrationConnection).where(IntegrationConnection.id == uuid.UUID(connection_id))
            )
        ).scalar_one()

        try:
            model = _ENTITY_MODELS[entity_type]
            # cast: select() over the union-typed model class comes back
            # Base-typed; _ENTITY_MODELS guarantees it's one of the three.
            record = cast(
                "Invoice | Expense | Bill",
                (
                    await session.execute(select(model).where(model.id == uuid.UUID(entity_id)))
                ).scalar_one(),
            )

            access_token = decrypt_token(connection.access_token_encrypted)
            client = accounting_client.get_accounting_client(connection.provider)

            account_id = connection.provider_account_id or ""
            if not account_id and not isinstance(
                client, accounting_client.FakeAccountingProviderClient
            ):
                # Migration 0030 added the column; connections made before it
                # have no realm/account id, and neither provider's token
                # addresses anything without one. Say so, rather than letting
                # it surface as an opaque 404 from a URL with an empty path
                # segment.
                raise accounting_client.AccountingProviderError(
                    f"This {connection.provider} connection predates the provider "
                    "account id being stored; reconnect the provider to sync again"
                )

            payload = await _serialize(session, entity_type, record)
            await _resolve_refs(
                session,
                client,
                connection=connection,
                entity_type=entity_type,
                payload=payload,
                access_token=access_token,
                account_id=account_id,
            )
            # entity_id as the idempotency key: stable across every retry
            # of THIS logical sync (Dramatiq re-invokes with the same
            # entity_id), which is exactly what lets the provider (real or
            # fake, see accounting_client.py's own docstring) recognize a
            # retried push as "already processed" instead of double-posting
            # it — the actual fix for the race this module's own docstring
            # describes (a failure between a successful push and this
            # function's own success-bookkeeping commit).
            try:
                external_record_id = await _push(
                    client,
                    entity_type=entity_type,
                    access_token=access_token,
                    account_id=account_id,
                    payload=payload,
                    entity_id=entity_id,
                )
            except accounting_client.AccountingAuthError:
                # The access token expired — an hour after connecting for
                # QuickBooks, twelve for FreshBooks — so this is the normal
                # steady state, not an exceptional one. Refresh, persist,
                # and try once. Only once: a second auth failure means the
                # refresh token itself is dead (rotated away, or past its
                # 100-day life), which retrying cannot fix.
                access_token = await _refresh_and_persist(session, client, connection)
                external_record_id = await _push(
                    client,
                    entity_type=entity_type,
                    access_token=access_token,
                    account_id=account_id,
                    payload=payload,
                    entity_id=entity_id,
                )
        except Exception as exc:
            await _upsert_sync_record(
                session,
                company_id=connection.company_id,
                connection_id=connection.id,
                entity_type=entity_type,
                entity_id=uuid.UUID(entity_id),
                status="failed",
                last_error=str(exc),
            )
            # Commit BEFORE re-raising — the failed-status write must
            # survive this exception propagating out of the `async with
            # session_factory()` block, it cannot rely on an implicit
            # commit-on-clean-exit that a raised exception would skip.
            await session.commit()
            raise

        await _upsert_sync_record(
            session,
            company_id=connection.company_id,
            connection_id=connection.id,
            entity_type=entity_type,
            entity_id=uuid.UUID(entity_id),
            status="success",
            last_error=None,
            external_record_id=external_record_id,
        )
        await session.commit()


sync_financial_record = dramatiq.actor(max_retries=3, actor_name="sync_financial_record")(
    _sync_financial_record
)
