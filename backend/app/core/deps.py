import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import bearer_token_ctx, claimed_tenant_id_ctx
from app.core.security import InvalidTokenError, decode_access_token
from app.db import SessionLocal, set_current_tenant, set_current_user
from app.models import CompanyUser, User


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
