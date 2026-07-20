from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Subscription
from app.services.rate_limit import _get_redis_client, _reset_redis_client_for_tests
from tests.conftest import TEST_DATABASE_URL


async def test_register_creates_company_and_admin_user(client):
    response = await client.post(
        "/auth/register",
        json={
            "company_name": "Acme Construction",
            "admin_full_name": "Ada Lovelace",
            "admin_email": "ada@acme.test",
            "admin_password": "supersecret123",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "ada@acme.test"
    assert "company_id" in body
    assert "user_id" in body


async def test_register_rejects_duplicate_email(client):
    payload = {
        "company_name": "Acme Construction",
        "admin_full_name": "Ada Lovelace",
        "admin_email": "ada@acme.test",
        "admin_password": "supersecret123",
    }
    first = await client.post("/auth/register", json=payload)
    assert first.status_code == 201

    second = await client.post("/auth/register", json={**payload, "company_name": "Beta Builders"})
    assert second.status_code == 409


async def test_register_rate_limited_after_max_attempts(client, monkeypatch):
    # conftest.py disables this limiter globally (REGISTER_RATE_LIMIT_ENABLED=
    # false) so the rest of the suite's dozens of /auth/register calls — all
    # reported as the same 127.0.0.1 client under httpx's ASGITransport —
    # don't trip it. Re-enabled here, scoped to this test only, with a low
    # limit so the test doesn't need dozens of requests to exercise it.
    monkeypatch.setattr(settings, "register_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "register_rate_limit_max_attempts", 2)
    monkeypatch.setattr(settings, "register_rate_limit_window_seconds", 60)

    # Force a fresh Redis client bound to THIS test's own event loop — see
    # _reset_redis_client_for_tests's docstring. Without this, whichever
    # test in the suite happens to trigger rate_limit.py's module-level
    # singleton first "wins" the loop it gets created on; any other test
    # that reuses it from a different (later, by-then-closed) loop raises
    # "Event loop is closed", the same class of bug already diagnosed once
    # for the DB engine (see db_session's loop_scope="function" above).
    _reset_redis_client_for_tests()
    redis_client = _get_redis_client()
    await redis_client.delete("ratelimit:register:127.0.0.1")

    payload = {
        "company_name": "Rate Limit Co",
        "admin_full_name": "Rate Limiter",
        "admin_email": "unused@example.test",
        "admin_password": "supersecret123",
    }

    # The limiter counts attempts, not successful registrations, so two
    # ordinary successes still consume the whole limit.
    first = await client.post("/auth/register", json={**payload, "admin_email": "rl1@acme.test"})
    assert first.status_code == 201
    second = await client.post("/auth/register", json={**payload, "admin_email": "rl2@acme.test"})
    assert second.status_code == 201

    third = await client.post("/auth/register", json={**payload, "admin_email": "rl3@acme.test"})
    assert third.status_code == 429
    assert third.json()["detail"] == "Too many registration attempts. Please try again later."

    await redis_client.delete("ratelimit:register:127.0.0.1")


async def test_login_returns_token_for_valid_credentials(client):
    await client.post(
        "/auth/register",
        json={
            "company_name": "Acme Construction",
            "admin_full_name": "Ada Lovelace",
            "admin_email": "ada@acme.test",
            "admin_password": "supersecret123",
        },
    )

    response = await client.post("/auth/login", json={"email": "ada@acme.test", "password": "supersecret123"})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 20
    assert "default_company_id" in body


async def test_login_rejects_wrong_password(client):
    await client.post(
        "/auth/register",
        json={
            "company_name": "Acme Construction",
            "admin_full_name": "Ada Lovelace",
            "admin_email": "ada@acme.test",
            "admin_password": "supersecret123",
        },
    )

    response = await client.post("/auth/login", json={"email": "ada@acme.test", "password": "wrong"})
    assert response.status_code == 401


async def test_login_rejects_unknown_email(client):
    response = await client.post("/auth/login", json={"email": "nobody@nowhere.test", "password": "whatever123"})
    assert response.status_code == 401


async def test_register_creates_a_trialing_pro_subscription(client):
    response = await client.post(
        "/auth/register",
        json={
            "company_name": "New Co",
            "admin_email": "trial-owner@newco.test",
            "admin_password": "correct horse battery staple",
            "admin_full_name": "New Owner",
        },
    )
    assert response.status_code == 201, response.text
    company_id = response.json()["company_id"]

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        async with session_factory() as session:
            result = await session.execute(
                select(Subscription).where(Subscription.company_id == company_id)
            )
            subscription = result.scalar_one()
            assert subscription.tier == "pro"
            assert subscription.status == "trialing"
            assert subscription.included_seats == 10
            assert subscription.stripe_customer_id.startswith("cus_fake_")
            assert subscription.stripe_subscription_id.startswith("sub_fake_")
    finally:
        await engine.dispose()


async def test_login_and_refresh_return_role(client):
    await client.post(
        "/auth/register",
        json={
            "company_name": "Role Co",
            "admin_full_name": "Role Admin",
            "admin_email": "role-admin@acme.test",
            "admin_password": "supersecret123",
        },
    )
    login = await client.post(
        "/auth/login", json={"email": "role-admin@acme.test", "password": "supersecret123"}
    )
    assert login.status_code == 200
    body = login.json()
    assert body["role"] == "admin"

    refresh = await client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert refresh.status_code == 200
    assert refresh.json()["role"] == "admin"
