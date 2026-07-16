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
from datetime import date

import asyncpg

from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL, set_subscription_tier

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
    # Tier gating (Task 5.7): child-branch creation is Enterprise-gated;
    # registration can only produce trialing/pro.
    await set_subscription_tier(register.json()["company_id"], "enterprise")
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


# =============================================================================
# Task 1.17: Project Management tenant-isolation regression tests
# =============================================================================
#
# Extends this file rather than starting a new one — same module (tenant
# isolation regression coverage), same helper shape
# (_register_and_login/_insert_*_directly), just a different set of tables.
#
# A citation note: the Phase 1 plan's Task 1.17 bullet attributes this
# task's hierarchy-visibility focus to 'US-3.1's "hierarchical visibility"
# business rule'. That attribution is imprecise, not absent: the phrase
# DOES appear verbatim in docs/02-functional-requirements.md line 102 —
# "Hierarchical visibility: a user with parent-branch access sees Projects
# across all child branches; a child-branch user sees only their own
# branch's Projects." — but as a general "### Business Rules" bullet for
# the whole Project Management (P1) section (covering US-3.1 through
# US-3.6), not attached to the specific US-3.1 story number, which is
# itself actually about Phases/Tasks ("As a Project Manager, I can define
# Phases... and add Tasks within each Phase"). Found during this task's
# spec review. The underlying intent the plan cites is still correct and
# well-supported by that Business Rules bullet: migration 0004's own
# docstring confirms `projects`, `phases`, `tasks`, `documents`, and
# `daily_logs` all carry the identical `tenant_isolation` policy pattern as
# `companies`/`leads` — a single FOR ALL policy gated through
# get_all_descendant_ids() — so a parent company's session does see rows
# belonging to its own id AND every descendant branch's id, and siblings
# never see each other's. That Business Rules bullet and the migration
# docstring, not the plan's specific-but-slightly-misattributed US-3.1
# citation, are what these tests actually verify.
#
# Rigor allocation mirrors Task 1.8's own choice to apply its deepest
# coverage (header-spoofing, parent/child hierarchy, sibling isolation, and
# the RLS-disable/re-enable proof) to exactly one representative table
# (`leads`) rather than duplicating all four across every CRM table, on the
# reasoning that the POLICY MECHANISM — not the table — is what's under
# test. The same judgment is applied here: `projects` (the table the other
# four cascade from, and the specific table an earlier session's field_crew
# visibility bug touched) gets the full treatment. `phases`, `tasks`,
# `documents`, and `daily_logs` each get one cheap, direct parent/child
# hierarchy-visibility test apiece, since seeding a row under a child branch
# and confirming the parent's own token can see it costs little and this is
# the one guarantee nothing prior to this task has exercised for any of
# these five tables. Sibling-isolation and the RLS-disable/re-enable proof
# are not repeated per-table: cross-tenant 404 coverage for phases, tasks,
# documents, and daily_logs already exists in test_phases_tasks.py,
# test_documents.py, and test_daily_logs.py from Tasks 1.14-1.16, and the
# policy-mechanism proof done once against `projects` below is
# representative of all five per migration 0004's docstring.


def _project_payload(**overrides):
    payload = {
        "name": "Kitchen Remodel",
        "site_address": "123 Main St",
    }
    payload.update(overrides)
    return payload


async def _create_project(client, headers, **overrides):
    response = await client.post("/projects", json=_project_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _insert_project_directly(company_id, **overrides):
    """Seeds a project row scoped to an arbitrary company_id via the
    RLS-exempt owner connection. Same rationale as _insert_lead_directly
    above: there's no legitimate way to create a project "as" a
    child-branch company through the API (the only account that exists is
    the parent admin's, whose active tenant context is always the parent's
    own company_id), so this is the only way to get a real row under a
    child branch to test hierarchy visibility against."""
    project_id = str(uuid.uuid4())
    fields = {"name": "Branch Kitchen Remodel", "site_address": "1 Branch Way"}
    fields.update(overrides)
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO projects (id, company_id, name, site_address) VALUES ($1, $2, $3, $4)",
            project_id,
            company_id,
            fields["name"],
            fields["site_address"],
        )
    finally:
        await conn.close()
    return project_id


async def _insert_phase_directly(project_id, company_id, **overrides):
    """See _insert_project_directly's docstring — identical rationale,
    applied to `phases`."""
    phase_id = str(uuid.uuid4())
    fields = {"name": "Branch Foundation", "sequence": 0}
    fields.update(overrides)
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO phases (id, project_id, company_id, name, sequence) VALUES ($1, $2, $3, $4, $5)",
            phase_id,
            project_id,
            company_id,
            fields["name"],
            fields["sequence"],
        )
    finally:
        await conn.close()
    return phase_id


async def _insert_task_directly(phase_id, company_id, **overrides):
    """See _insert_project_directly's docstring — identical rationale,
    applied to `tasks`."""
    task_id = str(uuid.uuid4())
    fields = {"name": "Branch Pour Footings"}
    fields.update(overrides)
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO tasks (id, phase_id, company_id, name) VALUES ($1, $2, $3, $4)",
            task_id,
            phase_id,
            company_id,
            fields["name"],
        )
    finally:
        await conn.close()
    return task_id


async def _insert_document_directly(project_id, company_id, uploaded_by, **overrides):
    """See _insert_project_directly's docstring — identical rationale,
    applied to `documents`. `storage_path` is a fabricated relative path,
    not a real on-disk file — this bypasses document_storage.py entirely
    (same as bypassing the API for the other four tables), and nothing
    here touches the filesystem."""
    document_id = str(uuid.uuid4())
    fields = {
        "file_name": "branch-blueprint.pdf",
        "storage_path": f"{company_id}/{project_id}/1/branch-blueprint.pdf",
    }
    fields.update(overrides)
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO documents (id, project_id, company_id, file_name, storage_path, uploaded_by) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            document_id,
            project_id,
            company_id,
            fields["file_name"],
            fields["storage_path"],
            uuid.UUID(uploaded_by),
        )
    finally:
        await conn.close()
    return document_id


async def _insert_daily_log_directly(project_id, company_id, author_id, **overrides):
    """See _insert_project_directly's docstring — identical rationale,
    applied to `daily_logs`."""
    log_id = str(uuid.uuid4())
    fields = {"log_date": date(2026, 8, 15), "notes": "Branch site notes"}
    fields.update(overrides)
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO daily_logs (id, project_id, company_id, author_id, log_date, notes) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            log_id,
            project_id,
            company_id,
            uuid.UUID(author_id),
            fields["log_date"],
            fields["notes"],
        )
    finally:
        await conn.close()
    return log_id


# --- `projects`: full Task-1.8-equivalent rigor ------------------------------


async def test_project_header_spoofing_via_x_tenant_id_is_blocked(client):
    """Mirrors test_lead_header_spoofing_via_x_tenant_id_is_blocked, through
    /projects instead of /leads — the X-Tenant-ID membership check in
    app/core/deps.py is route-agnostic, so this is expected to already
    hold, but confirming it explicitly on a Project-Management-scoped route
    costs little."""
    a = await _register_and_login(client, "Company A", "spoof-proj-a@acme.test")
    b = await _register_and_login(client, "Company B", "spoof-proj-b@acme.test")
    project_b = await _create_project(client, b["headers"])

    response = await client.get(
        f"/projects/{project_b['id']}",
        headers={**a["headers"], "X-Tenant-ID": b["company_id"]},
    )
    assert response.status_code == 403  # membership check rejects the spoofed claim


async def test_parent_admin_can_see_child_branch_project(client):
    """Mirrors test_parent_admin_can_see_child_branch_leads. Seeds a project
    directly under a child branch and confirms the parent admin's own
    token — still scoped to the parent's own company_id, no header spoofing
    involved — can see it via both GET and the list endpoint, exercising
    migration 0004's get_all_descendant_ids()-gated tenant_isolation policy
    for `projects`."""
    parent = await _register_and_login(client, "Parent Co", "parent-proj-admin@acme.test")

    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    child_project_id = await _insert_project_directly(child_id, name="Seattle Kitchen")

    get_response = await client.get(f"/projects/{child_project_id}", headers=parent["headers"])
    assert get_response.status_code == 200
    assert get_response.json()["company_id"] == child_id

    list_response = await client.get("/projects", headers=parent["headers"])
    assert list_response.status_code == 200
    ids = {item["id"] for item in list_response.json()["items"]}
    assert child_project_id in ids


async def test_sibling_branches_cannot_see_each_others_projects(client):
    """Mirrors test_sibling_branches_cannot_see_each_others_leads exactly,
    for `projects`: grants the parent admin a real company_users row in
    Branch A directly via SQL (no legitimate cross-tenant invitation flow
    exists to do this through the API), so X-Tenant-ID genuinely switches
    the active tenant context rather than merely attempting to spoof it,
    and confirms a project seeded directly under sibling Branch B is
    invisible (404) while acting as Branch A."""
    parent = await _register_and_login(client, "Parent Co", "sib-proj-admin@acme.test")

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

    project_b_id = await _insert_project_directly(child_b_id, name="Branch B Remodel")

    # Sanity check: acting as Branch A, Branch A's own (empty) list works —
    # if this 403s, the membership row above didn't take and the 404 below
    # would be meaningless.
    own_list = await client.get(
        "/projects", headers={**parent["headers"], "X-Tenant-ID": child_a_id}
    )
    assert own_list.status_code == 200
    assert own_list.json()["items"] == []

    response = await client.get(
        f"/projects/{project_b_id}",
        headers={**parent["headers"], "X-Tenant-ID": child_a_id},
    )
    assert response.status_code == 404


async def test_rls_policy_itself_blocks_cross_tenant_project_visibility(client):
    """Mirrors test_rls_policy_itself_blocks_cross_tenant_lead_visibility
    exactly, adapted to `projects` — the primary deliverable of this task's
    rigor for the anchor table. Connects as app_user directly (bypassing
    the FastAPI app entirely) to prove the POLICY, not app-layer filtering,
    blocks cross-tenant access to a project row. Then disables RLS as the
    table owner and confirms the identical query starts returning the row
    — showing the policy, not luck, was responsible. Then ALWAYS restores
    RLS in a finally, even if an assertion above fails partway through, so
    this test can never leave the database in an insecure state for any
    test that runs after it. Migration 0004's docstring confirms `phases`,
    `tasks`, `documents`, and `daily_logs` all share this exact policy
    pattern, so this one proof is representative of all five — the same
    reasoning Task 1.8 applied when it proved this mechanism once against
    `leads` rather than repeating it for `communication_logs`."""
    a = await _register_and_login(client, "Company A", "rls-proj-a@acme.test")
    b = await _register_and_login(client, "Company B", "rls-proj-b@acme.test")
    project_b = await _create_project(client, b["headers"])
    project_b_id = project_b["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
        )
        visible_as_a = await app_conn.fetchrow(
            "SELECT id FROM projects WHERE id = $1", project_b_id
        )
        assert visible_as_a is None, "RLS should block Company A's session from seeing Company B's project"
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        await owner_conn.execute("ALTER TABLE projects DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(APP_CONN_DSN)
        try:
            await app_conn2.execute(
                "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
            )
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM projects WHERE id = $1", project_b_id
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's project row should exist "
                "and be visible once RLS is off — if this fails, the row "
                "itself is missing, which means the test setup (not the "
                "policy) is broken."
            )
        finally:
            await app_conn2.close()
    finally:
        # ALWAYS restore RLS even if the assertion above fails — see
        # test_rls_policy_itself_blocks_cross_tenant_lead_visibility's
        # docstring for why this is a separate try/finally.
        try:
            await owner_conn.execute("ALTER TABLE projects ENABLE ROW LEVEL SECURITY")
        finally:
            await owner_conn.close()


# --- `phases`/`tasks`: direct DB-level hierarchy checks -----------------------
#
# Neither table has a GET/list route in this codebase (test_phases_tasks.py's
# own docstring: POST/PATCH are the only phase/task routes in scope), so
# there's no HTTP path to drive a hierarchy-visibility check through. These
# connect as app_user directly — the same technique the RLS-disable/re-enable
# proof above uses for its "visible as A" half, minus the disable/re-enable
# step, since the goal here is only to confirm the USING clause's
# get_all_descendant_ids() gating actually includes child-branch rows for
# these two tables, not to re-prove the policy mechanism from scratch.


async def test_parent_admin_can_see_child_branch_phase(client):
    parent = await _register_and_login(client, "Parent Co", "parent-phase-admin@acme.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    child_project_id = await _insert_project_directly(child_id, name="Seattle Kitchen")
    child_phase_id = await _insert_phase_directly(child_project_id, child_id, name="Seattle Foundation")

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", parent["company_id"]
        )
        row = await app_conn.fetchrow("SELECT id, company_id FROM phases WHERE id = $1", child_phase_id)
        assert row is not None, "parent's session should see a phase seeded under its own child branch"
        assert str(row["company_id"]) == child_id
    finally:
        await app_conn.close()


async def test_parent_admin_can_see_child_branch_task(client):
    parent = await _register_and_login(client, "Parent Co", "parent-task-admin@acme.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    child_project_id = await _insert_project_directly(child_id, name="Seattle Kitchen")
    child_phase_id = await _insert_phase_directly(child_project_id, child_id, name="Seattle Foundation")
    child_task_id = await _insert_task_directly(child_phase_id, child_id, name="Seattle Pour Footings")

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", parent["company_id"]
        )
        row = await app_conn.fetchrow("SELECT id, company_id FROM tasks WHERE id = $1", child_task_id)
        assert row is not None, "parent's session should see a task seeded under its own child branch"
        assert str(row["company_id"]) == child_id
    finally:
        await app_conn.close()


# --- `documents`/`daily_logs`: HTTP-level hierarchy checks --------------------
#
# Unlike phases/tasks, both of these have a real GET /projects/{id}/documents
# and GET /projects/{id}/daily-logs list route (Tasks 1.15/1.16), so these
# drive hierarchy visibility end-to-end through the actual API rather than a
# raw SQL check — one level deeper than the `projects` test above (project ->
# child resource), through the parent admin's own token with no header
# spoofing.


async def test_parent_admin_can_see_child_branch_document(client):
    parent = await _register_and_login(client, "Parent Co", "parent-doc-admin@acme.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    child_project_id = await _insert_project_directly(child_id, name="Seattle Kitchen")
    child_document_id = await _insert_document_directly(
        child_project_id, child_id, parent["user_id"], file_name="seattle-blueprint.pdf"
    )

    list_response = await client.get(
        f"/projects/{child_project_id}/documents", headers=parent["headers"]
    )
    assert list_response.status_code == 200, list_response.text
    ids = {item["id"] for item in list_response.json()["items"]}
    assert child_document_id in ids


async def test_parent_admin_can_see_child_branch_daily_log(client):
    parent = await _register_and_login(client, "Parent Co", "parent-dl-admin@acme.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    child_project_id = await _insert_project_directly(child_id, name="Seattle Kitchen")
    child_log_id = await _insert_daily_log_directly(
        child_project_id, child_id, parent["user_id"], notes="Seattle site notes"
    )

    list_response = await client.get(
        f"/projects/{child_project_id}/daily-logs", headers=parent["headers"]
    )
    assert list_response.status_code == 200, list_response.text
    ids = {item["id"] for item in list_response.json()["items"]}
    assert child_log_id in ids


# --- `documents`/`daily_logs`: WRITE-side company_id sourcing -----------------
#
# The two tests above prove parent -> child READ visibility for rows whose
# company_id was hand-set correctly by _insert_document_directly/
# _insert_daily_log_directly — they never exercise upload_document's/
# create_daily_log's own company_id-assignment logic at all. These two new
# tests close that gap: the parent admin's own token (still scoped to the
# PARENT's own company_id, no X-Tenant-ID switching) creates a Document/
# DailyLog against a REAL child-branch Project via the actual
# POST /projects/{id}/documents / POST /projects/{id}/daily-logs routes —
# RLS's get_all_descendant_ids() grant lets this succeed even without
# switching tenant context, exactly the scenario that surfaced a bug: both
# routes used to stamp the new row with `current.company_id` (the PARENT's
# id) instead of `project.company_id` (the child's own id), so the
# resulting Document/DailyLog silently belonged to the wrong company —
# invisible to a session later scoped directly to the child branch, despite
# hanging off that branch's own Project. Fixed by deriving company_id from
# the parent Project row instead of the acting session.


async def test_creating_document_under_child_branch_project_uses_project_company_id(client):
    parent = await _register_and_login(client, "Parent Co", "parent-doc-write-admin@acme.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    child_project_id = await _insert_project_directly(child_id, name="Seattle Kitchen")

    # Deliberately the parent's own default headers, NOT X-Tenant-ID-switched
    # to the child — RLS alone (get_all_descendant_ids()) makes the child's
    # Project visible/writable to this session.
    upload = await client.post(
        f"/projects/{child_project_id}/documents",
        data={"file_name": "seattle-blueprint.pdf"},
        files={"file": ("seattle-blueprint.pdf", b"fake-pdf-bytes", "application/pdf")},
        headers=parent["headers"],
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["company_id"] == child_id, (
        "Document created against a child-branch Project must belong to the "
        "PROJECT's own company (the child), not the acting session's "
        f"company (the parent) — got {upload.json()['company_id']!r}, "
        f"expected child_id={child_id!r}"
    )

    # Read it back — still the parent's own default headers, no X-Tenant-ID
    # switch (the parent has no `company_users` membership in the child at
    # all; only RLS's get_all_descendant_ids() grant makes the child's rows
    # visible to a parent-scoped session, the same mechanism the write above
    # relied on) — confirming the row is genuinely persisted and visible,
    # not just correctly labeled in the create response.
    list_response = await client.get(
        f"/projects/{child_project_id}/documents", headers=parent["headers"]
    )
    assert list_response.status_code == 200, list_response.text
    ids = {item["id"] for item in list_response.json()["items"]}
    assert upload.json()["id"] in ids


async def test_creating_daily_log_under_child_branch_project_uses_project_company_id(client):
    parent = await _register_and_login(client, "Parent Co", "parent-dl-write-admin@acme.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    child_project_id = await _insert_project_directly(child_id, name="Seattle Kitchen")

    daily_log = await client.post(
        f"/projects/{child_project_id}/daily-logs",
        json={"log_date": "2026-01-01", "notes": "Seattle site notes"},
        headers=parent["headers"],  # parent's own default headers, no X-Tenant-ID switch
    )
    assert daily_log.status_code == 201, daily_log.text
    assert daily_log.json()["company_id"] == child_id, (
        "DailyLog created against a child-branch Project must belong to the "
        "PROJECT's own company (the child), not the acting session's "
        f"company (the parent) — got {daily_log.json()['company_id']!r}, "
        f"expected child_id={child_id!r}"
    )

    # Read it back — same "parent's own default headers, no X-Tenant-ID
    # switch" reasoning as the Document test above.
    list_response = await client.get(
        f"/projects/{child_project_id}/daily-logs", headers=parent["headers"]
    )
    assert list_response.status_code == 200, list_response.text
    ids = {item["id"] for item in list_response.json()["items"]}
    assert daily_log.json()["id"] in ids


async def _fetch_audit_rows(company_id):
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        return await conn.fetch(
            "SELECT action, entity_id, log_metadata FROM audit_log WHERE company_id = $1",
            company_id,
        )
    finally:
        await conn.close()


async def test_changing_status_of_child_branch_project_uses_project_company_id_in_audit_log(
    client,
):
    """Same class of bug as the Document/DailyLog tests above, in
    `update_project_status`: the `project.status_changed` audit log entry
    used to come from `current.company_id` rather than
    `project.company_id`. `_get_project_or_404` already makes a
    child-branch Project reachable via RLS's `get_all_descendant_ids()`
    grant without switching `X-Tenant-ID`, so a parent admin's own default
    session can legally transition a descendant branch's Project — and the
    resulting audit entry must be recorded under the PROJECT's own company
    (the child), not the acting session's (the parent), or a session later
    scoped directly to the child branch auditing its own Project's history
    would see nothing for a status change the parent made."""
    parent = await _register_and_login(client, "Parent Co", "parent-status-write-admin@acme.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201
    child_id = create_child.json()["id"]

    child_project_id = await _insert_project_directly(child_id, name="Seattle Kitchen")

    # Deliberately the parent's own default headers, NOT X-Tenant-ID-switched
    # to the child — RLS alone makes the child's Project visible/writable.
    transition = await client.patch(
        f"/projects/{child_project_id}/status",
        json={"status": "pre_construction"},
        headers=parent["headers"],
    )
    assert transition.status_code == 200, transition.text
    assert transition.json()["status"] == "pre_construction"

    audit_rows = await _fetch_audit_rows(child_id)
    matching = [row for row in audit_rows if row["action"] == "project.status_changed"]
    assert len(matching) == 1, (
        "The project.status_changed audit log entry must be recorded under "
        "the PROJECT's own company (the child), not the acting session's "
        "company (the parent) — found 0 matching rows scoped to the child "
        "company"
    )
    assert str(matching[0]["entity_id"]) == child_project_id
