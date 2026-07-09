import uuid

import asyncpg
import pytest
from fastapi import FastAPI, Depends
from httpx import AsyncClient, ASGITransport

from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.middleware import TenantMiddleware
from app.core.security import decode_access_token
from app.models import Company
from tests.conftest import TEST_DATABASE_URL

ADMIN_CONN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


def uuid_from(value: str) -> uuid.UUID:
    return uuid.UUID(value)


def uuid_from_token_sub(access_token: str) -> uuid.UUID:
    return uuid.UUID(decode_access_token(access_token)["sub"])


test_app = FastAPI()
test_app.add_middleware(TenantMiddleware)


@test_app.get("/whoami")
async def whoami(current: CurrentUser = Depends(get_current_user)):
    return {"user_id": str(current.user.id), "company_id": str(current.company_id), "role": current.role}


@test_app.get("/admin-only")
async def admin_only(current: CurrentUser = Depends(require_role("admin"))):
    return {"ok": True}


@test_app.post("/whoami-then-raise")
async def whoami_then_raise(current: CurrentUser = Depends(get_current_user)):
    # Uses the still-open, tenant-scoped session to make a real write, then
    # raises — this is the exact shape design decision #8 exists to protect:
    # get_current_user must roll the write back, not commit it, when the
    # route handler fails after yield.
    await current.session.execute(
        Company.__table__.update().where(Company.id == current.company_id).values(name="MUTATED")
    )
    raise RuntimeError("simulated route handler failure")


async def _register_and_login(client, email="ada@acme.test"):
    await client.post(
        "/auth/register",
        json={
            "company_name": "Acme Construction",
            "admin_full_name": "Ada Lovelace",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return login.json()


async def test_get_current_user_resolves_default_company(client):
    token_body = await _register_and_login(client)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as whoami_client:
        response = await whoami_client.get(
            "/whoami", headers={"Authorization": f"Bearer {token_body['access_token']}"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["company_id"] == token_body["default_company_id"]
    assert body["role"] == "admin"


async def test_get_current_user_rejects_missing_token():
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as whoami_client:
        response = await whoami_client.get("/whoami")
    assert response.status_code == 401


async def test_get_current_user_rejects_unauthorized_tenant_header(client):
    token_body = await _register_and_login(client)
    fake_company_id = "00000000-0000-0000-0000-000000000000"

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as whoami_client:
        response = await whoami_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token_body['access_token']}",
                "X-Tenant-ID": fake_company_id,
            },
        )
    assert response.status_code == 403


async def test_require_role_allows_admin(client):
    admin_token = await _register_and_login(client, email="admin@acme.test")

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as admin_client:
        ok = await admin_client.get("/admin-only", headers={"Authorization": f"Bearer {admin_token['access_token']}"})
    assert ok.status_code == 200


async def test_require_role_blocks_non_admin_role(client):
    """register() always creates an admin (Task 14's invitation flow is the
    only route that will ever create a non-admin membership, and it doesn't
    exist yet), so this directly inserts a second, non-admin membership via
    the owner connection — bypassing RLS the same way conftest's
    _clean_tables fixture does — to actually exercise require_role's denial
    branch, which test_require_role_allows_admin never could."""
    owner_token = await _register_and_login(client, email="owner@acme.test")
    member_token = await _register_and_login(client, email="member@other.test")

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        await conn.execute(
            "INSERT INTO company_users (company_id, user_id, role) VALUES ($1, $2, 'field_crew')",
            uuid_from(owner_token["default_company_id"]),
            uuid_from_token_sub(member_token["access_token"]),
        )
    finally:
        await conn.close()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as member_client:
        blocked = await member_client.get(
            "/admin-only",
            headers={
                "Authorization": f"Bearer {member_token['access_token']}",
                "X-Tenant-ID": owner_token["default_company_id"],
            },
        )
    assert blocked.status_code == 403


async def test_get_current_user_rejects_malformed_tenant_header(client):
    token_body = await _register_and_login(client)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as whoami_client:
        response = await whoami_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token_body['access_token']}",
                "X-Tenant-ID": "not-a-uuid",
            },
        )
    assert response.status_code == 400


async def test_get_current_user_rejects_invalid_token():
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as whoami_client:
        response = await whoami_client.get("/whoami", headers={"Authorization": "Bearer garbage.not.a.jwt"})
    assert response.status_code == 401


async def test_get_current_user_rejects_deleted_user(client):
    token_body = await _register_and_login(client)

    deleted_user_id = uuid_from_token_sub(token_body["access_token"])
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        await conn.execute("DELETE FROM company_users WHERE user_id = $1", deleted_user_id)
        # audit_log.actor_id has no ondelete (RESTRICT — 7-year retention
        # policy, design decision #1), so register()'s own audit entry must
        # go first or this FK blocks the delete.
        await conn.execute("DELETE FROM audit_log WHERE actor_id = $1", deleted_user_id)
        await conn.execute("DELETE FROM users WHERE id = $1", deleted_user_id)
    finally:
        await conn.close()

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as whoami_client:
        response = await whoami_client.get(
            "/whoami", headers={"Authorization": f"Bearer {token_body['access_token']}"}
        )
    assert response.status_code == 401


async def test_get_current_user_rolls_back_when_route_handler_raises(client):
    """Regression test for design decision #8: get_current_user must roll
    back, not commit, a write made by a route handler that fails after
    yield. Without the yield-based restructuring this test would either
    persist "MUTATED" (if the naive eager-commit-before-yield version were
    reintroduced) or hang (if the commit were simply deleted without the
    try/except/finally around yield)."""
    token_body = await _register_and_login(client)

    # httpx's ASGITransport re-raises app exceptions by default (matching
    # Starlette's ServerErrorMiddleware, which always re-raises after
    # sending its 500 response) rather than returning a 500 Response — so
    # the failure mode under test is an exception here, not a status code.
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as raising_client:
        with pytest.raises(RuntimeError, match="simulated route handler failure"):
            await raising_client.post(
                "/whoami-then-raise", headers={"Authorization": f"Bearer {token_body['access_token']}"}
            )

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        name = await conn.fetchval(
            "SELECT name FROM companies WHERE id = $1", uuid_from(token_body["default_company_id"])
        )
    finally:
        await conn.close()
    assert name != "MUTATED"
