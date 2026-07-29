import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.after_commit import run_after_commit
from app.core.context import bearer_token_ctx, claimed_tenant_id_ctx
from app.core.observability import tag_current_tenant
from app.core.security import TENANT_SCOPE, InvalidTokenError, decode_access_token
from app.db import SessionLocal, set_current_tenant, set_current_user
from app.models import CompanyUser, Subscription, User


@dataclass
class CurrentUser:
    user: User
    company_id: uuid.UUID
    role: str
    session: AsyncSession


async def get_current_user():
    """A FastAPI "dependency with yield": everything after `yield` runs after
    the route handler returns (success or exception), not inline here. This
    is required, not stylistic — set_current_user/set_current_tenant use
    set_config(..., is_local=true), which is transaction-scoped (design
    decision #7). If this function committed the transaction before handing
    CurrentUser to the route handler, the tenant context would already be
    gone by the time route handlers (Task 12+) reuse CurrentUser.session for
    their own queries, and RLS would deny access to the caller's own data.
    Verified empirically: the same scenario with an eager commit() here
    returns zero rows for a route handler's own company; with the commit
    deferred past `yield`, it correctly returns the row.

    **Every `Depends(get_current_user)` in this codebase MUST pass
    `scope="function"`.** Deferring the commit past `yield` is necessary but
    not sufficient — it also matters *how far* past. FastAPI runs a
    dependency's exit code on one of two exit stacks (`fastapi/routing.py`):

        async with AsyncExitStack() as request_stack:      # closes LAST
            async with AsyncExitStack() as function_stack: # closes FIRST
                response = await f(request)
            await response(scope, receive, send)           # response sent here

    A generator dependency defaults to the `request` stack
    (`_get_computed_scope` returns "request" for any gen callable), which
    closes *after* the response has already been sent. So `POST /invitations`
    returned `201 Created` to the client and committed afterwards — a caller
    that immediately used the returned id could beat the commit and get a 404,
    because its new transaction's READ COMMITTED snapshot predated the insert.

    That was reproduced, not theorised: 400 concurrent create-then-accept
    cycles produced 5 x 404, and instrumenting the miss showed the RLS probe
    GUC set correctly and the row visible milliseconds later — the row simply
    wasn't committed yet when the probe's snapshot was taken.

    `scope="function"` moves the teardown to the inner stack, which closes
    *before* `await response(...)`, so the commit is durable by the time the
    client can act on the response. The scope is part of the dependency cache
    key (`_get_cache_key`), so a mix of scoped and unscoped call sites would
    resolve `get_current_user` TWICE per request — two sessions, two
    transactions, only one committed. It is all-or-nothing on purpose.
    """
    token = bearer_token_ctx.get()
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    try:
        payload = decode_access_token(token)
    except InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    # A platform-console token must never authenticate an ordinary API
    # request (migration 0023). Written as an allow-list rather than
    # `!= PLATFORM_SCOPE` so a scope added later fails closed here instead
    # of silently inheriting tenant access; the default covers tokens minted
    # before the claim existed, which stay valid until they expire.
    if payload.get("scope", TENANT_SCOPE) != TENANT_SCOPE:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    user_id = uuid.UUID(payload["sub"])
    claimed_tenant = claimed_tenant_id_ctx.get() or payload["default_company_id"]
    try:
        claimed_tenant_uuid = uuid.UUID(claimed_tenant)
    except (ValueError, AttributeError, TypeError):
        # claimed_tenant is attacker-controlled when it comes from the
        # X-Tenant-ID header (design decision #3) — a malformed value must
        # fail cleanly here, before a session is opened, rather than surface
        # as an unhandled 500 from the bare uuid.UUID() call below.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Malformed X-Tenant-ID header")

    session = SessionLocal()
    try:
        await session.begin()
        await set_current_user(session, str(user_id))

        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")

        # Verify membership via the self_membership RLS policy BEFORE trusting
        # the claimed tenant (design decision #3) — this is what stops a
        # spoofed X-Tenant-ID from granting access to a company the user
        # doesn't belong to.
        result = await session.execute(
            select(CompanyUser).where(
                CompanyUser.user_id == user_id, CompanyUser.company_id == claimed_tenant_uuid
            )
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this company")

        await set_current_tenant(session, str(claimed_tenant_uuid))

        # Tag the event scope with the VERIFIED tenant, once membership has
        # actually been checked above. Deliberately not the X-Tenant-ID
        # header, which is attacker-controlled (design decision #3) and so
        # says only what was claimed. A no-op when Sentry is off.
        tag_current_tenant(claimed_tenant_uuid, membership.role)

        # The transaction stays open here — do not commit before yielding.
        # See this function's docstring.
        yield CurrentUser(user=user, company_id=claimed_tenant_uuid, role=membership.role, session=session)

        await session.commit()
        # Side effects that must not happen unless the data did — today,
        # the accounting-sync enqueues. Deliberately AFTER the commit and
        # inside the try, so a rollback path never reaches it. See
        # app/core/after_commit.py for why ordering these the other way
        # round was a real bug rather than a style question.
        run_after_commit(session)
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def require_role(*allowed_roles: str):
    async def dependency(current: CurrentUser = Depends(get_current_user, scope="function")) -> CurrentUser:
        if current.role not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires one of roles: {allowed_roles}")
        return current

    return dependency


async def block_if_read_only(
    request: Request, current: CurrentUser = Depends(get_current_user, scope="function")
) -> None:
    """Task 3.24 (design spec Section 6). GET/HEAD/OPTIONS always pass —
    only non-read methods are subject to this check. Resolves the caller's
    ROOT company and checks ITS subscription's status: anything other than
    'trialing' or 'active' blocks the write with 403. This collapses
    Stripe's more granular dunning states into one simple rule rather than
    mirroring Stripe's exact status machine.

    `current: CurrentUser = Depends(get_current_user, scope="function")` is deliberately the
    SAME dependency every write route's own `require_role(...)` already
    depends on — FastAPI caches a dependency's result per request by
    callable+params, so declaring this alongside `require_role(...)` on the
    same route does not cause a second JWT decode or a second DB round trip
    for get_current_user's own work.

    Root resolution and the subscription lookup are ONE query, not two —
    `func.get_root_company_id(...)` inlined directly in the WHERE clause,
    same pattern `app/routers/subscriptions.py`'s own
    `_get_subscription_for_current` already uses. This dependency runs on
    every non-GET request across every write route in the app, so an
    avoidable extra round trip here is not a one-off cost.

    If no subscription row exists at all for the resolved root (should be
    unreachable — every root gets one atomically at registration), this
    fails OPEN rather than blocking — treated as an unreachable state, not
    something to build defensive handling for.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    status_result = await current.session.execute(
        select(Subscription.status).where(
            Subscription.company_id == func.get_root_company_id(current.company_id)
        )
    )
    status_value = status_result.scalar_one_or_none()

    if status_value is not None and status_value not in ("trialing", "active"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your subscription requires attention before you can make changes",
        )
