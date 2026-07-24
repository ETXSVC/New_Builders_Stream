"""Task 4.12 (design spec Section 5): sync_financial_record — pushes one
Invoice/Expense/Bill to one connected accounting provider (fake, per this
plan's scope), tracking per-record status in integration_sync_records.

Same undecorated-function/decorated-actor split as every other Dramatiq
actor in this codebase (see app/tasks/flag_overdue_financial_records.py's
own docstring for the full rationale). Uses the owner-role engine
(settings.migrations_database_url) — this actor is scoped to ONE
connection/company via connection_id, not a cross-tenant scan, but it
still uses the owner engine and filters explicitly by connection_id/
company_id in every query (never relying on RLS, since there is no
set_current_tenant() call in a worker process) — same established
convention every other worker actor in this codebase already follows,
rather than introducing a new "workers set tenant context" pattern for
just this one actor.

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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Bill, Expense, IntegrationConnection, IntegrationSyncRecord, Invoice
from app.services import accounting_client
from app.services.token_encryption import decrypt_token
from app.tasks import broker  # noqa: F401 - import-time side effect

_owner_engine = create_async_engine(settings.migrations_database_url, pool_pre_ping=True)
_OwnerSessionLocal = async_sessionmaker(_owner_engine, expire_on_commit=False, class_=AsyncSession)

_ENTITY_MODELS: dict[str, type[Invoice] | type[Expense] | type[Bill]] = {
    "invoice": Invoice,
    "expense": Expense,
    "bill": Bill,
}


def _serialize(entity_type: str, record: Invoice | Expense | Bill) -> dict:
    # isinstance, not entity_type string matching — the two are 1:1 via
    # _ENTITY_MODELS, and isinstance is the form mypy can narrow the union
    # through. entity_type stays a parameter for the mismatch guard below.
    if isinstance(record, Invoice):
        return {
            "invoice_number": record.invoice_number,
            "amount": str(record.amount),
            "status": record.status,
        }
    if isinstance(record, Expense):
        return {"description": record.description, "amount": str(record.amount)}
    if isinstance(record, Bill):
        return {"vendor_name": record.vendor_name, "amount": str(record.amount), "status": record.status}
    raise ValueError(f"Unknown entity_type: {entity_type!r}")


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


async def _sync_financial_record(
    *,
    connection_id: str,
    entity_type: str,
    entity_id: str,
    session_factory: async_sessionmaker[AsyncSession] = _OwnerSessionLocal,
) -> None:
    async with session_factory() as session:
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

            payload = _serialize(entity_type, record)
            # entity_id as the idempotency key: stable across every retry
            # of THIS logical sync (Dramatiq re-invokes with the same
            # entity_id), which is exactly what lets the provider (real or
            # fake, see accounting_client.py's own docstring) recognize a
            # retried push as "already processed" instead of double-posting
            # it — the actual fix for the race this module's own docstring
            # describes (a failure between a successful push and this
            # function's own success-bookkeeping commit).
            if entity_type == "invoice":
                external_record_id = await client.push_invoice(
                    access_token=access_token, invoice=payload, idempotency_key=entity_id
                )
            elif entity_type == "expense":
                external_record_id = await client.push_expense(
                    access_token=access_token, expense=payload, idempotency_key=entity_id
                )
            else:
                external_record_id = await client.push_bill(
                    access_token=access_token, bill=payload, idempotency_key=entity_id
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
