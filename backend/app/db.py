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
    remainder of the current transaction."""
    await session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": user_id})


async def set_current_tenant(session: AsyncSession, company_id: str) -> None:
    """Scopes every tenant-isolation RLS policy to this company (and its descendants)
    for the remainder of the current transaction."""
    await session.execute(text("SET LOCAL app.current_tenant = :cid"), {"cid": company_id})
