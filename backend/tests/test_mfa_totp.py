"""MFA/TOTP (docs/superpowers/specs/2026-07-16-mfa-totp-design.md).

Covers: enroll/activate (Task 7.3), the login challenge +
mfa_enrollment_required signal (7.4), disable + change-password
hardening (7.5). One file for the feature, real pyotp codes throughout.
"""
import time
import uuid

import pyotp

from tests.test_auth_token_lifecycle import _register_and_login

_TOTP_PERIOD_SECONDS = 30


async def _enroll_and_activate(client, ctx) -> str:
    """Enrolls + activates MFA for ctx's user; returns the base32 secret.

    Activates with a code from the PREVIOUS 30s step, not .now(): the
    ±1-step skew window accepts it, but the replay guard then records that
    PRIOR step as spent — leaving the CURRENT step free. Using .now() here
    would burn the current step at activation, and a caller then logging
    in immediately afterward with pyotp.TOTP(secret).now() would collide
    with that same step and get refused (candidate_step <= last_used) on
    ~97% of runs, since the two calls land in the same 30s window far more
    often than not. This ordering is what makes back-to-back
    activate-then-login deterministic (Task 7.3 review finding 3).
    """
    enroll = await client.post("/auth/mfa/enroll", headers=ctx["headers"])
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    previous_step_code = pyotp.TOTP(secret).at(int(time.time()) - _TOTP_PERIOD_SECONDS)
    activate = await client.post(
        "/auth/mfa/activate",
        json={"totp_code": previous_step_code},
        headers=ctx["headers"],
    )
    assert activate.status_code == 204, activate.text
    return secret


async def test_enroll_returns_secret_and_uri_once(client):
    ctx = await _register_and_login(client)
    r = await client.post("/auth/mfa/enroll", headers=ctx["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"secret", "otpauth_uri"}
    assert body["otpauth_uri"].startswith("otpauth://totp/")
    assert "Builders%20Stream" in body["otpauth_uri"] or "Builders+Stream" in body["otpauth_uri"]
    assert f"secret={body['secret']}" in body["otpauth_uri"]
    assert r.headers.get("Cache-Control") == "no-store"


async def test_enrollment_secret_is_stored_encrypted(client):
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)
    r = await client.post("/auth/mfa/enroll", headers=ctx["headers"])
    secret = r.json()["secret"]
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        row = await conn.fetchrow(
            "SELECT totp_secret_encrypted, mfa_activated_at FROM users WHERE id = $1",
            uuid.UUID(ctx["user_id"]),
        )
    finally:
        await conn.close()
    assert row["totp_secret_encrypted"] is not None
    assert secret not in row["totp_secret_encrypted"]  # ciphertext, not plaintext
    assert row["mfa_activated_at"] is None  # pending, not active


async def test_activate_happy_path_writes_audit_row(client):
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)
    await _enroll_and_activate(client, ctx)
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        row = await conn.fetchrow(
            "SELECT mfa_activated_at FROM users WHERE id = $1", uuid.UUID(ctx["user_id"])
        )
        audit = await conn.fetch(
            "SELECT action FROM audit_log WHERE action = 'auth.mfa_activated'"
        )
    finally:
        await conn.close()
    assert row["mfa_activated_at"] is not None
    assert len(audit) == 1


async def test_activate_with_no_pending_enrollment_is_400(client):
    ctx = await _register_and_login(client)
    r = await client.post(
        "/auth/mfa/activate", json={"totp_code": "123456"}, headers=ctx["headers"]
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "No MFA enrollment pending"


async def test_activate_with_wrong_code_is_401(client):
    ctx = await _register_and_login(client)
    enroll = await client.post("/auth/mfa/enroll", headers=ctx["headers"])
    secret = enroll.json()["secret"]
    wrong = pyotp.TOTP(secret).at(0)  # a code from 1970 — structurally valid, certainly wrong
    r = await client.post(
        "/auth/mfa/activate", json={"totp_code": wrong}, headers=ctx["headers"]
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid TOTP code"


async def test_enroll_while_active_is_409_but_reenroll_while_pending_rotates(client):
    ctx = await _register_and_login(client)
    first = await client.post("/auth/mfa/enroll", headers=ctx["headers"])
    second = await client.post("/auth/mfa/enroll", headers=ctx["headers"])
    assert second.status_code == 200  # pending enrollment may be regenerated
    assert second.json()["secret"] != first.json()["secret"]
    activate = await client.post(
        "/auth/mfa/activate",
        json={"totp_code": pyotp.TOTP(second.json()["secret"]).now()},
        headers=ctx["headers"],
    )
    assert activate.status_code == 204, activate.text
    third = await client.post("/auth/mfa/enroll", headers=ctx["headers"])
    assert third.status_code == 409
    assert third.json()["detail"] == "MFA is already active"


async def test_pending_enrollment_does_not_gate_login(client):
    ctx = await _register_and_login(client)
    await client.post("/auth/mfa/enroll", headers=ctx["headers"])  # pending only
    r = await client.post(
        "/auth/login", json={"email": ctx["email"], "password": ctx["password"]}
    )
    assert r.status_code == 200, r.text


async def test_active_mfa_gates_login(client):
    ctx = await _register_and_login(client)
    secret = await _enroll_and_activate(client, ctx)

    missing = await client.post(
        "/auth/login", json={"email": ctx["email"], "password": ctx["password"]}
    )
    assert missing.status_code == 401
    assert missing.json()["detail"] == "TOTP code required"

    wrong = await client.post(
        "/auth/login",
        json={"email": ctx["email"], "password": ctx["password"], "totp_code": pyotp.TOTP(secret).at(0)},
    )
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "Invalid TOTP code"

    ok = await client.post(
        "/auth/login",
        json={"email": ctx["email"], "password": ctx["password"], "totp_code": pyotp.TOTP(secret).now()},
    )
    assert ok.status_code == 200, ok.text


async def test_wrong_password_wins_over_totp_disclosure(client):
    """A caller who has NOT proved the password must get the generic
    credentials 401, never the TOTP-required detail — MFA status is only
    disclosed past the password check."""
    ctx = await _register_and_login(client)
    await _enroll_and_activate(client, ctx)
    r = await client.post(
        "/auth/login", json={"email": ctx["email"], "password": "wrong-password-1"}
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password"


async def test_totp_replay_is_refused_at_login(client):
    ctx = await _register_and_login(client)
    secret = await _enroll_and_activate(client, ctx)
    code = pyotp.TOTP(secret).now()
    first = await client.post(
        "/auth/login",
        json={"email": ctx["email"], "password": ctx["password"], "totp_code": code},
    )
    assert first.status_code == 200, first.text
    replay = await client.post(
        "/auth/login",
        json={"email": ctx["email"], "password": ctx["password"], "totp_code": code},
    )
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Invalid TOTP code"


async def test_refresh_needs_no_totp_mid_session(client):
    ctx = await _register_and_login(client)
    secret = await _enroll_and_activate(client, ctx)
    login = await client.post(
        "/auth/login",
        json={"email": ctx["email"], "password": ctx["password"], "totp_code": pyotp.TOTP(secret).now()},
    )
    r = await client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert r.status_code == 200, r.text
    # Task 7.4 review: mfa_enrollment_required is computed independently at
    # refresh (a fresh User select, not carried from login) — assert it
    # here too, not just at /auth/login, so a regression in that second
    # computation (wrong role check, stale value) is actually caught.
    assert r.json()["mfa_enrollment_required"] is False  # registration's admin, already active


async def test_mfa_enrollment_required_signal(client):
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)  # registration creates an ADMIN
    assert ctx["login"]["mfa_enrollment_required"] is True
    secret = await _enroll_and_activate(client, ctx)
    relogin = await client.post(
        "/auth/login",
        json={"email": ctx["email"], "password": ctx["password"], "totp_code": pyotp.TOTP(secret).now()},
    )
    assert relogin.json()["mfa_enrollment_required"] is False

    # Non-admin, MFA-less: the nudge is admin-only (spec Decision 3). Role
    # edited via owner DSN (same direct-DB setup precedent as
    # set_subscription_tier), on a SECOND fresh user so the assertions
    # above stay untouched.
    ctx2 = await _register_and_login(client)
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        result = await conn.execute(
            "UPDATE company_users SET role = 'accountant' WHERE user_id = $1",
            uuid.UUID(ctx2["user_id"]),
        )
        assert result == "UPDATE 1", result
    finally:
        await conn.close()
    nonadmin = await client.post(
        "/auth/login", json={"email": ctx2["email"], "password": ctx2["password"]}
    )
    assert nonadmin.status_code == 200, nonadmin.text
    assert nonadmin.json()["mfa_enrollment_required"] is False
