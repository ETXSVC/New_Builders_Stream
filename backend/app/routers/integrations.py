"""Task 4.7 (design spec Section 3): GET /integrations/{provider}/connect.
Top-level routes, not company-nested — provider is a path segment
(quickbooks/freshbooks), matching the API spec's own sketch. RBAC per
docs/07-security-compliance.md Section 2's "Integrations" row:
Admin/Accountant only, ZERO Client/PM/Field-Crew access.
"""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.deps import CurrentUser, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.tier_gating import require_module, tier_allows
from app.db import session_scope, set_current_tenant
from app.models import IntegrationConnection, IntegrationSyncRecord
from app.schemas.integration import (
    AuthorizationUrlResponse,
    SyncRecordResponse,
    SyncStatusResponse,
)
from app.services.accounting_client import get_accounting_client
from app.services.audit import write_audit_log
from app.services.integration_oauth_state import (
    InvalidOAuthStateError,
    sign_oauth_state,
    verify_oauth_state,
)
from app.services.token_encryption import encrypt_token

router = APIRouter(prefix="/integrations", tags=["integrations"])

_ROLES = ("admin", "accountant")

Provider = Literal["quickbooks", "freshbooks"]


@router.get("/{provider}/connect", response_model=AuthorizationUrlResponse)
async def connect(
    provider: Provider,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _tier: CurrentUser = Depends(require_module("integrations")),
) -> AuthorizationUrlResponse:
    state = sign_oauth_state(company_id=str(current.company_id), provider=provider)
    client = get_accounting_client(provider)
    authorization_url = await client.get_authorization_url(
        company_id=str(current.company_id), state=state
    )
    return AuthorizationUrlResponse(authorization_url=authorization_url)


async def _upsert_connection(
    session: AsyncSession, *, company_id: uuid.UUID, provider: str, access_token: str, refresh_token: str
) -> IntegrationConnection:
    """Reconnecting the same provider (callback firing again) replaces
    stale tokens rather than erroring — design spec Section 1's own
    ON CONFLICT (company_id, provider) DO UPDATE requirement. Uses
    Postgres's native upsert (not a SELECT-then-INSERT-or-UPDATE) so this
    is a single atomic statement, not a race between two separate
    connect attempts for the same company+provider landing concurrently.

    Takes a bare `session: AsyncSession`, not `current: CurrentUser` (as
    Task 4.8 originally had it) — this helper is now also called from
    `callback` below, which has no `CurrentUser` at all (see that route's
    own docstring). `connect`'s RBAC-checked `CurrentUser` was never
    actually needed by this function's own logic, only its `.session`.

    `encrypt_token(access_token)`/`encrypt_token(refresh_token)` are each
    computed exactly once and reused in both the `.values()` insert branch
    and the `.set_={}` update branch (Task 4.8 code-quality review, Minor
    issue (a)): calling `encrypt_token` twice per token wastefully doubles
    Fernet's work, and since Fernet's output is non-deterministic (a fresh
    random IV/nonce per call), two calls would silently produce two
    different-but-both-valid ciphertexts for the same plaintext — reusing
    one computed value avoids that surprise entirely, not just the waste.
    """
    access_token_encrypted = encrypt_token(access_token)
    refresh_token_encrypted = encrypt_token(refresh_token)
    stmt = (
        pg_insert(IntegrationConnection)
        .values(
            id=uuid.uuid4(),
            company_id=company_id,
            provider=provider,
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
        )
        .on_conflict_do_update(
            index_elements=["company_id", "provider"],
            set_={
                "access_token_encrypted": access_token_encrypted,
                "refresh_token_encrypted": refresh_token_encrypted,
            },
        )
        .returning(IntegrationConnection)
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one()


@router.get("/{provider}/callback", response_class=RedirectResponse, status_code=status.HTTP_303_SEE_OTHER)
async def callback(
    provider: Provider,
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    """Task 4.9 (design spec Section 3): the OAuth redirect target.

    On success this 303-redirects the BROWSER back into the frontend at
    `{frontend_base_url}/integrations?connected={provider}` rather than
    returning the connection as JSON — the caller here is a person
    mid-OAuth-dance, not an API client, and landing them on a raw JSON
    page was a known UX gap flagged (and deferred) when the integrations
    frontend was built. Error paths deliberately KEEP their HTTP error
    responses (400 invalid state, 403 tier): they're exceptional,
    security-relevant outcomes where a plain error beats silently
    bouncing the user onward, and programmatic tests assert on them.

    No `CurrentUser` here — this is an external redirect from the
    (fake, today) accounting provider back into our app, carrying no
    bearer token, so `app/core/deps.py`'s `get_current_user` dependency
    chain cannot run on this route at all (the same structural reality
    `/invitations/{id}/accept` already established for this codebase).
    Instead this route opens its own session and sets RLS tenant context
    manually, exactly like every Dramatiq worker actor does for the same
    "no request-scoped session to inherit" reason — see in particular
    `app/tasks/estimate_pdf.py`'s `_generate_and_persist`, which is the
    closest existing precedent: a standalone `SessionLocal()` session, one
    explicit `set_current_tenant` call, then real work, then a single
    `commit()`.

    Reuses `app.db.session_scope`/`set_current_tenant` directly rather than
    duplicating a second engine/sessionmaker in this file or inlining the
    raw `SELECT set_config(...)` statement: `set_current_tenant` is a plain
    function of `(session, company_id)` with no dependency on
    `TenantMiddleware` or any other request-scoped state (confirmed by
    reading `app/core/middleware.py` — `TenantMiddleware` only stashes the
    raw bearer token/tenant header into contextvars, it never touches a
    session or calls `set_current_tenant` itself). `accept_invitation`
    (`app/routers/invitations.py`) is the closest existing precedent for
    this exact shape — a route with no `CurrentUser` at all, using
    `async with session_scope() as session: async with session.begin():` —
    reused verbatim here rather than the plan's own literal reference code
    (a bare, un-guarded `session.begin()`/`session.commit()`), because
    `session.begin()` used as a context manager gives an automatic
    rollback-on-exception guarantee `accept_invitation` already relies on;
    a bare awaited `begin()` does not make that guarantee explicit, and
    this is the single highest-stakes unauthenticated write route in this
    feature to leave it implicit on.

    Uses `session_scope()` (`app.db`, the `app_user`-role DSN
    `get_current_user`'s own dependency chain uses via `settings.
    database_url`), not the owner-role DSN cross-tenant worker actors like
    `check_compliance_expiry` use — this route must go through RLS like any
    other tenant-scoped write, it does not need cross-tenant visibility the
    way a scheduled job scanning every company does.

    Every write in this session must go through RLS scoped to the company
    the signed `state` decoded — never trust `company_id` from anywhere
    else in this request, since there is no `CurrentUser` to cross-check it
    against.
    """
    try:
        state_company_id, state_provider = verify_oauth_state(state)
    except InvalidOAuthStateError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired state") from exc

    if state_provider != provider:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "state does not match provider")

    company_id = uuid.UUID(state_company_id)

    async with session_scope() as session:
        async with session.begin():
            await set_current_tenant(session, str(company_id))

            # Tier gating (spec Section 3): callback has no CurrentUser for
            # require_module to hang off — the signed state IS its auth — so
            # the check runs in-route. This also closes the one bypass the
            # dependency couldn't: a state minted while Enterprise, redeemed
            # within its 10-minute TTL after a downgrade.
            if not await tier_allows(session, company_id, "integrations"):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "This feature requires the enterprise plan",
                )

            client = get_accounting_client(provider)
            access_token, refresh_token = await client.exchange_code_for_tokens(code=code)

            connection = await _upsert_connection(
                session,
                company_id=company_id,
                provider=provider,
                access_token=access_token,
                refresh_token=refresh_token,
            )

            await write_audit_log(
                session,
                company_id=company_id,
                actor_id=None,
                action="integration.connected",
                entity_type="integration_connection",
                entity_id=connection.id,
                metadata={"provider": provider},
            )

    return RedirectResponse(
        f"{settings.frontend_base_url}/integrations?connected={provider}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{provider}/sync-status", response_model=SyncStatusResponse)
async def sync_status(
    provider: Provider,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> SyncStatusResponse:
    """Task 4.10 (design spec Section 3): read-only, cursor-paginated sync
    status listing. Same `_ROLES` (admin/accountant) RBAC gate as
    `connect` above.

    The `IntegrationConnection` lookup below DOES filter explicitly on
    `company_id == current.company_id`, unlike `leads.py`'s `GET /leads`
    (which relies on RLS alone) — `leads` is a LIST endpoint, where the
    parent/child roll-up `get_all_descendant_ids()` RLS policy returning
    multiple companies' rows is the intended behavior. This route instead
    does a `scalar_one_or_none()` lookup keyed only on `provider`, which
    has no such tolerance: `integration_connections` has a
    `UNIQUE(company_id, provider)` constraint, not a globally unique one,
    so a parent company and a child branch that each independently connect
    the same provider are two distinct, RLS-visible rows to a parent
    admin — `provider` alone can't disambiguate them, and `scalar_one_or_
    none()` would raise `MultipleResultsFound` (an unhandled 500) instead
    of correctly resolving to "this company's own connection." Matches
    `subscriptions.py`'s own precedent for this exact "hierarchy + need
    exactly one row" shape (an explicit company filter alongside RLS, not
    RLS alone) — though unlike `subscriptions` (root-company-scoped by
    design), `integration_connections` rows are scoped to whichever
    specific company connected (see `connect`/`callback` above, both of
    which use `current.company_id`/the signed state's own `company_id`
    directly, never a root-company resolution), so the filter here is the
    caller's own `company_id`, not `get_root_company_id(...)`.

    `IntegrationSyncRecord` rows, by contrast, need no explicit
    `company_id` filter of their own: they're looked up by `connection_id
    == connection.id`, and `connection.id` is a global UUID primary key
    that (once resolved to a specific row above) unambiguously identifies
    one connection — filtering by that FK is sufficient on its own,
    independent of RLS breadth.
    """
    connection_result = await current.session.execute(
        select(IntegrationConnection).where(
            IntegrationConnection.provider == provider,
            IntegrationConnection.company_id == current.company_id,
        )
    )
    connection = connection_result.scalar_one_or_none()
    if connection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {provider} connection for this company")

    query = select(IntegrationSyncRecord).where(IntegrationSyncRecord.connection_id == connection.id)
    if status_filter is not None:
        query = query.where(IntegrationSyncRecord.status == status_filter)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=IntegrationSyncRecord.created_at,
        id_col=IntegrationSyncRecord.id,
        cursor=cursor,
        limit=limit,
    )

    return SyncStatusResponse(
        provider=connection.provider,
        connected_at=connection.connected_at,
        records=[SyncRecordResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )
