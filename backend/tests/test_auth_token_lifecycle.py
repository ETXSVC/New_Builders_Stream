"""Auth token lifecycle (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md).

Covers: access-token lifetime honoring settings (Task 6.1), refresh-token
issue/rotation/reuse-detection (Tasks 6.4-6.5), logout (6.6), and
change-password revoke-all (6.7). One file for the whole feature, same
convention as test_tier_gating.py.
"""
import uuid

import jwt as pyjwt


def _register_payload():
    uid = uuid.uuid4().hex[:8]
    return {
        "company_name": f"TokenCo {uid}",
        "admin_full_name": "Toni Token",
        "admin_email": f"toni-{uid}@tokenco.test",
        "admin_password": "correct-horse-9",
    }


async def _register_and_login(client) -> dict:
    """Returns {"email", "password", "company_id", "user_id", "login": <login response json>}."""
    payload = _register_payload()
    register = await client.post("/auth/register", json=payload)
    assert register.status_code == 201, register.text
    login = await client.post(
        "/auth/login",
        json={"email": payload["admin_email"], "password": payload["admin_password"]},
    )
    assert login.status_code == 200, login.text
    return {
        "email": payload["admin_email"],
        "password": payload["admin_password"],
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "login": login.json(),
    }


async def test_access_token_lifetime_honors_settings(client):
    """exp - iat must equal settings.jwt_expire_minutes * 60 exactly — this
    guards the 15-minute production default without pinning the test to
    wall-clock time (conftest pins JWT_EXPIRE_MINUTES=60 for tests; the
    invariant under test is that whatever the setting says is what the
    token gets, which is the same code path the 15-minute default uses)."""
    from app.config import settings

    ctx = await _register_and_login(client)
    claims = pyjwt.decode(
        ctx["login"]["access_token"],
        options={"verify_signature": False},
    )
    assert claims["exp"] - claims["iat"] == settings.jwt_expire_minutes * 60
