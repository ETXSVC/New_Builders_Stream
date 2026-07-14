"""Task 3.15+: Subscription model, RLS, and root-only-ownership behavior.
More coverage (endpoints, RBAC, trial creation) is added in later tasks in
this same file."""
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import Subscription
from tests.conftest import TEST_DATABASE_URL


async def test_subscription_model_round_trips_all_columns():
    # No HTTP route exists yet (this task is model/migration only) — insert
    # directly via a raw owner-role connection the same way
    # test_tenant_isolation_phase3.py's own helpers do, to prove the table
    # and its columns exist and accept the values the design spec requires,
    # before any endpoint is built on top of it.
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            # A bare company row with no parent — root company, per this
            # table's own ownership rule.
            company_id = uuid.uuid4()
            await session.execute(
                text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'Root Co')"),
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


async def _register(client, company_name="Sub Test Co", email="admin@subtest.test"):
    response = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_email": email,
            "admin_password": "correct horse battery staple",
            "admin_full_name": "Test Admin",
        },
    )
    assert response.status_code == 201, response.text
    login = await client.post("/auth/login", json={"email": email, "password": "correct horse battery staple"})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": response.json()["company_id"]}


async def test_get_subscriptions_me_returns_the_trial_for_admin(client):
    admin = await _register(client)

    response = await client.get("/subscriptions/me", headers=admin["headers"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "pro"
    assert body["status"] == "trialing"
    assert body["included_seats"] == 10


async def test_get_subscriptions_me_forbidden_for_project_manager(client):
    admin = await _register(client, email="pm-forbidden-admin@subtest.test")
    invite = await client.post(
        "/invitations",
        json={"email": "pm@subtest.test", "role": "project_manager"},
        headers=admin["headers"],
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "correct horse battery staple"},
    )
    assert accept.status_code == 200, accept.text
    pm_login = await client.post(
        "/auth/login", json={"email": "pm@subtest.test", "password": "correct horse battery staple"}
    )
    pm_token = pm_login.json()["access_token"]

    response = await client.get(
        "/subscriptions/me", headers={"Authorization": f"Bearer {pm_token}"}
    )
    assert response.status_code == 403


async def test_portal_session_returns_a_url_for_admin(client):
    admin = await _register(client, email="portal-admin@subtest.test")

    response = await client.post("/subscriptions/portal-session", headers=admin["headers"])

    assert response.status_code == 200, response.text
    assert response.json()["url"].startswith("https://")
