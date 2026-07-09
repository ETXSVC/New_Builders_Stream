"""Task 1.8: CRM tenant-isolation regression tests.

New file rather than an extension of test_tenant_isolation.py: that file is
specifically about `companies` (registration, child-company creation,
company-hierarchy visibility) and its helper/fixture shape is built around
company_id-only payloads. This task covers `leads` and `communication_logs`
— a different module (app/routers/leads.py) with different fixture needs
(lead payloads, nested communication-log routes) — so a dedicated file
reads more cleanly than interleaving CRM-specific setup into the companies
file. It mirrors that file's and test_rls_policy_regression.py's structure
and rigor closely on purpose, per this task's spec.

Cross-tenant 404s for direct-ID access on both `leads` and
`communication_logs` are already covered by test_leads.py
(test_get_cross_tenant_lead_returns_404, test_list_leads_is_tenant_scoped)
and test_communication_logs.py (the *_under_cross_tenant_lead_returns_404
tests) from Tasks 1.4/1.7 — not duplicated here. This file adds the three
pieces of adversarial coverage nothing has exercised yet for the CRM
tables: a lead-scoped header-spoofing test, empirical parent/child
hierarchy visibility for `leads`, and — the main point of this task — an
RLS-disable/re-enable regression test proving the `leads` policy itself
(not app-layer filtering) is what blocks cross-tenant access, following
Phase 0 Task 16's exact pattern.
"""
import uuid

import asyncpg

from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL

APP_CONN_DSN = TEST_APP_DATABASE_URL.replace("+asyncpg", "")
OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


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
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    body = login.json()
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


def _lead_payload(**overrides):
    payload = {
        "contact_name": "Jane Homeowner",
        "project_name": "Kitchen Remodel",
        "email": "jane@example.test",
        "phone": "555-0100",
        "project_type": "residential",
        "estimated_value": "15000.00",
        "notes": "Prefers morning calls",
    }
    payload.update(overrides)
    return payload


async def _create_lead(client, headers, **overrides):
    response = await client.post("/leads", json=_lead_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_lead_directly(company_id, **overrides):
    """Seeds a lead row scoped to an arbitrary company_id via the RLS-exempt
    owner connection. There's no way to legitimately create a lead "as" a
    child-branch company through the API — the only account that exists is
    the parent admin's, whose active tenant context is always the parent's
    own company_id — so directly inserting through the owner connection
    (same rationale as conftest.py's _clean_tables fixture and
    test_leads.py's pagination fixtures: this is test setup, not a runtime
    code path) is the only way to get a real row under a child branch to
    test hierarchy visibility against."""
    lead_id = str(uuid.uuid4())
    fields = {
        "contact_name": "Branch Contact",
        "project_name": "Branch Remodel",
        "email": "branch@example.test",
        "project_type": "residential",
    }
    fields.update(overrides)
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO leads (id, company_id, contact_name, project_name, email, project_type) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            lead_id,
            company_id,
            fields["contact_name"],
            fields["project_name"],
            fields["email"],
            fields["project_type"],
        )
    finally:
        await conn.close()
    return lead_id


async def test_lead_header_spoofing_via_x_tenant_id_is_blocked(client):
    """Mirrors test_tenant_isolation.py's
    test_company_a_cannot_impersonate_company_b_via_header, but through a
    CRM route rather than /companies. The X-Tenant-ID membership check in
    app/core/deps.py is route-agnostic, so this is expected to already
    hold, but this task's spec calls for confirming it explicitly on a
    lead-scoped route, since /leads is a different router than
    /companies with its own dependency wiring."""
    a = await _register_and_login(client, "Company A", "spoof-lead-a@acme.test")
    b = await _register_and_login(client, "Company B", "spoof-lead-b@acme.test")
    lead_b = await _create_lead(client, b["headers"])

    response = await client.get(
        f"/leads/{lead_b['id']}",
        headers={**a["headers"], "X-Tenant-ID": b["company_id"]},
    )
    assert response.status_code == 403  # membership check rejects the spoofed claim


async def test_parent_admin_can_see_child_branch_leads(client):
    """Task 1.2's migration gave `leads` the identical tenant_isolation
    policy pattern used for `companies` (get_all_descendant_ids()), but
    nothing has exercised that mechanism for leads specifically until this
    task. Seeds a lead directly under a child branch (see
    _insert_lead_directly's docstring for why) and confirms the parent
    admin's own token — still scoped to the parent's own company_id, no
    header spoofing involved — can see it via both GET and the list
    endpoint. Mirrors test_tenant_isolation.py's
    test_parent_can_create_and_see_child_branch."""
    parent = await _register_and_login(client, "Parent Co", "parent-lead-admin@acme.test")

    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    child_lead_id = await _insert_lead_directly(child_id, project_name="Seattle Kitchen")

    get_response = await client.get(f"/leads/{child_lead_id}", headers=parent["headers"])
    assert get_response.status_code == 200
    assert get_response.json()["company_id"] == child_id

    list_response = await client.get("/leads", headers=parent["headers"])
    assert list_response.status_code == 200
    ids = {item["id"] for item in list_response.json()["items"]}
    assert child_lead_id in ids


async def test_sibling_branches_cannot_see_each_others_leads(client):
    """Grants the parent admin a *real* company_users row in Branch A
    directly via SQL — there's no cross-tenant invitation flow to create
    this legitimately in a test, the same constraint
    test_tenant_isolation.py's own sibling test ran into (see its
    docstring) — so X-Tenant-ID can genuinely switch the active tenant
    context to Branch A, not merely attempt to spoof it, and the request
    exercises real tenant-scoped list/get logic rather than only the
    membership guard. Confirms that, acting as Branch A, a lead seeded
    directly under sibling Branch B is invisible (404, not an empty
    list/leak of existence)."""
    parent = await _register_and_login(client, "Parent Co", "sib-lead-admin@acme.test")

    child_a = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Branch A"},
        headers=parent["headers"],
    )
    child_b = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Branch B"},
        headers=parent["headers"],
    )
    child_a_id = child_a.json()["id"]
    child_b_id = child_b.json()["id"]

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO company_users (company_id, user_id, role, created_at) "
            "VALUES ($1, $2, 'admin', now())",
            child_a_id,
            parent["user_id"],
        )
    finally:
        await conn.close()

    lead_b_id = await _insert_lead_directly(child_b_id, project_name="Branch B Remodel")

    # Sanity check: acting as Branch A, Branch A's own (empty) list works —
    # if this 403s, the membership row above didn't take and the 404 below
    # would be meaningless (indistinguishable from "can't act as Branch A
    # at all").
    own_list = await client.get(
        "/leads", headers={**parent["headers"], "X-Tenant-ID": child_a_id}
    )
    assert own_list.status_code == 200
    assert own_list.json()["items"] == []

    response = await client.get(
        f"/leads/{lead_b_id}",
        headers={**parent["headers"], "X-Tenant-ID": child_a_id},
    )
    assert response.status_code == 404


async def test_rls_policy_itself_blocks_cross_tenant_lead_visibility(client):
    """Mirrors test_rls_policy_regression.py's
    test_rls_policy_itself_blocks_cross_tenant_row_visibility exactly,
    adapted to `leads` — the one piece of adversarial coverage nothing has
    exercised for the CRM tables until this task, and the task's primary
    deliverable. Connects as app_user directly (bypassing the FastAPI app
    entirely) to prove the POLICY, not app-layer filtering, blocks
    cross-tenant access to a lead row. Then disables RLS as the table
    owner and confirms the identical query starts returning the row —
    showing the policy, not luck, was responsible. Then ALWAYS restores
    RLS in a finally, even if an assertion above fails partway through, so
    this test can never leave the database in an insecure state for any
    test that runs after it."""
    a = await _register_and_login(client, "Company A", "rls-lead-a@acme.test")
    b = await _register_and_login(client, "Company B", "rls-lead-b@acme.test")
    lead_b = await _create_lead(client, b["headers"])
    lead_b_id = lead_b["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        # set_config(), not `SET app.current_tenant = $1` — see
        # set_current_tenant's docstring in app/db.py (Task 3) for why a
        # bound parameter there is a syntax error.
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
        )
        visible_as_a = await app_conn.fetchrow("SELECT id FROM leads WHERE id = $1", lead_b_id)
        assert visible_as_a is None, "RLS should block Company A's session from seeing Company B's lead"
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        await owner_conn.execute("ALTER TABLE leads DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(APP_CONN_DSN)
        try:
            await app_conn2.execute(
                "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
            )
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM leads WHERE id = $1", lead_b_id
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's lead row should exist and "
                "be visible once RLS is off — if this fails, the row itself "
                "is missing, which means the test setup (not the policy) is "
                "broken."
            )
        finally:
            await app_conn2.close()
    finally:
        # ALWAYS restore RLS even if the assertion above fails, so this test
        # can't leave the database in an insecure state for other tests —
        # same two-level try/finally shape as
        # test_rls_policy_regression.py, and for the same reason: the
        # restore itself is isolated so a failure in the ALTER (network
        # blip, lock wait) still guarantees owner_conn is closed rather
        # than leaking it, and doesn't get masked by replacing a
        # still-propagating AssertionError from the block above.
        try:
            await owner_conn.execute("ALTER TABLE leads ENABLE ROW LEVEL SECURITY")
        finally:
            await owner_conn.close()
