"""Task 3.7: Compliance Tenant-Isolation Regression Tests
(Subcontractors/ComplianceDocuments).

New file rather than extending test_tenant_isolation_phase2.py: that file's
own module docstring already explains the "new file per phase/module, not
one giant tenant-isolation file" convention (test_tenant_isolation_phase1.py
got its own file, and phase2's has since grown to 1400+ lines covering both
Cost Catalog and Estimates/Change-Orders/Esignatures) — the same reasoning
applies here, matching precedent for a fresh Phase 3 file.

What this file deliberately does NOT re-derive (see test_subcontractors.py,
Tasks 3.4/3.5, for the existing coverage):
  - Cross-tenant 404 on GET /subcontractors/{id}
    (test_get_subcontractor_cross_tenant_returns_404), on
    POST /subcontractors/{id}/compliance-documents
    (test_upload_compliance_document_cross_tenant_subcontractor_returns_404),
    and on GET /subcontractors/{id}/compliance-documents
    (test_list_compliance_documents_cross_tenant_subcontractor_returns_404).
  - RBAC (admin-only write, admin/project_manager/accountant read;
    field_crew/client forbidden everywhere) for every one of these routes.
  - Nonexistent-id 404s, pagination, validation (doc_type/expires_on/
    filename), and storage-path shape.

What IS new here, matching this task's spec:
  (a) test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked_for_subcontractors —
      the X-Tenant-ID membership guard (app/core/deps.py), which runs BEFORE
      any RLS policy is evaluated, rejects an attempt to access a genuinely
      unrelated company's subcontractors by spoofing the tenant header.
      Mirrors test_tenant_isolation_phase2.py's own
      test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked.
  (b) test_rls_policy_itself_blocks_cross_tenant_subcontractor_visibility —
      the ONE RLS-disable/re-enable proof this task's spec calls for, on
      `subcontractors` only. `subcontractors`/`compliance_documents`/
      `subcontractor_assignments`/`compliance_notifications` all share the
      IDENTICAL plain `tenant_isolation` policy shape (migration 0009's own
      module docstring: "each gets its own ordinary, single, non-inherited
      tenant_isolation policy, the exact shape 0008 gave change_orders") —
      one proof per distinct policy shape is this codebase's established
      convention (test_tenant_isolation_phase2.py's own
      test_rls_policy_itself_blocks_cross_tenant_change_order_visibility
      docstring states this explicitly), so `compliance_documents` etc. are
      not re-proven here. Connects as app_user directly (bypassing the
      FastAPI app entirely, and therefore `_get_subcontractor_or_404`), to
      prove the POLICY itself — not application-layer filtering — blocks a
      genuinely unrelated tenant from seeing another tenant's subcontractor
      row. Then disables RLS as the table owner, confirms the identical
      query starts returning the row, then ALWAYS restores RLS in a finally
      (even if an assertion above fails), so this test can never leave the
      database in an insecure state for any test that runs after it in the
      same pytest session — same two-level try/finally discipline as every
      other RLS-disable/re-enable proof in this codebase.
  (c) test_parent_admin_can_see_child_branch_subcontractor_and_compliance_document
      and test_sibling_branches_cannot_see_each_others_subcontractor_or_compliance_document —
      the parent/child hierarchy visibility case, same precedent as every
      prior phase's own isolation file
      (test_tenant_isolation_phase1.py's leads,
      test_tenant_isolation_phase2.py's estimates/change_orders): a
      parent-company admin's own token (no X-Tenant-ID switch) can see a
      child branch's Subcontractor and ComplianceDocument via RLS's
      get_all_descendant_ids() grant; two sibling branches of the same
      parent cannot see each other's, checked symmetrically.
  (d) test_creating_compliance_document_under_child_branch_subcontractor_uses_subcontractor_company_id —
      the write-side company_id-sourcing test explicitly called for by this
      task, proving Task 3.5's upload_compliance_document
      (app/routers/subcontractors.py) got `company_id=subcontractor.company_id`
      right from the start — NOT a bug being fixed after the fact (verified
      during Task 3.5's own spec-compliance review). Exact same empirical
      shape as test_tenant_isolation_phase2.py's own
      test_creating_change_order_under_child_branch_project_uses_project_company_id:
      the parent admin creates a Subcontractor under a child branch (via
      X-Tenant-ID-switched headers), then uploads a compliance document
      against that subcontractor using the PARENT's own DEFAULT headers
      (deliberately NOT X-Tenant-ID-switched to the child) — RLS's
      get_all_descendant_ids() grant alone makes the child's subcontractor
      visible/writable to this session, which is the only way
      `current.company_id` (parent) and `subcontractor.company_id` (child)
      genuinely diverge without an explicit header switch. Asserts the
      resulting ComplianceDocument's `company_id` (present on
      ComplianceDocumentResponse) equals the CHILD's id, not the parent's.

Helper duplication (`_register_and_login`, `_create_subcontractor`,
`_upload_compliance_document`, `_add_membership_directly`,
`_create_child_with_membership`) follows the established per-test-file
convention (test_subcontractors.py's own module docstring; also
test_tenant_isolation_phase2.py) rather than sharing them via conftest.py.
"""
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


async def _add_membership_directly(user_id, company_id, role):
    """Grants an existing user a real company_users row in a company they
    neither registered nor were invited into. There is no legitimate API
    path for this (see test_cost_catalog.py's module docstring for the full
    chicken-and-egg explanation) — this is test-setup plumbing, identical
    rationale to test_tenant_isolation_phase2.py's own
    _add_membership_directly."""
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
    as either company via X-Tenant-ID. Identical to
    test_tenant_isolation_phase2.py's helper of the same name — duplicated
    rather than imported across test modules, matching this codebase's
    existing convention of each test file owning its own self-contained
    helper set."""
    create = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": name},
        headers=parent["headers"],
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


def _subcontractor_payload(**overrides):
    payload = {
        "name": "Ace Plumbing Co",
        "trade": "plumbing",
        "contact_email": "contact@aceplumbing.test",
    }
    payload.update(overrides)
    return payload


async def _create_subcontractor(client, actor, **overrides):
    response = await client.post(
        "/subcontractors", json=_subcontractor_payload(**overrides), headers=actor["headers"]
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _upload_compliance_document(
    client, actor, subcontractor_id, *, doc_type="insurance_certificate", expires_on="2027-01-01",
    file_name="certificate.pdf", content: bytes = b"pdf-bytes",
):
    return await client.post(
        f"/subcontractors/{subcontractor_id}/compliance-documents",
        data={"doc_type": doc_type, "expires_on": expires_on},
        files={"file": (file_name, content, "application/octet-stream")},
        headers=actor["headers"],
    )


# =============================================================================
# (a) Header-spoofing via X-Tenant-ID, genuinely unrelated tenant.
# =============================================================================


async def test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked_for_subcontractors(
    client,
):
    """Mirrors test_tenant_isolation_phase2.py's own
    test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked,
    through /subcontractors instead of /catalogs/items. The X-Tenant-ID
    membership check in app/core/deps.py is route-agnostic and runs BEFORE
    any RLS policy is ever evaluated, so Company A cannot even reach the
    point of asking RLS whether it can see Company B's row — the request is
    rejected outright because Company A has no company_users membership row
    in Company B (a genuinely unrelated company, not a parent/child of A)."""
    a = await _register_and_login(client, "Company A", "spoof-sub-a@acme.test")
    b = await _register_and_login(client, "Company B", "spoof-sub-b@acme.test")
    await _create_subcontractor(client, b, name="B's Sub")

    response = await client.get(
        "/subcontractors",
        headers={**a["headers"], "X-Tenant-ID": b["company_id"]},
    )
    assert response.status_code == 403  # membership check rejects the spoofed claim


# =============================================================================
# (b) RLS-disable/re-enable proof — `subcontractors` only (representative of
# the shared plain policy shape all four Compliance tables use).
# =============================================================================


async def test_rls_policy_itself_blocks_cross_tenant_subcontractor_visibility(client):
    """Mirrors test_tenant_isolation_phase2.py's own
    test_rls_policy_itself_blocks_cross_tenant_change_order_visibility
    exactly, adapted to `subcontractors` — the ONE RLS-disable/re-enable
    proof this task's spec calls for (see this file's own module docstring
    for the "one proof per shared policy shape" rationale). Connects as
    app_user directly (bypassing the FastAPI app, and therefore
    `_get_subcontractor_or_404` entirely) to prove the POLICY itself, not
    application-layer filtering, blocks a genuinely unrelated tenant from
    seeing another tenant's subcontractor row. Then disables RLS as the
    table owner and confirms the identical query starts returning the row,
    showing the policy, not luck, was responsible. Then ALWAYS restores RLS
    in a finally, even if an assertion above fails partway through, so this
    test can never leave the database in an insecure state for any test that
    runs after it — same two-level try/finally discipline as every other
    RLS-disable/re-enable proof in this codebase."""
    a = await _register_and_login(client, "Company A", "rls-sub-a@acme.test")
    b = await _register_and_login(client, "Company B", "rls-sub-b@acme.test")
    subcontractor_b = await _create_subcontractor(client, b, name="B's Sub")
    subcontractor_b_id = subcontractor_b["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        # set_config(), not `SET app.current_tenant = $1` — see
        # set_current_tenant's docstring in app/db.py (Task 3) for why a
        # bound parameter there is a syntax error.
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
        )
        visible_as_a = await app_conn.fetchrow(
            "SELECT id FROM subcontractors WHERE id = $1", subcontractor_b_id
        )
        assert visible_as_a is None, (
            "RLS should block Company A's session from seeing Company B's "
            "subcontractor"
        )
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        await owner_conn.execute("ALTER TABLE subcontractors DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(APP_CONN_DSN)
        try:
            await app_conn2.execute(
                "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
            )
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM subcontractors WHERE id = $1", subcontractor_b_id
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's subcontractor row should "
                "exist and be visible once RLS is off — if this fails, the "
                "row itself is missing, which means the test setup (not the "
                "policy) is broken."
            )
        finally:
            await app_conn2.close()
    finally:
        # ALWAYS restore RLS even if the assertion above fails — see this
        # test's own docstring for why this is a separate try/finally.
        try:
            await owner_conn.execute("ALTER TABLE subcontractors ENABLE ROW LEVEL SECURITY")
        finally:
            await owner_conn.close()


# =============================================================================
# (c) Parent/child hierarchy visibility, `subcontractors` and
# `compliance_documents` (judgment call #8, Phase 1's Task 1.17 precedent).
# =============================================================================


async def test_parent_admin_can_see_child_branch_subcontractor_and_compliance_document(client):
    """Task 3.1's/3.4's/3.5's migration and router gave `subcontractors`/
    `compliance_documents` the identical `get_all_descendant_ids()`
    tenant_isolation policy shape used for `leads`/`projects`/
    `change_orders`, but nothing has exercised that mechanism for either
    table specifically until this task. Builds a real Subcontractor and
    ComplianceDocument through the real API, acting AS the child branch
    (X-Tenant-ID, backed by a genuine company_users row from
    `_create_child_with_membership` — not header-spoofing). Then confirms
    the parent admin's own token — still scoped to the parent's own
    company_id, no header switching involved — can see both via
    `GET /subcontractors/{id}` and
    `GET /subcontractors/{id}/compliance-documents` (list). Mirrors
    test_tenant_isolation_phase1.py's
    test_parent_admin_can_see_child_branch_leads and
    test_tenant_isolation_phase2.py's
    test_parent_admin_can_see_child_branch_estimate_and_change_order."""
    parent = await _register_and_login(client, "Parent Co", "parent-hier-sub-admin@acme.test")
    child_id = await _create_child_with_membership(client, parent, "Seattle Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}

    subcontractor = await _create_subcontractor(client, {"headers": child_headers})
    upload = await _upload_compliance_document(client, {"headers": child_headers}, subcontractor["id"])
    assert upload.status_code == 201, upload.text
    compliance_document = upload.json()

    get_subcontractor = await client.get(
        f"/subcontractors/{subcontractor['id']}", headers=parent["headers"]
    )
    assert get_subcontractor.status_code == 200, get_subcontractor.text
    assert get_subcontractor.json()["id"] == subcontractor["id"]

    list_compliance_documents = await client.get(
        f"/subcontractors/{subcontractor['id']}/compliance-documents", headers=parent["headers"]
    )
    assert list_compliance_documents.status_code == 200, list_compliance_documents.text
    ids = {item["id"] for item in list_compliance_documents.json()["items"]}
    assert compliance_document["id"] in ids


async def test_sibling_branches_cannot_see_each_others_subcontractor_or_compliance_document(client):
    """Grants the parent admin real company_users rows in BOTH sibling
    branches directly via SQL (`_create_child_with_membership`, twice) so
    X-Tenant-ID genuinely switches the active tenant context to either
    branch rather than merely attempting to spoof it — same setup this
    codebase's other sibling-branch tests
    (test_tenant_isolation_phase1.py's
    test_sibling_branches_cannot_see_each_others_leads,
    test_tenant_isolation_phase2.py's
    test_sibling_branches_cannot_see_each_others_estimate_or_change_order)
    use. Each branch builds its own Subcontractor and ComplianceDocument
    through the real API. Acting as Branch A, Branch B's Subcontractor is
    invisible (404 via `GET /subcontractors/{id}`), and Branch B's
    ComplianceDocument list — nested under Branch B's own Subcontractor — is
    equally invisible (404): the subcontractor itself 404s before the
    compliance_documents table's own RLS check is ever reached, the same
    "nested resource 404s on the invisible parent" pattern every other
    nested route in this codebase follows. Checked symmetrically in both
    directions."""
    parent = await _register_and_login(client, "Parent Co", "sib-hier-sub-admin@acme.test")
    child_a_id = await _create_child_with_membership(client, parent, "Branch A")
    child_b_id = await _create_child_with_membership(client, parent, "Branch B")
    headers_a = {**parent["headers"], "X-Tenant-ID": child_a_id}
    headers_b = {**parent["headers"], "X-Tenant-ID": child_b_id}

    subcontractor_a = await _create_subcontractor(client, {"headers": headers_a}, name="A's Sub")
    upload_a = await _upload_compliance_document(client, {"headers": headers_a}, subcontractor_a["id"])
    assert upload_a.status_code == 201, upload_a.text

    subcontractor_b = await _create_subcontractor(client, {"headers": headers_b}, name="B's Sub")
    upload_b = await _upload_compliance_document(client, {"headers": headers_b}, subcontractor_b["id"])
    assert upload_b.status_code == 201, upload_b.text

    # --- Acting as Branch A: Branch B's rows are invisible. -----------------
    get_sub_b_as_a = await client.get(f"/subcontractors/{subcontractor_b['id']}", headers=headers_a)
    assert get_sub_b_as_a.status_code == 404

    list_docs_b_as_a = await client.get(
        f"/subcontractors/{subcontractor_b['id']}/compliance-documents", headers=headers_a
    )
    assert list_docs_b_as_a.status_code == 404

    # --- Symmetric: acting as Branch B, Branch A's rows are equally
    # invisible. ---------------------------------------------------------
    get_sub_a_as_b = await client.get(f"/subcontractors/{subcontractor_a['id']}", headers=headers_b)
    assert get_sub_a_as_b.status_code == 404

    list_docs_a_as_b = await client.get(
        f"/subcontractors/{subcontractor_a['id']}/compliance-documents", headers=headers_b
    )
    assert list_docs_a_as_b.status_code == 404


# =============================================================================
# (d) `compliance_documents`: WRITE-side company_id sourcing.
#
# Task 3.5's upload_compliance_document (app/routers/subcontractors.py)
# stamps a new ComplianceDocument with `company_id=subcontractor.company_id`
# (the PARENT ENTITY it's nested under), exactly mirroring
# create_change_order's own post-Phase-2-fix pattern of deriving company_id
# from `project.company_id`, never from `current.company_id` (the ACTING
# session's own company). This was verified correct during Task 3.5's own
# spec-compliance review — this test proves it, using the same empirical
# shape test_tenant_isolation_phase2.py's own
# test_creating_change_order_under_child_branch_project_uses_project_company_id
# established: the case where current.company_id and the parent entity's
# company_id genuinely diverge WITHOUT an explicit X-Tenant-ID switch, via
# RLS's get_all_descendant_ids() grant alone.
# =============================================================================


async def test_creating_compliance_document_under_child_branch_subcontractor_uses_subcontractor_company_id(
    client,
):
    parent = await _register_and_login(client, "Parent Co", "parent-co-write-sub-admin@acme.test")
    child_id = await _create_child_with_membership(client, parent, "Seattle Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}

    subcontractor = await _create_subcontractor(client, {"headers": child_headers})
    assert subcontractor["company_id"] == child_id

    # Deliberately the parent's own default headers, NOT X-Tenant-ID-switched
    # to the child — RLS alone makes the child's Subcontractor visible/
    # writable to this session, which is the only way current.company_id
    # (parent) and subcontractor.company_id (child) genuinely diverge here.
    upload = await _upload_compliance_document(client, parent, subcontractor["id"])
    assert upload.status_code == 201, upload.text
    compliance_document = upload.json()
    assert compliance_document["company_id"] == child_id, (
        "ComplianceDocument created against a child-branch Subcontractor "
        "must belong to the SUBCONTRACTOR's own company (the child), not "
        "the acting session's company (the parent) — got "
        f"{compliance_document['company_id']!r}, expected child_id={child_id!r}"
    )

    # Read it back via the child's own tenant context to confirm it's
    # genuinely visible there too, not just correctly labeled.
    list_response = await client.get(
        f"/subcontractors/{subcontractor['id']}/compliance-documents", headers=child_headers
    )
    assert list_response.status_code == 200, list_response.text
    ids = {item["id"] for item in list_response.json()["items"]}
    assert compliance_document["id"] in ids
