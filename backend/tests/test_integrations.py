"""Task 4.7 (design spec Section 3): GET /integrations/{provider}/connect."""
import uuid

import asyncpg

from app.services.integration_oauth_state import sign_oauth_state
from tests.conftest import TEST_DATABASE_URL, set_subscription_tier


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
    # Tier gating (Task 5.6): integrations is Enterprise-gated;
    # registration can only produce trialing/pro.
    await set_subscription_tier(register.json()["company_id"], "enterprise")
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


async def _connect(client, headers, company_id, provider="quickbooks"):
    state = sign_oauth_state(company_id=company_id, provider=provider)
    response = await client.get(f"/integrations/{provider}/callback?code=fake-code&state={state}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_sync_status_404s_for_a_provider_with_no_connection(client):
    admin = await _register_and_login(client, "Integ Co 7", "integ-7@example.test")

    response = await client.get("/integrations/quickbooks/sync-status", headers=admin["headers"])
    assert response.status_code == 404


async def test_sync_status_returns_the_connection_summary_with_an_empty_records_list(client):
    admin = await _register_and_login(client, "Integ Co 8", "integ-8@example.test")
    await _connect(client, admin["headers"], admin["company_id"])

    response = await client.get("/integrations/quickbooks/sync-status", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "quickbooks"
    assert body["records"] == []


async def test_project_manager_cannot_read_sync_status(client):
    admin = await _register_and_login(client, "Integ Co 9", "integ-9@example.test")
    await _connect(client, admin["headers"], admin["company_id"])
    invite = await client.post(
        "/invitations", json={"email": "pm-integ2@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-integ2@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.get("/integrations/quickbooks/sync-status", headers=pm_headers)
    assert response.status_code == 403


async def _insert_sync_record_directly(company_id, connection_id, **overrides):
    """Seeds a real integration_sync_records row via the RLS-exempt owner
    connection — sync_status's whole purpose is serializing rows like this
    one, and Tasks 4.11-4.13 (the actual writers) don't exist yet, so this
    is the only way to exercise the response-serialization path before
    then. Same rationale as test_tenant_isolation_phase1.py's
    _insert_lead_directly: this is test setup, not a runtime code path."""
    record_id = str(uuid.uuid4())
    fields = {
        "entity_type": "invoice",
        "entity_id": str(uuid.uuid4()),
        "status": "success",
    }
    fields.update(overrides)
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        await conn.execute(
            "INSERT INTO integration_sync_records "
            "(id, company_id, connection_id, entity_type, entity_id, status) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            record_id,
            company_id,
            connection_id,
            fields["entity_type"],
            fields["entity_id"],
            fields["status"],
        )
    finally:
        await conn.close()
    return record_id


async def test_sync_status_returns_a_seeded_sync_record(client):
    admin = await _register_and_login(client, "Integ Co 10", "integ-10@example.test")
    connection = await _connect(client, admin["headers"], admin["company_id"])
    record_id = await _insert_sync_record_directly(admin["company_id"], connection["id"])

    response = await client.get("/integrations/quickbooks/sync-status", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["records"]) == 1
    record = body["records"][0]
    assert record["id"] == record_id
    assert record["entity_type"] == "invoice"
    assert record["status"] == "success"


async def test_sync_status_does_not_500_when_a_child_branch_shares_the_same_provider(client):
    """Regression test: integration_connections is UNIQUE per (company_id,
    provider), not globally — a parent company and a child branch that
    each independently connect the same provider are two distinct rows
    RLS makes visible to the parent admin. Before this test was added,
    sync_status filtered its IntegrationConnection lookup by `provider`
    alone and called scalar_one_or_none(), which raises
    MultipleResultsFound (an unhandled 500) in exactly this situation."""
    parent = await _register_and_login(client, "Integ Co 11", "integ-11@example.test")
    await _connect(client, parent["headers"], parent["company_id"])

    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Integ Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        await conn.execute(
            "INSERT INTO integration_connections "
            "(id, company_id, provider, access_token_encrypted, refresh_token_encrypted) "
            "VALUES ($1, $2, 'quickbooks', 'x', 'y')",
            str(uuid.uuid4()),
            child_id,
        )
    finally:
        await conn.close()

    response = await client.get("/integrations/quickbooks/sync-status", headers=parent["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "quickbooks"
