import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import bearer_token_ctx, claimed_tenant_id_ctx
from app.core.security import InvalidTokenError, decode_access_token
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
    """
    token = bearer_token_ctx.get()
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    try:
        payload = decode_access_token(token)
    except InvalidTokenError:
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

        # The transaction stays open here — do not commit before yielding.
        # See this function's docstring.
        yield CurrentUser(user=user, company_id=claimed_tenant_uuid, role=membership.role, session=session)

        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def require_role(*allowed_roles: str):
    async def dependency(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current.role not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires one of roles: {allowed_roles}")
        return current

    return dependency


async def get_root_company_id(session: AsyncSession, company_id: uuid.UUID) -> uuid.UUID:
    """Thin wrapper around the `get_root_company_id` SQL function (migration
    0010_billing_schema.py). Shared by `block_if_read_only` below and
    `app/routers/subscriptions.py`'s own subscription-lookup helper — DRY:
    both need "which root company governs this session's effective
    subscription" and neither should duplicate the raw `select(func...)`
    call."""
    result = await session.execute(select(func.get_root_company_id(company_id)))
    return result.scalar_one()


async def block_if_read_only(
    request: Request, current: CurrentUser = Depends(get_current_user)
) -> None:
    """Task 3.24 (design spec Section 6). GET/HEAD/OPTIONS always pass —
    only non-read methods are subject to this check. Resolves the caller's
    ROOT company and checks ITS subscription's status: anything other than
    'trialing' or 'active' blocks the write with 403. This collapses
    Stripe's more granular dunning states into one simple rule rather than
    mirroring Stripe's exact status machine.

    `current: CurrentUser = Depends(get_current_user)` is deliberately the
    SAME dependency every write route's own `require_role(...)` already
    depends on — FastAPI caches a dependency's result per request by
    callable+params, so declaring this alongside `require_role(...)` on the
    same route does not cause a second JWT decode or a second DB round trip
    for get_current_user's own work.

    If no subscription row exists at all for the resolved root (should be
    unreachable — every root gets one atomically at registration), this
    fails OPEN rather than blocking — treated as an unreachable state, not
    something to build defensive handling for.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    root_id = await get_root_company_id(current.session, current.company_id)

    status_result = await current.session.execute(
        select(Subscription.status).where(Subscription.company_id == root_id)
    )
    status_value = status_result.scalar_one_or_none()

    if status_value is not None and status_value not in ("trialing", "active"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your subscription requires attention before you can make changes",
        )
