"""Task 3.15+: Subscription model, RLS, and root-only-ownership behavior.
More coverage (endpoints, RBAC, trial creation) is added in later tasks in
this same file."""
import uuid

import pytest
from sqlalchemy import select

from app.models import Subscription


@pytest.mark.anyio
async def test_subscription_model_round_trips_all_columns(client):
    # No HTTP route exists yet (this task is model/migration only) — insert
    # directly via a raw owner-role connection the same way
    # test_tenant_isolation_phase3.py's own helpers do, to prove the table
    # and its columns exist and accept the values the design spec requires,
    # before any endpoint is built on top of it.
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from tests.conftest import TEST_DATABASE_URL

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            # A bare company row with no parent — root company, per this
            # table's own ownership rule.
            company_id = uuid.uuid4()
            await session.execute(
                __import__("sqlalchemy").text(
                    "INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Root Co')"
                ),
                {"id": company_id},
            )
            subscription_id = uuid.uuid4()
            session.add(
                Subscription(
                    id=subscription_id,
                    company_id=company_id,
                    stripe_customer_id="cus_fake123",
                    stripe_subscription_id="sub_fake123",
                    tier="pro",
                    status="trialing",
                    included_seats=10,
                    current_period_end=None,
                )
            )
            await session.commit()

            result = await session.execute(
                select(Subscription).where(Subscription.id == subscription_id)
            )
            row = result.scalar_one()
            assert row.company_id == company_id
            assert row.tier == "pro"
            assert row.status == "trialing"
            assert row.included_seats == 10
    finally:
        await engine.dispose()
