"""Task 3.24: block_if_read_only (design spec Section 6). Unit-level tests
against the dependency function directly — no HTTP route needed yet, since
no route is wired to it until later tasks. A later task adds the
completeness introspection test and an end-to-end behavior test once
routes exist."""
import uuid
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.deps import CurrentUser, block_if_read_only
from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL


@dataclass
class _FakeRequest:
    method: str


async def _make_current_user_for_status(status_value: str) -> tuple[CurrentUser, callable]:
    """Builds a real Subscription row with the given status, then a real
    app_user-role session scoped to that company — not a mock session — so
    this test exercises the actual RLS-backed get_root_company_id query
    path, not a stubbed one."""
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    company_id = uuid.uuid4()
    async with owner_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'RO Test Co')"),
            {"id": company_id},
        )
        await conn.execute(
            text(
                "INSERT INTO subscriptions "
                "(id, company_id, stripe_customer_id, stripe_subscription_id, tier, status, included_seats) "
                "VALUES (:id, :company_id, 'cus_x', 'sub_x', 'pro', :status, 10)"
            ),
            {"id": uuid.uuid4(), "company_id": company_id, "status": status_value},
        )
    await owner_engine.dispose()

    app_engine = create_async_engine(TEST_APP_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    session = session_factory()
    await session.execute(
        text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(company_id)}
    )
    current = CurrentUser(user=None, company_id=company_id, role="admin", session=session)

    async def cleanup() -> None:
        # session.execute() above implicitly opens a transaction that is
        # never committed/rolled back by this helper's own callers — engine
        # .dispose() alone does NOT roll back or close an in-flight session
        # first, so without this, the underlying connection sits
        # "idle in transaction" in Postgres indefinitely, holding locks
        # that block a LATER test's TRUNCATE-based cleanup fixture
        # (conftest.py's _clean_tables) forever. Reproduced directly: this
        # exact bug hung a full pytest run for over an hour with Postgres
        # itself perfectly healthy — pg_stat_activity showed two
        # "idle in transaction" app_user connections from this helper
        # blocking a TRUNCATE. Roll back (not commit — nothing here should
        # persist past the test) before disposing the engine.
        await session.rollback()
        await session.close()
        await app_engine.dispose()

    return current, cleanup


async def test_get_requests_always_pass_through():
    current, cleanup = await _make_current_user_for_status("canceled")
    try:
        await block_if_read_only(request=_FakeRequest(method="GET"), current=current)
    finally:
        await cleanup()


async def test_post_passes_when_trialing():
    current, cleanup = await _make_current_user_for_status("trialing")
    try:
        await block_if_read_only(request=_FakeRequest(method="POST"), current=current)
    finally:
        await cleanup()


async def test_post_passes_when_active():
    current, cleanup = await _make_current_user_for_status("active")
    try:
        await block_if_read_only(request=_FakeRequest(method="POST"), current=current)
    finally:
        await cleanup()


async def test_post_blocked_when_past_due():
    current, cleanup = await _make_current_user_for_status("past_due")
    try:
        with pytest.raises(HTTPException) as exc_info:
            await block_if_read_only(request=_FakeRequest(method="POST"), current=current)
        assert exc_info.value.status_code == 403
    finally:
        await cleanup()


async def test_post_blocked_when_canceled():
    current, cleanup = await _make_current_user_for_status("canceled")
    try:
        with pytest.raises(HTTPException) as exc_info:
            await block_if_read_only(request=_FakeRequest(method="POST"), current=current)
        assert exc_info.value.status_code == 403
    finally:
        await cleanup()
