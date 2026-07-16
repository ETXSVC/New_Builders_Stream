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


async def test_login_returns_a_refresh_token(client):
    ctx = await _register_and_login(client)
    body = ctx["login"]
    assert "refresh_token" in body, body
    assert isinstance(body["refresh_token"], str) and len(body["refresh_token"]) >= 32
    assert body["refresh_token"] != body["access_token"]


async def test_stored_refresh_token_is_hashed_not_plaintext(client):
    """Owner-DSN check (same direct-DB test-setup precedent as
    set_subscription_tier): the DB row holds a 64-char hex SHA-256, never
    the presentable secret."""
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)
    secret = ctx["login"]["refresh_token"]
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        rows = await conn.fetch(
            "SELECT token_hash FROM refresh_tokens WHERE user_id = $1",
            uuid.UUID(ctx["user_id"]),
        )
    finally:
        await conn.close()
    assert len(rows) == 1
    stored = rows[0]["token_hash"]
    assert len(stored) == 64 and all(c in "0123456789abcdef" for c in stored)
    assert stored != secret


async def _refresh(client, refresh_token: str):
    return await client.post("/auth/refresh", json={"refresh_token": refresh_token})


async def test_refresh_rotates_and_rederives_company(client):
    ctx = await _register_and_login(client)
    first = ctx["login"]
    r = await _refresh(client, first["refresh_token"])
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    assert second["default_company_id"] == str(first["default_company_id"]) or second[
        "default_company_id"
    ] == first["default_company_id"]

    # the rotated-away token is spent: a second use is refused
    dead = await _refresh(client, first["refresh_token"])
    assert dead.status_code == 401
    assert dead.json()["detail"] == "Invalid refresh token"


async def test_rotation_chain_each_token_works_exactly_once(client):
    ctx = await _register_and_login(client)
    tokens = [ctx["login"]["refresh_token"]]
    for _ in range(2):
        r = await _refresh(client, tokens[-1])
        assert r.status_code == 200, r.text
        tokens.append(r.json()["refresh_token"])
    assert len(set(tokens)) == 3


async def test_reuse_of_a_spent_token_kills_the_whole_family(client):
    """The reuse-detection core: after A -> B rotation, presenting A again
    must 401 AND kill B (the legitimate successor) — and the containment
    must SURVIVE the 401 (i.e. the family revocation commits even though
    the request errors). Also asserts the audit row."""
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)
    token_a = ctx["login"]["refresh_token"]
    token_b = (await _refresh(client, token_a)).json()["refresh_token"]

    async def _reuse_audit_count() -> int:
        conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
        try:
            audit = await conn.fetch(
                "SELECT action FROM audit_log WHERE action = 'auth.refresh_reuse_detected'"
            )
        finally:
            await conn.close()
        return len(audit)

    reused = await _refresh(client, token_a)
    assert reused.status_code == 401
    assert reused.json()["detail"] == "Invalid refresh token"
    assert await _reuse_audit_count() == 1, "reuse of A must write exactly one audit row"

    survivor = await _refresh(client, token_b)
    assert survivor.status_code == 401, (
        "the legitimate successor must be dead after reuse detection: "
        f"{survivor.status_code}: {survivor.text}"
    )
    # Presenting B — now revoked by the family kill — is ITSELF a reuse
    # event per the service's rule ("already rotated or revoked"), so it
    # writes a second audit row. The spec's service contract says exactly
    # that ("on a row that is already revoked or rotated ... emits the
    # reuse audit row"); the plan snippet's expected count of 1 predated
    # tracing that through this test's own second presentation.
    assert await _reuse_audit_count() == 2, "presenting dead B is a second audited reuse event"


async def test_concurrent_refresh_of_same_token_one_wins_then_family_dies(client):
    """Race coverage promised by commit e956459: rotate_refresh_token's
    SELECT ... FOR UPDATE serializes two simultaneous rotations of the SAME
    token. Whichever request takes the row lock first rotates and commits;
    the other blocks on the lock, then reads the committed revoked_at and
    deterministically takes the reuse branch — so the status multiset is
    exactly {200, 401}, never {200, 200} (the lost-update attack the lock
    exists to prevent). And because the loser's reuse branch revokes the
    WHOLE family, the winner's freshly minted successor dies with it: the
    net effect of a raced refresh is full-family containment."""
    import asyncio

    ctx = await _register_and_login(client)
    token = ctx["login"]["refresh_token"]

    r1, r2 = await asyncio.gather(_refresh(client, token), _refresh(client, token))
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 401], (r1.status_code, r1.text, r2.status_code, r2.text)

    winner = r1 if r1.status_code == 200 else r2
    successor = winner.json()["refresh_token"]
    after = await _refresh(client, successor)
    assert after.status_code == 401, (
        "the loser's reuse detection must have revoked the whole family, "
        f"including the winner's successor: {after.status_code}: {after.text}"
    )


async def test_expired_refresh_token_is_refused(client):
    """Back-date expires_at via owner DSN (same direct-DB setup precedent
    as set_subscription_tier)."""
    import asyncpg

    from tests.conftest import TEST_DATABASE_URL

    ctx = await _register_and_login(client)
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        result = await conn.execute(
            "UPDATE refresh_tokens SET expires_at = now() - interval '1 day' WHERE user_id = $1",
            uuid.UUID(ctx["user_id"]),
        )
        assert result == "UPDATE 1", result
    finally:
        await conn.close()
    r = await _refresh(client, ctx["login"]["refresh_token"])
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid refresh token"


async def test_garbage_refresh_token_is_refused(client):
    r = await _refresh(client, "not-a-real-token")
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid refresh token"


async def test_token_responses_are_cache_control_no_store(client):
    """RFC 6749 §5.1: any response carrying tokens must be sent with
    Cache-Control: no-store (quality-review follow-up). Both token-returning
    routes — login and refresh — set it on their success responses."""
    ctx = await _register_and_login(client)
    login = await client.post(
        "/auth/login", json={"email": ctx["email"], "password": ctx["password"]}
    )
    assert login.status_code == 200, login.text
    assert login.headers.get("Cache-Control") == "no-store"

    r = await _refresh(client, login.json()["refresh_token"])
    assert r.status_code == 200, r.text
    assert r.headers.get("Cache-Control") == "no-store"
