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


async def get_current_user() -> CurrentUser:
    token = bearer_token_ctx.get()
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    try:
        payload = decode_access_token(token)
    except InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    user_id = uuid.UUID(payload["sub"])
    claimed_tenant = claimed_tenant_id_ctx.get() or payload["default_company_id"]
    claimed_tenant_uuid = uuid.UUID(claimed_tenant)

    session = SessionLocal()
    try:
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
        await session.commit()

        # KNOWN CONCERN (surfaced during Task 11, not fixed here — see task
        # report): set_current_user/set_current_tenant use
        # set_config(..., is_local=true), which is transaction-scoped. This
        # commit() ends that transaction, so current_setting() for both GUCs
        # reads back '' (not the value just set — design decision #7) on any
        # later statement, including ones issued via `session` after this
        # function returns. Confirmed empirically against Postgres directly:
        # a SELECT current_setting(...) run immediately after a commit that
        # followed a set_config(..., true) call in the same transaction
        # returns '', not the set value. Task 12's planned get_company (and
        # Task 13's create_child_company) call `current.session.execute(...)`
        # directly, expecting the tenant context established here to still be
        # active — as written, it will not be, and RLS will deny rows that
        # legitimately belong to the caller. Removing this commit() instead
        # leaves the transaction (and its locks) open for the lifetime of the
        # session object, which — since nothing in this plan currently closes
        # or commits a read-only request's session — deadlocks this test
        # suite's own `_clean_tables` autouse fixture (its raw-connection
        # TRUNCATE blocks forever on the lock; reproduced against live
        # Postgres). Neither option is fully correct without also adding a
        # request-scoped teardown that commits/closes `current.session`
        # exactly once, after the route handler runs — that's out of this
        # task's file scope (deps.py only). Flagging for whoever implements
        # Task 12: reconcile before relying on `current.session` for
        # tenant-scoped queries downstream of this dependency.
        return CurrentUser(user=user, company_id=claimed_tenant_uuid, role=membership.role, session=session)
    except Exception:
        await session.close()
        raise


def require_role(*allowed_roles: str):
    async def dependency(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current.role not in allowed_roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires one of roles: {allowed_roles}")
        return current

    return dependency
