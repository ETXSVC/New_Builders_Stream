"""Task 4.7 (design spec Section 3): GET /integrations/{provider}/connect.
Top-level routes, not company-nested — provider is a path segment
(quickbooks/freshbooks), matching the API spec's own sketch. RBAC per
docs/07-security-compliance.md Section 2's "Integrations" row:
Admin/Accountant only, ZERO Client/PM/Field-Crew access.
"""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, require_role
from app.db import session_scope, set_current_tenant
from app.models import IntegrationConnection
from app.schemas.integration import AuthorizationUrlResponse, IntegrationConnectionResponse
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


@router.get("/{provider}/callback", response_model=IntegrationConnectionResponse)
async def callback(
    provider: Provider,
    code: str = Query(...),
    state: str = Query(...),
) -> IntegrationConnectionResponse:
    """Task 4.9 (design spec Section 3): the OAuth redirect target.

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

    return IntegrationConnectionResponse.model_validate(connection)
