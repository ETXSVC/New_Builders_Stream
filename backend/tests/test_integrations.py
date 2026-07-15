"""Task 4.7 (design spec Section 3): GET /integrations/{provider}/connect."""


async def _register_and_login(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def test_connect_returns_a_fake_authorization_url(client):
    admin = await _register_and_login(client, "Integ Co 1", "integ-1@example.test")

    response = await client.get("/integrations/quickbooks/connect", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorization_url"].startswith("https://quickbooks.fake-oauth.test/")
    assert "state=" in body["authorization_url"]


async def test_connect_rejects_an_unknown_provider(client):
    admin = await _register_and_login(client, "Integ Co 2", "integ-2@example.test")

    response = await client.get("/integrations/xero/connect", headers=admin["headers"])
    assert response.status_code == 422


async def test_project_manager_cannot_connect(client):
    admin = await _register_and_login(client, "Integ Co 3", "integ-3@example.test")
    invite = await client.post(
        "/invitations", json={"email": "pm-integ@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-integ@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.get("/integrations/quickbooks/connect", headers=pm_headers)
    assert response.status_code == 403


import asyncpg

from app.services.integration_oauth_state import sign_oauth_state
from tests.conftest import TEST_DATABASE_URL

ADMIN_CONN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


async def test_callback_with_a_validly_signed_state_creates_the_connection(client):
    admin = await _register_and_login(client, "Integ Co 4", "integ-4@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")

    response = await client.get(
        f"/integrations/quickbooks/callback?code=fake-code&state={state}"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "quickbooks"

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT provider, access_token_encrypted, refresh_token_encrypted "
            "FROM integration_connections WHERE company_id = $1",
            admin["company_id"],
        )
    finally:
        await conn.close()
    assert row["provider"] == "quickbooks"
    # Never plaintext at rest — docs/07-security-compliance.md Section 4.
    assert "access_fake_" not in row["access_token_encrypted"]
    assert "refresh_fake_" not in row["refresh_token_encrypted"]


async def test_callback_writes_an_audit_log_entry(client):
    admin = await _register_and_login(client, "Integ Co 5", "integ-5@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")

    await client.get(f"/integrations/quickbooks/callback?code=fake-code&state={state}")

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        rows = await conn.fetch(
            "SELECT action FROM audit_log WHERE company_id = $1", admin["company_id"]
        )
    finally:
        await conn.close()
    actions = {row["action"] for row in rows}
    assert "integration.connected" in actions


async def test_callback_with_an_invalid_state_returns_400(client):
    response = await client.get(
        "/integrations/quickbooks/callback?code=fake-code&state=not-a-real-token"
    )
    assert response.status_code == 400


async def test_callback_rejects_a_state_signed_for_a_different_provider(client):
    admin = await _register_and_login(client, "Integ Co 7", "integ-7@example.test")
    # state is validly signed, just for the wrong provider — proves the
    # state_provider != provider check is load-bearing, not redundant with
    # signature verification alone.
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")

    response = await client.get(
        f"/integrations/freshbooks/callback?code=fake-code&state={state}"
    )
    assert response.status_code == 400


async def test_reconnecting_the_same_provider_replaces_the_old_tokens(client):
    admin = await _register_and_login(client, "Integ Co 6", "integ-6@example.test")
    state_1 = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    await client.get(f"/integrations/quickbooks/callback?code=code-1&state={state_1}")

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        first_token = await conn.fetchval(
            "SELECT access_token_encrypted FROM integration_connections WHERE company_id = $1",
            admin["company_id"],
        )
    finally:
        await conn.close()

    state_2 = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    second = await client.get(f"/integrations/quickbooks/callback?code=code-2&state={state_2}")
    assert second.status_code == 200, second.text

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        rows = await conn.fetch(
            "SELECT id, access_token_encrypted FROM integration_connections WHERE company_id = $1",
            admin["company_id"],
        )
    finally:
        await conn.close()
    assert len(rows) == 1, "reconnecting must update the existing row, not insert a second one"
    assert rows[0]["access_token_encrypted"] != first_token
