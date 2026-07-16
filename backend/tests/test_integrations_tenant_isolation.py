"""Integrations tenant-isolation regression tests (design spec Section 7).
Mirrors test_invoicing_ap_tenant_isolation.py's exact structure."""
import uuid

import asyncpg

from app.services.integration_oauth_state import sign_oauth_state
from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL, set_subscription_tier

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")
APP_CONN_DSN = TEST_APP_DATABASE_URL.replace("+asyncpg", "")


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
    # registration can only produce trialing/pro. Child-branch flows get
    # their enterprise tier from this same bump (the subscription belongs
    # to the root).
    await set_subscription_tier(register.json()["company_id"], "enterprise")
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _add_membership_directly(user_id, company_id, role):
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO company_users (company_id, user_id, role, created_at) "
            "VALUES ($1, $2, $3, now())",
            company_id,
            user_id,
            role,
        )
    finally:
        await conn.close()


async def _create_child_with_membership(client, parent, name, role="admin"):
    create = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": name}, headers=parent["headers"]
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


async def _connect(client, headers, company_id, provider="quickbooks"):
    state = sign_oauth_state(company_id=company_id, provider=provider)
    response = await client.get(f"/integrations/{provider}/callback?code=fake&state={state}")
    assert response.status_code == 200, response.text
    return response.json()


async def test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked_for_connect(client):
    company_a = await _register_and_login(client, "Iso Integ Co A", "iso-integ-a@example.test")
    company_b = await _register_and_login(client, "Iso Integ Co B", "iso-integ-b@example.test")

    spoofed_headers = {**company_a["headers"], "X-Tenant-ID": company_b["company_id"]}
    response = await client.get("/integrations/quickbooks/connect", headers=spoofed_headers)
    assert response.status_code == 403


async def test_sync_status_404s_across_tenants_at_the_application_layer(client):
    company_a = await _register_and_login(client, "Iso Integ Co C", "iso-integ-c@example.test")
    company_b = await _register_and_login(client, "Iso Integ Co D", "iso-integ-d@example.test")
    await _connect(client, company_b["headers"], company_b["company_id"])

    response = await client.get("/integrations/quickbooks/sync-status", headers=company_a["headers"])
    assert response.status_code == 404


async def test_rls_policy_itself_blocks_cross_tenant_connection_visibility(client):
    """Connects as app_user directly (bypassing the FastAPI app entirely) to
    prove the POLICY, not app-layer filtering, blocks cross-tenant access to
    an integration_connections row. Then disables RLS as the table owner and
    confirms the identical query starts returning the row — showing the
    policy, not luck (missing row, missing grant, wrong DSN), was
    responsible. The two-level try/finally ALWAYS restores RLS, even if an
    assertion above fails partway through, so this test can never leave the
    database in an insecure state for any test that runs after it — same
    discipline as test_invoicing_ap_tenant_isolation.py's equivalent."""
    a = await _register_and_login(client, "Iso Integ Co E", "iso-integ-e@example.test")
    b = await _register_and_login(client, "Iso Integ Co F", "iso-integ-f@example.test")
    connection_b = await _connect(client, b["headers"], b["company_id"])
    connection_b_id = connection_b["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
        )
        visible_as_a = await app_conn.fetchrow(
            "SELECT id FROM integration_connections WHERE id = $1", uuid.UUID(connection_b_id)
        )
        assert visible_as_a is None, (
            "RLS should block Company A's session from seeing Company B's integration connection"
        )
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        await owner_conn.execute("ALTER TABLE integration_connections DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(APP_CONN_DSN)
        try:
            await app_conn2.execute(
                "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
            )
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM integration_connections WHERE id = $1", uuid.UUID(connection_b_id)
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's connection row should exist and be visible "
                "once RLS is off — if this fails, the row itself is missing, which means the "
                "test setup (not the policy) is broken."
            )
        finally:
            await app_conn2.close()
    finally:
        try:
            await owner_conn.execute("ALTER TABLE integration_connections ENABLE ROW LEVEL SECURITY")
        finally:
            await owner_conn.close()


async def test_parent_admin_can_see_child_branch_connection(client):
    parent = await _register_and_login(client, "Iso Integ Parent", "iso-integ-parent@example.test")
    child_id = await _create_child_with_membership(client, parent, "Integ Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}

    await _connect(client, child_headers, child_id)

    response = await client.get(
        "/integrations/quickbooks/sync-status", headers={**parent["headers"], "X-Tenant-ID": child_id}
    )
    assert response.status_code == 200, response.text


async def test_sibling_branches_cannot_see_each_others_connections(client):
    """Checked symmetrically in both directions — a single one-way check
    wouldn't rule out a bug that only manifests checking the reverse
    direction.

    sync-status keys off the CALLER's own company_id, not a specific
    connection id in the URL, so the 404-based proof the AR/AP file uses
    (GET the sibling's row by id) has no direct equivalent here. Instead,
    each branch connects a DIFFERENT provider — Branch A quickbooks only,
    Branch B freshbooks only — so a branch asking sync-status for the
    provider only its SIBLING connected must 404: the only way it could
    200 is by resolving the sibling's connection row as its own. The
    positive-control 200s at the end prove the 404s reflect isolation,
    not a broken connect flow."""
    parent = await _register_and_login(client, "Iso Integ Parent 2", "iso-integ-parent2@example.test")
    child_a_id = await _create_child_with_membership(client, parent, "Integ Branch A")
    child_b_id = await _create_child_with_membership(client, parent, "Integ Branch B")
    headers_a = {**parent["headers"], "X-Tenant-ID": child_a_id}
    headers_b = {**parent["headers"], "X-Tenant-ID": child_b_id}

    await _connect(client, headers_a, child_a_id, provider="quickbooks")
    await _connect(client, headers_b, child_b_id, provider="freshbooks")

    response_b_sees_a = await client.get("/integrations/quickbooks/sync-status", headers=headers_b)
    assert response_b_sees_a.status_code == 404, (
        "Branch B has no quickbooks connection of its own — a 200 here could only "
        "mean it resolved Branch A's quickbooks connection as its own"
    )
    response_a_sees_b = await client.get("/integrations/freshbooks/sync-status", headers=headers_a)
    assert response_a_sees_b.status_code == 404, (
        "Branch A has no freshbooks connection of its own — a 200 here could only "
        "mean it resolved Branch B's freshbooks connection as its own"
    )

    # Positive controls: each branch DOES see the provider it itself connected.
    response_a_own = await client.get("/integrations/quickbooks/sync-status", headers=headers_a)
    assert response_a_own.status_code == 200, response_a_own.text
    assert response_a_own.json()["provider"] == "quickbooks"
    response_b_own = await client.get("/integrations/freshbooks/sync-status", headers=headers_b)
    assert response_b_own.status_code == 200, response_b_own.text
    assert response_b_own.json()["provider"] == "freshbooks"
