"""Authentication for the platform console (migration 0023).

The mirror image of `app/core/deps.py`'s `get_current_user`, and
deliberately a separate function rather than a branch inside it. That one
is the most security-critical code in this codebase — every request in the
product runs through it — and the whole point of this design is that
adding cross-tenant administration did not require editing its logic. The
only change made there is a two-line scope check that *narrows* what it
accepts.

Three independent things must all hold before a caller is a platform
admin, and they fail in this order:

  1. the bearer token carries `scope: "platform"` — an ordinary access
     token, however privileged its holder is inside their own company,
     cannot reach these routes;
  2. the user still exists;
  3. an unrevoked `platform_admins` row names them — checked on EVERY
     request, not baked into the token, so revoking the privilege takes
     effect within one request rather than one token lifetime.

Step 3 reads through the ordinary RLS-constrained `app_user` connection
under the `self_read` policy, which is scoped to `app.current_user_id`.
That is not a workaround: the policy exists precisely so the request path
can answer "am *I* a platform admin?" without being able to enumerate who
else is.
"""
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import bearer_token_ctx
from app.core.platform_db import PlatformSessionLocal
from app.core.security import PLATFORM_SCOPE, InvalidTokenError, decode_access_token
from app.db import SessionLocal, set_current_user
from app.models import PlatformAdmin, User


@dataclass
class PlatformActor:
    user: User
    # The BYPASSRLS session the console's cross-tenant work runs on. Held
    # open for the request and committed after the handler returns, the same
    # shape `get_current_user` uses.
    session: AsyncSession


async def get_platform_admin():
    token = bearer_token_ctx.get()
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    try:
        payload = decode_access_token(token)
    except InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    if payload.get("scope") != PLATFORM_SCOPE:
        # Same message and status as a bad token on purpose. A tenant admin
        # probing this route learns that it exists (the path is in the
        # OpenAPI schema anyway) but not whether their own account is one
        # scope away from reaching it.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")

    if PlatformSessionLocal is None:
        # PLATFORM_DATABASE_URL unset. A loud "not configured" rather than
        # falling back to a wider connection — see app/core/platform_db.py.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The platform console is not configured on this deployment",
        )

    user_id = uuid.UUID(payload["sub"])

    # Privilege check on the ORDINARY connection, under the self_read RLS
    # policy. Deliberately not on the BYPASSRLS session below: doing the
    # "may this caller act?" read through a connection that ignores RLS
    # would mean the check and the policy protecting it never meet.
    async with SessionLocal() as auth_session:
        async with auth_session.begin():
            await set_current_user(auth_session, str(user_id))
            result = await auth_session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if user is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")

            grant = await auth_session.execute(
                select(PlatformAdmin).where(
                    PlatformAdmin.user_id == user_id, PlatformAdmin.revoked_at.is_(None)
                )
            )
            if grant.scalar_one_or_none() is None:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a platform administrator")

    session = PlatformSessionLocal()
    try:
        await session.begin()
        yield PlatformActor(user=user, session=session)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
