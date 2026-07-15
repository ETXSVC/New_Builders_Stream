"""AR/AP tenant-isolation regression tests (design spec Section 9). AR half
first (Task 3.40); AP half appended by Task 3.43."""
import asyncpg

from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL

# Naming matches test_tenant_isolation_phase3.py's own convention exactly:
# OWNER_DSN (table-owner/superuser, used for direct membership inserts and
# RLS disable/re-enable) vs. APP_CONN_DSN (the app_user role RLS policies
# actually apply to). Deliberately not "ADMIN_CONN_DSN" — this codebase
# already uses "admin" for the company_users.role value, and reusing it here
# for an unrelated DB-connection-privilege concept would be confusing.
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
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _add_membership_directly(user_id, company_id, role):
    """Grants an existing user a real company_users row in a company they
    neither registered nor were invited into. There is no legitimate API path
    for this — creating a child company via POST /companies/{id}/children
    does NOT itself grant the creating admin membership in the new child (see
    app/routers/companies.py's create_child_company and app/core/deps.py's
    get_current_user membership check) — so an X-Tenant-ID switch into a
    freshly created child 403s ("Not a member of this company") without this.
    Same rationale, and identical pattern, as every other phase's own
    _add_membership_directly helper (e.g. test_tenant_isolation_phase3.py's)."""
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
    """Creates a real child branch via the actual API route, then grants the
    parent admin membership in it directly, so the SAME admin token can act
    as either company via X-Tenant-ID. Identical to every other phase's own
    helper of the same name."""
    create = await client.post(
        f"/companies/{parent['company_id']}/children", json={"name": name}, headers=parent["headers"]
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


async def _create_project(client, headers, name="Iso Project"):
    response = await client.post(
        "/projects", json={"name": name, "site_address": "1 Main St"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_invoice(client, headers, project_id, amount="500.00"):
    response = await client.post(
        f"/projects/{project_id}/invoices", json={"amount": amount}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked_for_invoices(client):
    company_a = await _register_and_login(client, "Iso AR Co A", "iso-ar-a@example.test")
    company_b = await _register_and_login(client, "Iso AR Co B", "iso-ar-b@example.test")
    project_b = await _create_project(client, company_b["headers"])

    spoofed_headers = {**company_a["headers"], "X-Tenant-ID": company_b["company_id"]}
    response = await client.post(
        f"/projects/{project_b['id']}/invoices", json={"amount": "100.00"}, headers=spoofed_headers
    )
    assert response.status_code == 403


async def test_invoice_get_404s_across_tenants_at_the_application_layer(client):
    """Confirms `GET /invoices/{id}` itself 404s for a genuinely unrelated
    tenant (app/routers/invoices.py's `_get_invoice_or_404` has no explicit
    company_id filter of its own — RLS is the only thing narrowing this
    query). The POLICY-level proof (that RLS itself, not app-layer luck, is
    responsible) is the separate
    test_rls_policy_itself_blocks_cross_tenant_invoice_visibility test below,
    same "one HTTP-level proof + one policy-level proof" split every other
    tenant-isolation file in this codebase uses."""
    company_a = await _register_and_login(client, "Iso AR Co C", "iso-ar-c@example.test")
    company_b = await _register_and_login(client, "Iso AR Co D", "iso-ar-d@example.test")
    project_b = await _create_project(client, company_b["headers"])
    invoice_b = await _create_invoice(client, company_b["headers"], project_b["id"])

    response = await client.get(f"/invoices/{invoice_b['id']}", headers=company_a["headers"])
    assert response.status_code == 404


async def test_rls_policy_itself_blocks_cross_tenant_invoice_visibility(client):
    """Mirrors test_tenant_isolation_phase3.py's own
    test_rls_policy_itself_blocks_cross_tenant_subcontractor_visibility
    exactly, adapted to `invoices` — connects as app_user directly
    (bypassing the FastAPI app, and therefore `_get_invoice_or_404`
    entirely) to prove the POLICY itself, not application-layer filtering,
    blocks a genuinely unrelated tenant from seeing another tenant's invoice
    row. Then disables RLS as the table owner and confirms the identical
    query starts returning the row, showing the policy, not luck, was
    responsible. Then ALWAYS restores RLS in a finally, even if an assertion
    above fails partway through, so this test can never leave the database
    in an insecure state for any test that runs after it — same two-level
    try/finally discipline as every other RLS-disable/re-enable proof in
    this codebase."""
    a = await _register_and_login(client, "Iso AR Co E", "iso-ar-e@example.test")
    b = await _register_and_login(client, "Iso AR Co F", "iso-ar-f@example.test")
    project_b = await _create_project(client, b["headers"])
    invoice_b = await _create_invoice(client, b["headers"], project_b["id"])
    invoice_b_id = invoice_b["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        # set_config(), not `SET app.current_tenant = $1` — see
        # set_current_tenant's docstring in app/db.py for why a bound
        # parameter there is a syntax error.
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
        )
        visible_as_a = await app_conn.fetchrow(
            "SELECT id FROM invoices WHERE id = $1", invoice_b_id
        )
        assert visible_as_a is None, (
            "RLS should block Company A's session from seeing Company B's invoice"
        )
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        await owner_conn.execute("ALTER TABLE invoices DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(APP_CONN_DSN)
        try:
            await app_conn2.execute(
                "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
            )
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM invoices WHERE id = $1", invoice_b_id
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's invoice row should exist "
                "and be visible once RLS is off — if this fails, the row "
                "itself is missing, which means the test setup (not the "
                "policy) is broken."
            )
        finally:
            await app_conn2.close()
    finally:
        # ALWAYS restore RLS even if the assertion above fails — see this
        # test's own docstring for why this is a separate try/finally.
        try:
            await owner_conn.execute("ALTER TABLE invoices ENABLE ROW LEVEL SECURITY")
        finally:
            await owner_conn.close()


async def test_parent_admin_can_see_child_branch_invoice(client):
    parent = await _register_and_login(client, "Iso AR Parent", "iso-ar-parent@example.test")
    child_id = await _create_child_with_membership(client, parent, "Branch")

    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}
    project = await _create_project(client, child_headers)
    invoice = await _create_invoice(client, child_headers, project["id"])

    response = await client.get(
        f"/invoices/{invoice['id']}", headers={**parent["headers"], "X-Tenant-ID": child_id}
    )
    assert response.status_code == 200, response.text


async def test_sibling_branches_cannot_see_each_others_invoices(client):
    """Checked symmetrically in both directions — same convention
    test_tenant_isolation_phase3.py's own sibling-branch tests use (e.g.
    test_sibling_branches_cannot_see_each_others_subcontractor_or_compliance_document):
    a single one-way check (A can't see B's) wouldn't rule out a bug that
    only manifests checking the reverse direction (B can't see A's)."""
    parent = await _register_and_login(client, "Iso AR Parent 2", "iso-ar-parent2@example.test")
    child_a_id = await _create_child_with_membership(client, parent, "Branch A")
    child_b_id = await _create_child_with_membership(client, parent, "Branch B")

    headers_a = {**parent["headers"], "X-Tenant-ID": child_a_id}
    headers_b = {**parent["headers"], "X-Tenant-ID": child_b_id}
    project_a = await _create_project(client, headers_a, name="Branch A Project")
    invoice_a = await _create_invoice(client, headers_a, project_a["id"])
    project_b = await _create_project(client, headers_b, name="Branch B Project")
    invoice_b = await _create_invoice(client, headers_b, project_b["id"])

    response_b_sees_a = await client.get(f"/invoices/{invoice_a['id']}", headers=headers_b)
    assert response_b_sees_a.status_code == 404

    response_a_sees_b = await client.get(f"/invoices/{invoice_b['id']}", headers=headers_a)
    assert response_a_sees_b.status_code == 404
