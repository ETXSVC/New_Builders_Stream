from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Runtime engine: connects as the restricted `app_user` role (see design decision #1).
# RLS policies are enforced against this connection.
engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def set_current_user(session: AsyncSession, user_id: str) -> None:
    """Scopes the self-membership RLS policy (design decision #3) to this user for the
    remainder of the current transaction.

    Uses set_config(), not `SET LOCAL ... = :param`, because PostgreSQL's SET/SET LOCAL
    grammar only accepts a literal in the value position — a bound parameter there is a
    syntax error at the server, regardless of driver. set_config(name, value, is_local)
    is a plain function call, so it accepts bound parameters normally; is_local=true
    gives it the same transaction-scoped reset-on-commit/rollback semantics as SET LOCAL.
    """
    await session.execute(
        text("SELECT set_config('app.current_user_id', :uid, true)"), {"uid": user_id}
    )


async def set_current_tenant(session: AsyncSession, company_id: str) -> None:
    """Scopes every tenant-isolation RLS policy to this company (and its descendants)
    for the remainder of the current transaction. See set_current_user's docstring for
    why this uses set_config() instead of SET LOCAL with a bound parameter."""
    await session.execute(
        text("SELECT set_config('app.current_tenant', :cid, true)"), {"cid": company_id}
    )
