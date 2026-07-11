"""Task 2.6: Cost Catalog tenant-isolation regression tests, adapted for
`cost_catalog_items`' NEW bidirectional RLS policy (migration 0005).

New file rather than extending test_tenant_isolation_phase1.py: that file's
own module docstring already explains the "new file per module, not one
giant tenant-isolation file" convention (leads/communication_logs got their
own file rather than being folded into test_tenant_isolation.py's
companies-only shape), and the same reasoning applies here even more
strongly — `cost_catalog_items` is not just a different table, it is the
ONLY table in this codebase whose tenant_isolation policy is asymmetric
(USING grants visibility both DOWNWARD via get_all_descendant_ids() AND
UPWARD via the new get_all_ancestor_ids(), while WITH CHECK stays
descendant-only). Every helper and test below exists specifically to probe
that asymmetry, which has no equivalent in the phase1 file.

What this file deliberately does NOT re-derive (see each table's own test
file for the existing coverage):
  - Router-level 404 on POST /catalogs/items/{id}/override against a
    genuinely-unrelated tenant's item, router-level "parent unaffected by
    child's override", the resolved/deduped GET /catalogs/items list view,
    and RBAC (admin/PM can write, field_crew/accountant/client cannot) —
    all covered by test_cost_catalog.py (Task 2.5).
  - resolve_visible_catalog_items' own hop-distance/dedup/tie-break/orphan
    logic, and empirical parent-sees-child-and-grandchild-override-chain
    visibility — covered by test_cost_catalog_inheritance.py (Task 2.4).

What IS new here, matching this task's three-part spec:
  (a) test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked
      and test_genuinely_unrelated_tenant_cannot_override_each_others_items —
      a tenant with NO ancestor/descendant relationship at all to another
      sees nothing of its catalog in either direction: header-spoofing is
      rejected by the membership guard (403), and the override route's own
      application-layer visibility check independently rejects both
      directions (A -> B's item and B -> A's item) with 404. The 404 half
      necessarily reuses the same override route Task 2.5 already drives
      through this exact HTTP path (see test_override_of_invisible_item_returns_404)
      — cited rather than re-derived at length — but is included here
      because this file's job is to establish the "genuinely unrelated,
      neither direction of the OR fires" baseline invariant that (b) and
      (c) below build on, and to check it symmetrically (both directions),
      which test_cost_catalog.py's own test only checks one way.
  (b) test_rls_policy_itself_blocks_unrelated_tenant_catalog_item_visibility —
      the RLS-disable/re-enable proof (Phase 0 Task 16's pattern, already
      applied to `companies`/`leads`/`projects`) applied to
      `cost_catalog_items` for the first time: connects as app_user
      directly, confirms invisibility, disables RLS, confirms the row
      becomes visible, re-enables, confirms invisibility returns — proving
      the POLICY itself, not resolve_visible_catalog_items' application-layer
      dedup/membership check, is what blocks a genuinely unrelated tenant.
  (c) test_sibling_branches_cannot_see_each_others_catalog_items_despite_shared_ancestor —
      the single most important test in this file. Two children of the SAME
      parent are each, individually, a legitimate ancestor-chain hit for
      their OWN catalog resolution (each sees the shared parent's items via
      the new upward grant) — but neither is an ancestor OR a descendant of
      the OTHER, so the bidirectional policy's OR must fail on BOTH terms
      for cross-sibling access, exactly as it does for two totally unrelated
      companies. This is the scenario most likely to be gotten subtly wrong
      by an implementation that conflates "somewhere in the same tree" with
      "on my own ancestor chain" — get_all_ancestor_ids(sibling_a) walks
      ONLY sibling_a's own parent_id chain (parent, grandparent, ...), never
      sideways to a sibling. Checked at both the HTTP/app layer (list
      omission, override 404) and the raw RLS-policy layer (direct app_user
      SELECT by id), in both directions, so this doesn't collapse into
      "just ordinary downward isolation under a different name."
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
    chicken-and-egg explanation: POST /companies/{id}/children doesn't grant
    the creating admin membership in the child, and POST /invitations only
    ever scopes to the caller's own already-active tenant) — this is
    test-setup plumbing, the same rationale test_tenant_isolation_phase1.py's
    sibling-branch tests and test_cost_catalog.py's
    _add_membership_directly rely on."""
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
    as either company via X-Tenant-ID. Identical to test_cost_catalog.py's
    helper of the same name — duplicated rather than imported across test
    modules, matching this codebase's existing convention of each test file
    owning its own self-contained helper set."""
    create = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": name},
        headers=parent["headers"],
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


def _item_payload(**overrides):
    payload = {
        "category": "framing",
        "name": "2x4 Lumber",
        "unit": "each",
        "unit_rate": "5.00",
    }
    payload.update(overrides)
    return payload


async def _create_catalog_item(client, headers, **overrides):
    response = await client.post("/catalogs/items", json=_item_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# =============================================================================
# (a) Genuinely unrelated tenant: neither ancestor nor descendant of the
# other, so BOTH terms of the bidirectional policy's OR must fail.
# =============================================================================


async def test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked(client):
    """Mirrors test_tenant_isolation_phase1.py's
    test_lead_header_spoofing_via_x_tenant_id_is_blocked, through a
    catalogs route instead of /leads. The X-Tenant-ID membership check in
    app/core/deps.py is route-agnostic and runs BEFORE any RLS policy is
    ever evaluated, so this is expected to already hold regardless of
    cost_catalog_items' unusual bidirectional policy — but confirming it
    explicitly here matters precisely because that policy grants broader
    read visibility than any other table's: if the membership guard were
    ever accidentally bypassed or weakened for this router, the
    bidirectional policy would make the resulting leak worse (both
    directions) than on any other table."""
    a = await _register_and_login(client, "Company A", "spoof-cat-a@acme.test")
    b = await _register_and_login(client, "Company B", "spoof-cat-b@acme.test")
    await _create_catalog_item(client, b["headers"], name="B's Item")

    response = await client.get(
        "/catalogs/items",
        headers={**a["headers"], "X-Tenant-ID": b["company_id"]},
    )
    assert response.status_code == 403  # membership check rejects the spoofed claim


async def test_genuinely_unrelated_tenant_cannot_override_each_others_items(client):
    """The HTTP-level "sees nothing in either direction" check. There is no
    GET /catalogs/items/{id} route in this codebase (create, override, and
    list are the only three catalogs routes — app/routers/catalogs.py) so
    POST /catalogs/items/{id}/override is the only ID-addressed access
    surface available to probe direct-ID access through the API.
    test_cost_catalog.py::test_override_of_invisible_item_returns_404
    already drives this exact route for a genuinely-unrelated tenant in one
    direction (Task 2.5) — not re-derived at length here. What's new: this
    checks BOTH directions symmetrically (A -> B's item AND B -> A's item),
    which matters specifically for this table because the policy's grant is
    an OR of two independent clauses (descendant, ancestor) — a boundary
    bug in either one alone could make exactly one direction leak while the
    other still correctly 404s, and a single-direction test would never
    catch that."""
    a = await _register_and_login(client, "Company A", "cross-cat-a@acme.test")
    b = await _register_and_login(client, "Company B", "cross-cat-b@acme.test")
    item_a = await _create_catalog_item(client, a["headers"], name="A's Item")
    item_b = await _create_catalog_item(client, b["headers"], name="B's Item")

    a_overrides_b = await client.post(
        f"/catalogs/items/{item_b['id']}/override",
        json=_item_payload(unit_rate="9.99"),
        headers=a["headers"],
    )
    assert a_overrides_b.status_code == 404

    b_overrides_a = await client.post(
        f"/catalogs/items/{item_a['id']}/override",
        json=_item_payload(unit_rate="9.99"),
        headers=b["headers"],
    )
    assert b_overrides_a.status_code == 404


# =============================================================================
# (b) RLS-disable/re-enable regression test — proves the POLICY, not
# resolve_visible_catalog_items' application-layer membership check, is what
# blocks a genuinely unrelated tenant. Mirrors
# test_rls_policy_regression.py::test_rls_policy_itself_blocks_cross_tenant_row_visibility
# and test_tenant_isolation_phase1.py's lead/project equivalents exactly,
# adapted to cost_catalog_items.
# =============================================================================


async def test_rls_policy_itself_blocks_unrelated_tenant_catalog_item_visibility(client):
    """Connects as app_user directly (bypassing the FastAPI app, and
    therefore bypassing resolve_visible_catalog_items entirely) to prove the
    POLICY itself — not app-layer filtering/dedup — blocks a genuinely
    unrelated tenant from seeing another tenant's catalog item. Then
    disables RLS as the table owner and confirms the identical query starts
    returning the row, showing the policy, not luck, was responsible. Then
    ALWAYS restores RLS in a finally, even if an assertion above fails
    partway through, so this test can never leave the database in an
    insecure state for any test that runs after it — same two-level
    try/finally discipline as test_rls_policy_regression.py, for the same
    reason (the restore itself is isolated so a failure in the ALTER
    doesn't get masked by a still-propagating AssertionError, and owner_conn
    is guaranteed closed either way)."""
    a = await _register_and_login(client, "Company A", "rls-cat-a@acme.test")
    b = await _register_and_login(client, "Company B", "rls-cat-b@acme.test")
    item_b = await _create_catalog_item(client, b["headers"], name="B's Item")
    item_b_id = item_b["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        # set_config(), not `SET app.current_tenant = $1` — see
        # set_current_tenant's docstring in app/db.py (Task 3) for why a
        # bound parameter there is a syntax error.
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
        )
        visible_as_a = await app_conn.fetchrow(
            "SELECT id FROM cost_catalog_items WHERE id = $1", item_b_id
        )
        assert visible_as_a is None, (
            "RLS should block Company A's session from seeing Company B's "
            "catalog item — A is neither a descendant nor an ancestor of B, "
            "so both terms of the bidirectional policy's OR should fail"
        )
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        await owner_conn.execute("ALTER TABLE cost_catalog_items DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(APP_CONN_DSN)
        try:
            await app_conn2.execute(
                "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
            )
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM cost_catalog_items WHERE id = $1", item_b_id
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's catalog item row should "
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
            await owner_conn.execute("ALTER TABLE cost_catalog_items ENABLE ROW LEVEL SECURITY")
        finally:
            await owner_conn.close()


# =============================================================================
# (c) Sibling branches: two children of the SAME parent. Each sibling
# legitimately sees the shared parent's catalog (their own ancestor chain)
# but must NOT see each other's — the bidirectional policy's upward grant is
# scoped to the caller's OWN ancestor chain, never a lateral relative's. This
# is the scenario most likely for a naive implementation to get subtly
# wrong, and the primary reason this file exists.
# =============================================================================


async def test_sibling_branches_cannot_see_each_others_catalog_items_despite_shared_ancestor(client):
    """Two children (Branch A, Branch B) of one Parent Co. Grants the parent
    admin real company_users rows in BOTH branches directly via SQL — there
    is no legitimate cross-tenant invitation flow to do this through the
    API (same constraint test_tenant_isolation_phase1.py's own sibling test
    and test_cost_catalog.py's override tests run into) — so X-Tenant-ID
    genuinely switches the active tenant context to either branch rather
    than merely attempting to spoof it.

    First establishes the POSITIVE half of the invariant this test is
    really about: Branch A's own catalog resolution DOES include the
    shared parent's item (get_all_ancestor_ids(branch_a) walks UP branch_a's
    own parent_id chain and correctly reaches Parent Co) — if this sanity
    check failed, the negative assertions below would be meaningless
    (indistinguishable from "the ancestor grant doesn't work at all" rather
    than "the ancestor grant is correctly scoped").

    Then proves the NEGATIVE half — the actual point of this task — in both
    directions and at both layers:
      - HTTP/app layer: acting as Branch A, Branch B's item is absent from
        the resolved list AND overriding it 404s (resolve_visible_catalog_items'
        membership check independently rejects it); symmetric for B -> A.
      - Raw RLS-policy layer: connecting as app_user directly with
        app.current_tenant set to Branch A, a direct SELECT by id for
        Branch B's item returns no row — proving get_all_ancestor_ids(branch_a)
        does NOT include branch_b (they are siblings, not ancestor/
        descendant of each other), even though both branches ARE each
        individually in Parent Co's own descendant set. It is exactly this
        "both descendants of the same ancestor" framing that makes the
        sibling case easy to conflate with ordinary downward isolation —
        the assertion below is scoped to branch_a's own ancestor chain
        specifically, not to any test that could pass merely because
        ordinary downward isolation already blocks unrelated companies."""
    parent = await _register_and_login(client, "Parent Co", "sib-cat-parent@acme.test")
    child_a_id = await _create_child_with_membership(client, parent, "Branch A")
    child_b_id = await _create_child_with_membership(client, parent, "Branch B")

    parent_item = await _create_catalog_item(client, parent["headers"], name="Shared Lumber")

    # --- Positive sanity check: A sees its OWN ancestor's (the shared
    # parent's) item via the new upward grant. -----------------------------
    a_sanity_list = await client.get(
        "/catalogs/items", headers={**parent["headers"], "X-Tenant-ID": child_a_id}
    )
    assert a_sanity_list.status_code == 200
    a_sanity_ids = {item["id"] for item in a_sanity_list.json()["items"]}
    assert parent_item["id"] in a_sanity_ids, (
        "sanity check failed: Branch A should see Parent Co's catalog item "
        "via the bidirectional policy's upward (ancestor) grant — if this "
        "fails, the negative assertions below prove nothing"
    )

    # Each branch creates its own catalog item, through the real API acting
    # as that branch.
    item_a = await _create_catalog_item(
        client, {**parent["headers"], "X-Tenant-ID": child_a_id}, name="Branch A Special Item"
    )
    item_b = await _create_catalog_item(
        client, {**parent["headers"], "X-Tenant-ID": child_b_id}, name="Branch B Special Item"
    )

    # --- HTTP/app layer, both directions -----------------------------------
    a_list = await client.get(
        "/catalogs/items", headers={**parent["headers"], "X-Tenant-ID": child_a_id}
    )
    a_ids = {item["id"] for item in a_list.json()["items"]}
    assert item_b["id"] not in a_ids, "Branch A's resolved catalog must not include sibling Branch B's item"

    b_list = await client.get(
        "/catalogs/items", headers={**parent["headers"], "X-Tenant-ID": child_b_id}
    )
    b_ids = {item["id"] for item in b_list.json()["items"]}
    assert item_a["id"] not in b_ids, "Branch B's resolved catalog must not include sibling Branch A's item"

    a_overrides_b = await client.post(
        f"/catalogs/items/{item_b['id']}/override",
        json=_item_payload(name="Branch A Special Item", unit_rate="7.00"),
        headers={**parent["headers"], "X-Tenant-ID": child_a_id},
    )
    assert a_overrides_b.status_code == 404

    b_overrides_a = await client.post(
        f"/catalogs/items/{item_a['id']}/override",
        json=_item_payload(name="Branch B Special Item", unit_rate="7.00"),
        headers={**parent["headers"], "X-Tenant-ID": child_b_id},
    )
    assert b_overrides_a.status_code == 404

    # --- Raw RLS-policy layer, both directions ------------------------------
    # Bypasses the app (and resolve_visible_catalog_items) entirely, proving
    # the POLICY's get_all_ancestor_ids(caller) call is scoped to the
    # caller's own chain and does not leak sideways to a sibling.
    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute("SELECT set_config('app.current_tenant', $1, false)", child_a_id)
        row_b_as_a = await app_conn.fetchrow(
            "SELECT id FROM cost_catalog_items WHERE id = $1", item_b["id"]
        )
        assert row_b_as_a is None, (
            "RLS should block Branch A's session from seeing Branch B's "
            "catalog item — both are descendants of the same Parent Co, "
            "but B is not on A's own ancestor chain, and A is not a "
            "descendant of B"
        )
    finally:
        await app_conn.close()

    app_conn2 = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn2.execute("SELECT set_config('app.current_tenant', $1, false)", child_b_id)
        row_a_as_b = await app_conn2.fetchrow(
            "SELECT id FROM cost_catalog_items WHERE id = $1", item_a["id"]
        )
        assert row_a_as_b is None, (
            "RLS should equally block Branch B's session from seeing "
            "Branch A's catalog item, for the same reason in reverse"
        )
    finally:
        await app_conn2.close()


# =============================================================================
# Task 2.16: Estimates / Estimate Line Items tenant-isolation regression
# tests
# =============================================================================
#
# Extends this file rather than starting a new one — same module (Phase 2
# tenant-isolation regression coverage), same helper shape
# (_register_and_login/_add_membership_directly), just a different pair of
# tables. Mirrors test_tenant_isolation_phase1.py's own Task 1.17 append
# convention: a new commented section at the end of an existing file, not a
# rewritten module docstring.
#
# Per migration 0007's own docstring, `estimates` and `estimate_line_items`
# are each plain, flat, company-scoped tables with their OWN separate
# ordinary `tenant_isolation` policy — the same get_all_descendant_ids()-only
# shape `leads`/`projects` (0004) use, NOT `cost_catalog_items`' bidirectional
# OR-of-two-clauses shape (0005). The sibling-branch/upward-visibility test
# this file's Task 2.6 section applies to `cost_catalog_items` is therefore
# deliberately NOT replicated here — it exists to probe an asymmetry
# (get_all_ancestor_ids()) that these two tables' policies simply don't have.
# This section is structurally closer to test_tenant_isolation_phase1.py's
# per-table coverage (header-spoofing + one RLS-disable/re-enable proof) than
# to this same file's own cost_catalog_items tests.
#
# What this section deliberately does NOT re-derive:
#   - Cross-tenant 404 on GET /estimates/{id}
#     (test_estimates.py::test_get_estimate_cross_tenant_returns_404, Task
#     2.10) and on PUT /estimates/{id}/lines
#     (test_estimates.py::test_replace_line_items_cross_tenant_estimate_returns_404,
#     Task 2.11) — both already exercise _get_estimate_or_404's ordinary
#     RLS-backed existence check against a genuinely cross-tenant id, the
#     same mechanism this section's own RLS-disable/re-enable proof confirms
#     at the raw-policy layer below. Re-driving the identical HTTP-level 404
#     here would add no new coverage.
#   - RBAC (admin/PM can write, field_crew/client/accountant cannot on
#     write routes; admin/PM/accountant/client can read) and the client's
#     status='sent' list-scoping — all covered by test_estimates.py (Tasks
#     2.10/2.11).
#
# What IS new here:
#   - test_estimate_header_spoofing_via_x_tenant_id_is_blocked — the
#     X-Tenant-ID membership check confirmed explicitly on an
#     Estimates-scoped route, mirroring
#     test_tenant_isolation_phase1.py's test_lead_header_spoofing_via_x_tenant_id_is_blocked
#     and this file's own test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked.
#   - test_rls_policy_itself_blocks_cross_tenant_estimate_visibility — the
#     RLS-disable/re-enable proof (Phase 0 Task 16's pattern), applied to
#     `estimates` for the first time, proving the POLICY itself — not
#     _get_estimate_or_404's application-layer query — blocks a genuinely
#     unrelated tenant. Proven once for `estimates` only, per this task's own
#     spec ("representative of the plain policy shape both tables share"):
#     migration 0007's docstring confirms `estimate_line_items` carries the
#     identical FOR ALL / get_all_descendant_ids()-gated policy shape, so
#     this one proof is representative of both, the same "prove the
#     mechanism once per policy-shape, not per-table" judgment Task 1.8
#     applied to `leads`/`communication_logs` and Task 1.17 applied to
#     `projects`/`phases`/`tasks`/`documents`/`daily_logs`.
#   - test_export_estimate_pdf_forbidden_for_accountant_client_and_field_crew
#     and test_export_estimate_pdf_genuinely_cross_tenant_returns_404 — close
#     a coverage gap Task 2.15's own spec-compliance review flagged as
#     "naturally belonging to Task 2.16": POST /estimates/{id}/export
#     (test_estimate_pdf_export.py, Task 2.15) had no explicit
#     accountant/client 403 test, and its only 404 case
#     (test_export_estimate_pdf_not_found_returns_404) uses an all-zeros
#     nonexistent UUID rather than a genuinely cross-tenant estimate
#     belonging to a real, unrelated company. Both close here, in this
#     task's own file, rather than in test_estimate_pdf_export.py.


def _project_payload(**overrides):
    payload = {
        "name": "Kitchen Remodel Project",
        "site_address": "123 Main St",
    }
    payload.update(overrides)
    return payload


async def _create_project(client, headers, **overrides):
    response = await client.post("/projects", json=_project_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _markup_profile_payload(**overrides):
    payload = {
        "name": "Standard Markup",
        "overhead_pct": "10.00",
        "profit_pct": "15.00",
    }
    payload.update(overrides)
    return payload


async def _create_markup_profile(client, headers, **overrides):
    response = await client.post(
        "/markup-profiles", json=_markup_profile_payload(**overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_estimate(client, headers, *, project_id, markup_profile_id):
    response = await client.post(
        "/estimates",
        json={"project_id": project_id, "markup_profile_id": markup_profile_id},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _invite_and_login_as(client, admin, role, email):
    """Identical to test_estimates.py's helper of the same name — duplicated
    rather than imported, matching this codebase's established
    each-test-file-owns-its-own-helper-set convention."""
    invite = await client.post(
        "/invitations",
        json={"email": email, "role": role},
        headers=admin["headers"],
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Invited User", "password": "anothersecret123"},
    )
    assert accept.status_code == 200, accept.text
    login = await client.post("/auth/login", json={"email": email, "password": "anothersecret123"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


# --- Header-spoofing and RLS-disable/re-enable proof, `estimates` -----------


async def test_estimate_header_spoofing_via_x_tenant_id_is_blocked(client):
    """Mirrors test_tenant_isolation_phase1.py's
    test_lead_header_spoofing_via_x_tenant_id_is_blocked and this file's own
    test_genuinely_unrelated_tenant_header_spoofing_via_x_tenant_id_is_blocked,
    through an Estimates-scoped route. The X-Tenant-ID membership check in
    app/core/deps.py is route-agnostic and runs before any RLS policy is
    evaluated, so this is expected to already hold — confirming it
    explicitly here matters because /estimates is its own router with its
    own dependency wiring, not yet exercised by any prior task's isolation
    coverage."""
    a = await _register_and_login(client, "Company A", "spoof-est-a@acme.test")
    b = await _register_and_login(client, "Company B", "spoof-est-b@acme.test")
    project_b = await _create_project(client, b["headers"])
    markup_b = await _create_markup_profile(client, b["headers"])
    estimate_b = await _create_estimate(
        client, b["headers"], project_id=project_b["id"], markup_profile_id=markup_b["id"]
    )

    response = await client.get(
        f"/estimates/{estimate_b['id']}",
        headers={**a["headers"], "X-Tenant-ID": b["company_id"]},
    )
    assert response.status_code == 403  # membership check rejects the spoofed claim


async def test_rls_policy_itself_blocks_cross_tenant_estimate_visibility(client):
    """Mirrors test_tenant_isolation_phase1.py's
    test_rls_policy_itself_blocks_cross_tenant_lead_visibility and this
    file's own
    test_rls_policy_itself_blocks_unrelated_tenant_catalog_item_visibility,
    adapted to `estimates` — the primary deliverable of this task. Connects
    as app_user directly (bypassing the FastAPI app, and therefore
    _get_estimate_or_404 entirely) to prove the POLICY itself, not
    application-layer filtering, blocks a genuinely unrelated tenant from
    seeing another tenant's estimate row. Then disables RLS as the table
    owner and confirms the identical query starts returning the row, showing
    the policy, not luck, was responsible. Then ALWAYS restores RLS in a
    finally, even if an assertion above fails partway through, so this test
    can never leave the database in an insecure state for any test that runs
    after it — same two-level try/finally discipline as
    test_rls_policy_regression.py, for the same reason (the restore itself
    is isolated so a failure in the ALTER doesn't get masked by a still-
    propagating AssertionError, and owner_conn is guaranteed closed either
    way). Migration 0007's docstring confirms `estimate_line_items` shares
    this exact policy shape, so this one proof is representative of both —
    see this section's own header comment for the "prove the mechanism once
    per policy-shape" rationale."""
    a = await _register_and_login(client, "Company A", "rls-est-a@acme.test")
    b = await _register_and_login(client, "Company B", "rls-est-b@acme.test")
    project_b = await _create_project(client, b["headers"])
    markup_b = await _create_markup_profile(client, b["headers"])
    estimate_b = await _create_estimate(
        client, b["headers"], project_id=project_b["id"], markup_profile_id=markup_b["id"]
    )
    estimate_b_id = estimate_b["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        # set_config(), not `SET app.current_tenant = $1` — see
        # set_current_tenant's docstring in app/db.py (Task 3) for why a
        # bound parameter there is a syntax error.
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
        )
        visible_as_a = await app_conn.fetchrow(
            "SELECT id FROM estimates WHERE id = $1", estimate_b_id
        )
        assert visible_as_a is None, (
            "RLS should block Company A's session from seeing Company B's "
            "estimate"
        )
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        await owner_conn.execute("ALTER TABLE estimates DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(APP_CONN_DSN)
        try:
            await app_conn2.execute(
                "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
            )
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM estimates WHERE id = $1", estimate_b_id
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's estimate row should exist "
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
            await owner_conn.execute("ALTER TABLE estimates ENABLE ROW LEVEL SECURITY")
        finally:
            await owner_conn.close()


# --- Task 2.15 review gap closure: POST /estimates/{id}/export --------------


async def test_export_estimate_pdf_forbidden_for_accountant_client_and_field_crew(client):
    """Closes the RBAC half of the gap Task 2.15's own spec-compliance
    review flagged: test_estimate_pdf_export.py only ever drove
    POST /estimates/{id}/export as admin, project_manager, and field_crew
    (test_export_estimate_pdf_forbidden_for_field_crew) — accountant and
    client, both in _READ_ROLES but absent from _WRITE_ROLES
    (app/routers/estimates.py), had never been driven through this specific
    route at all. field_crew is included in this same loop too — not because
    it needs new coverage (it's already proven in
    test_estimate_pdf_export.py), but to confirm all three non-write roles
    are rejected consistently by the same _WRITE_ROLES-gated dependency, in
    one place."""
    admin = await _register_and_login(client, "Acme Construction", "exp-rbac-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "exp-rbac-acct@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "exp-rbac-client@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "exp-rbac-crew@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )

    for actor in (accountant, client_role, field_crew):
        response = await client.post(
            f"/estimates/{estimate['id']}/export", headers=actor["headers"]
        )
        assert response.status_code == 403


async def test_export_estimate_pdf_genuinely_cross_tenant_returns_404(client):
    """Closes the 404 half of the gap Task 2.15's own spec-compliance review
    flagged: test_estimate_pdf_export.py's only 404 case
    (test_export_estimate_pdf_not_found_returns_404) uses the all-zeros
    nonexistent UUID, which never distinguishes "doesn't exist at all" from
    "exists, but belongs to someone else" — the latter is the case Inherited
    Invariant #8 (and _get_estimate_or_404's own docstring) says must be
    handled identically, but that identity had never actually been PROVEN
    for this specific route with a real, unrelated company's estimate id.
    This creates a genuine estimate under Company A and drives the export
    route as Company B."""
    a = await _register_and_login(client, "Company A", "exp-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "exp-cross-b@acme.test")
    project_a = await _create_project(client, a["headers"])
    markup_a = await _create_markup_profile(client, a["headers"])
    estimate_a = await _create_estimate(
        client, a["headers"], project_id=project_a["id"], markup_profile_id=markup_a["id"]
    )

    response = await client.post(f"/estimates/{estimate_a['id']}/export", headers=b["headers"])
    assert response.status_code == 404
