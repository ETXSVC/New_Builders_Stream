"""MFA/TOTP (docs/superpowers/specs/2026-07-16-mfa-totp-design.md).

Covers: enroll/activate (Task 7.3), the login challenge +
mfa_enrollment_required signal (7.4), disable + change-password
hardening (7.5). One file for the feature, real pyotp codes throughout.
"""
import uuid

import pyotp

from tests.test_auth_token_lifecycle import _register_and_login


async def _enroll_and_activate(client, ctx) -> str:
    """Enrolls + activates MFA for ctx's user; returns the base32 secret."""
    enroll = await client.post("/auth/mfa/enroll", headers=ctx["headers"])
    assert enroll.status_code == 200, enroll.text
    secret = enroll.json()["secret"]
    activate = await client.post(
        "/auth/mfa/activate",
        json={"totp_code": pyotp.TOTP(secret).now()},
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
