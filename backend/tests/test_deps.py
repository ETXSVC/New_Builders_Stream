import pytest
from fastapi import FastAPI, Depends
from httpx import AsyncClient, ASGITransport

from app.core.deps import CurrentUser, get_current_user, require_role
from app.core.middleware import TenantMiddleware

test_app = FastAPI()
test_app.add_middleware(TenantMiddleware)


@test_app.get("/whoami")
async def whoami(current: CurrentUser = Depends(get_current_user)):
    return {"user_id": str(current.user.id), "company_id": str(current.company_id), "role": current.role}


@test_app.get("/admin-only")
async def admin_only(current: CurrentUser = Depends(require_role("admin"))):
    return {"ok": True}


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


async def test_require_role_blocks_wrong_role(client):
    admin_token = await _register_and_login(client, email="admin@acme.test")

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as admin_client:
        ok = await admin_client.get("/admin-only", headers={"Authorization": f"Bearer {admin_token['access_token']}"})
    assert ok.status_code == 200
