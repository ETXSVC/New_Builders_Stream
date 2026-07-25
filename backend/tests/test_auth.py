import time

import jwt
import pyotp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models import Subscription
from app.routers.auth import _email_rate_limit_key
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


async def test_register_succeeds_when_redis_is_down(client, monkeypatch):
    """The limiter fails OPEN on a Redis outage (WARNING-logged): failing
    closed would turn a Redis outage into a total signup outage — see
    check_rate_limit's docstring for the trade-off."""
    monkeypatch.setattr(settings, "register_rate_limit_enabled", True)
    # Point the limiter at a port nothing listens on, with a fresh client
    # bound to this test's loop; reset again afterwards so the poisoned
    # client can't leak into later tests (the event-loop hazard
    # _reset_redis_client_for_tests documents).
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:1/0")
    _reset_redis_client_for_tests()
    try:
        response = await client.post(
            "/auth/register",
            json={
                "company_name": "Redis Down Co",
                "admin_full_name": "Fail Open",
                "admin_email": "fail-open@acme.test",
                "admin_password": "supersecret123",
            },
        )
        assert response.status_code == 201, response.text
    finally:
        _reset_redis_client_for_tests()


# =============================================================================
# Login + TOTP throttling
#
# /auth/register was rate limited from the start; /auth/login was not, which
# left unlimited password guessing against a known address — and unlimited
# TOTP guessing behind it, since the replay guard only blocks reuse of a
# code, never a fresh guess.
# =============================================================================


async def _register_for_login_tests(client, email):
    response = await client.post(
        "/auth/register",
        json={
            "company_name": f"Login RL {email}",
            "admin_full_name": "Rate Limited",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert response.status_code == 201, response.text


async def test_login_rate_limited_per_email_after_max_attempts(client, monkeypatch):
    """Many hosts grinding ONE account is what the per-email counter sees
    and the per-IP counter cannot. Wrong-password attempts must count, or
    the limiter would only ever throttle successful logins."""
    monkeypatch.setattr(settings, "login_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "login_rate_limit_email_max_attempts", 2)
    monkeypatch.setattr(settings, "login_rate_limit_email_window_seconds", 60)
    # High enough not to fire first and mask the per-email result.
    monkeypatch.setattr(settings, "login_rate_limit_ip_max_attempts", 1000)

    _reset_redis_client_for_tests()
    redis_client = _get_redis_client()
    email = "login-rl-email@acme.test"
    email_key = _email_rate_limit_key("ratelimit:login:email", email)
    await redis_client.delete(email_key, "ratelimit:login:ip:127.0.0.1")

    await _register_for_login_tests(client, email)

    for _ in range(2):
        attempt = await client.post(
            "/auth/login", json={"email": email, "password": "wrong-password"}
        )
        assert attempt.status_code == 401, attempt.text

    # The correct password must NOT get through once the window is spent —
    # otherwise the limiter would be trivially bypassed by the one guess
    # that matters.
    blocked = await client.post(
        "/auth/login", json={"email": email, "password": "supersecret123"}
    )
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["detail"] == "Too many login attempts. Please try again later."

    await redis_client.delete(email_key, "ratelimit:login:ip:127.0.0.1")


async def test_login_rate_limited_per_ip_across_different_emails(client, monkeypatch):
    """One host spraying MANY accounts is invisible to the per-email
    counter — each address gets its own budget — so the per-IP counter has
    to catch it."""
    monkeypatch.setattr(settings, "login_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "login_rate_limit_ip_max_attempts", 2)
    monkeypatch.setattr(settings, "login_rate_limit_ip_window_seconds", 60)
    monkeypatch.setattr(settings, "login_rate_limit_email_max_attempts", 1000)

    _reset_redis_client_for_tests()
    redis_client = _get_redis_client()
    ip_key = "ratelimit:login:ip:127.0.0.1"
    await redis_client.delete(ip_key)

    for i in range(2):
        attempt = await client.post(
            "/auth/login", json={"email": f"spray-{i}@acme.test", "password": "whatever"}
        )
        assert attempt.status_code == 401, attempt.text

    blocked = await client.post(
        "/auth/login", json={"email": "spray-3@acme.test", "password": "whatever"}
    )
    assert blocked.status_code == 429, blocked.text

    await redis_client.delete(ip_key)


async def test_login_rate_limit_key_does_not_store_the_plaintext_email(client):
    """Redis is an operational cache — keys surface in MONITOR, KEYS,
    slowlogs and debug dumps — so the per-address counter must not turn it
    into a roster of every address that has attempted a login."""
    key = _email_rate_limit_key("ratelimit:login:email", "Someone@Example.test")

    assert "someone@example.test" not in key.lower()
    assert key.startswith("ratelimit:login:email:")
    # Case- and whitespace-insensitive, so one address is one counter.
    assert key == _email_rate_limit_key("ratelimit:login:email", "  SOMEONE@example.TEST ")


async def test_totp_verification_is_rate_limited(client, monkeypatch):
    """A 6-digit code is a 1-in-a-million guess and the replay guard only
    blocks REUSE, so without a limiter an attacker holding the password
    could grind the entire code space."""
    monkeypatch.setattr(settings, "totp_rate_limit_max_attempts", 2)
    monkeypatch.setattr(settings, "totp_rate_limit_window_seconds", 60)

    _reset_redis_client_for_tests()
    redis_client = _get_redis_client()

    email = "totp-rl@acme.test"
    await _register_for_login_tests(client, email)
    login = await client.post(
        "/auth/login", json={"email": email, "password": "supersecret123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    enroll = await client.post("/auth/mfa/enroll", headers=headers)
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    # Activate with the PREVIOUS step, so the replay guard doesn't burn the
    # current one — see test_mfa_totp.py's helper for the full rationale.
    previous_step_code = pyotp.TOTP(secret).at(int(time.time()) - 30)
    activate = await client.post(
        "/auth/mfa/activate", json={"totp_code": previous_step_code}, headers=headers
    )
    assert activate.status_code == 204, activate.text

    user_id = jwt.decode(
        login.json()["access_token"], settings.jwt_secret, algorithms=["HS256"]
    )["sub"]
    await redis_client.delete(f"ratelimit:totp:{user_id}")

    for _ in range(2):
        attempt = await client.post(
            "/auth/login",
            json={"email": email, "password": "supersecret123", "totp_code": "000000"},
        )
        assert attempt.status_code == 401, attempt.text

    blocked = await client.post(
        "/auth/login",
        json={"email": email, "password": "supersecret123", "totp_code": pyotp.TOTP(secret).now()},
    )
    assert blocked.status_code == 429, blocked.text

    await redis_client.delete(f"ratelimit:totp:{user_id}")
