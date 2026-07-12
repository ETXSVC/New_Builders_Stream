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


# =============================================================================
# Task 2.24: Esignatures / Change Orders tenant-isolation regression tests
# =============================================================================
#
# Extends this file rather than starting a new one — same module (Phase 2
# tenant-isolation regression coverage), same helper shape
# (_register_and_login/_add_membership_directly/_create_child_with_membership),
# just a different pair of tables. Follows this file's own Task 2.16 section's
# append convention exactly: a new commented banner at the end, not a
# rewritten module docstring.
#
# Per migration 0006's (`esignatures`) and 0008's (`change_orders`) own module
# docstrings — both re-read as part of this task, not assumed — each is a
# plain, flat, company-scoped table with its OWN separate ordinary
# `tenant_isolation` policy: the same `get_all_descendant_ids()`-only shape
# `leads`/`projects`/`estimates`/`markup_profiles` use, NOT `cost_catalog_items`'
# bidirectional OR-of-two-clauses shape (0005). The sibling-branch upward-
# visibility probe this file's Task 2.6 section applies to `cost_catalog_items`
# is therefore NOT replicated here either, for the same reason the Task 2.16
# section already gives.
#
# What this section deliberately does NOT re-derive:
#   - `Esignature`'s own immutability/REVOKE guarantee (raw UPDATE/DELETE
#     rejected as app_user) and the `document_type` CHECK constraint —
#     covered by test_esignatures.py (Task 2.17/2.18). This section is
#     tenant-isolation ONLY.
#   - `change_orders`' own RBAC coverage (admin/PM write-gating,
#     client-only approve/reject, non-write-role rejection) and its state
#     machine (409 on illegal create/approve/reject, `send-for-signature`'s
#     no-mutation contract) — covered by test_change_orders.py (Tasks
#     2.21/2.22). Also tenant-isolation ONLY here.
#   - Cross-tenant 404 on `GET /esignatures/{id}` — already proven by
#     test_esignatures.py::test_get_esignature_cross_tenant_returns_404
#     (Task 2.18), which drives a genuinely unrelated Company A/Company B
#     pair through this exact route with no header-spoofing involved.
#     Checked here (judgment call #4 in this task's own spec) rather than
#     assumed: that test exists and covers exactly this scenario, so it is
#     cited rather than duplicated.
#   - Cross-tenant 404 on `POST /projects/{id}/change-orders` (create) and
#     `GET /projects/{id}/change-orders` (list) — already proven by
#     test_change_orders.py::test_create_change_order_cross_tenant_project_returns_404
#     and ::test_list_change_orders_cross_tenant_project_returns_404 (Task
#     2.21). Both of those go through `_get_project_or_404` — a DIFFERENT
#     code path than the one this section's own new
#     `test_change_order_send_for_signature_genuinely_cross_tenant_returns_404`
#     test below closes a gap for (see that test's own docstring).
#
# What IS new here:
#   - test_esignature_header_spoofing_via_x_tenant_id_is_blocked — the
#     X-Tenant-ID membership check confirmed explicitly on an
#     Esignatures-scoped route, mirroring this file's own
#     test_estimate_header_spoofing_via_x_tenant_id_is_blocked. Drives a
#     real Esignature into existence via the ChangeOrder approve flow
#     (judgment call #2 in this task's own spec: the lightest path anywhere
#     in this codebase to a real, persisted Esignature row — the Estimate
#     approval path needs a MarkupProfile, a CostCatalogItem, line items,
#     and a `calculate` call first; ChangeOrder approval needs none of
#     that).
#   - test_change_order_send_for_signature_header_spoofing_via_x_tenant_id_is_blocked —
#     the same X-Tenant-ID membership-check confirmation, on
#     `POST /change-orders/{id}/send-for-signature` (judgment call #5:
#     `change_orders` has no singular `GET /{id}` route, and
#     `send-for-signature` is `_WRITE_ROLES`-gated and — per Task 2.22 — a
#     pure validation gate with no side effects even on success, making it
#     safe to call without any state cleanup concern).
#   - test_change_order_send_for_signature_genuinely_cross_tenant_returns_404 —
#     closes a genuine coverage gap (judgment call #6, confirmed by reading
#     test_change_orders.py in full): its three existing 404 tests for this
#     route family
#     (test_send_for_signature_nonexistent_change_order_returns_404,
#     test_approve_nonexistent_change_order_returns_404,
#     test_reject_nonexistent_change_order_returns_404) all use the
#     all-zeros nonexistent UUID, never a real, unrelated company's
#     change_order id — so `_get_change_order_or_404`'s own cross-tenant
#     behavior (as distinct from `_get_project_or_404`'s, already proven for
#     create/list) had never actually been exercised. Closes here rather
#     than in test_change_orders.py, matching Task 2.16's own precedent of
#     closing Task 2.15's review gap in this same tenant-isolation file
#     rather than the router's own primary test file.
#   - test_rls_policy_itself_blocks_cross_tenant_change_order_visibility —
#     the ONE new RLS-disable/re-enable proof this task's spec calls for,
#     applied to `change_orders` (judgment call #7). Per the plan's own
#     text, this is meant to stand in as representative of the shared plain
#     policy shape across `esignatures`/`estimates`/`change_orders`/
#     `markup_profiles` — `estimates` already has its own such proof (this
#     file's Task 2.16 section, test_rls_policy_itself_blocks_cross_tenant_estimate_visibility),
#     which only left `esignatures`/`change_orders`/`markup_profiles`
#     unproven at the raw-policy layer. This task's own scope is explicitly
#     "one proof, on change_orders" — not esignatures, not a second one for
#     markup_profiles. **Discrepancy found and flagged rather than silently
#     assumed**: the plan's text asserts `markup_profiles` "already got its
#     own proof in Task 2.6" — checked directly (grepped
#     test_markup_profiles.py and every other test file for
#     `DISABLE ROW LEVEL SECURITY`/`markup_profiles`) and no such proof
#     exists anywhere in this codebase; test_markup_profiles.py's own
#     `test_list_markup_profiles_is_tenant_scoped` only proves app-layer
#     scoping, not the raw-policy-level guarantee. `markup_profiles`
#     therefore remains genuinely unproven at that layer after this task —
#     out of this task's own explicit scope to fix, so left as-is here, but
#     recorded for whoever picks up that gap next.
#   - test_parent_admin_can_see_child_branch_estimate_and_change_order and
#     test_sibling_branches_cannot_see_each_others_estimate_or_change_order
#     (judgment call #8) — the parent/child hierarchy visibility case for
#     `estimates`/`change_orders`, mirroring
#     test_tenant_isolation_phase1.py's Task 1.17
#     test_parent_admin_can_see_child_branch_leads/
#     test_sibling_branches_cannot_see_each_others_leads precedent. Built
#     through the REAL create routes (Project -> MarkupProfile -> Estimate,
#     Project -> ChangeOrder) acting as the child/sibling branch via
#     X-Tenant-ID header switching against a REAL company_users membership
#     row (_create_child_with_membership) — NOT header-spoofing, and NOT
#     phase1's `_insert_lead_directly` raw-SQL shortcut, which exists there
#     only because `leads` has few required fields; `estimates`/
#     `change_orders` have real FK dependency chains (Project,
#     MarkupProfile) far more naturally built through the real API, matching
#     this file's own Task 2.6 sibling-branch catalog test's approach.
#     Confirmed by reading test_estimates.py in full first: it already uses
#     X-Tenant-ID branch-switching for an unrelated purpose (child-branch
#     catalog-override rate resolution,
#     test_replace_line_items_uses_child_branch_override_rate) but has no
#     parent-sees-child or sibling-invisibility test for Estimate rows
#     themselves — a genuine gap, closed here.


async def _advance_project_to_active(client, headers, project_id):
    """Shortest legal transition path from a freshly created (`draft`)
    Project to `active` — copied from test_change_orders.py's own
    `_PRECONDITION_PATH["active"]` (`["pre_construction", "active"]`), the
    only path this section needs (Change Orders are only legal to create
    against an active Project)."""
    for step_status in ("pre_construction", "active"):
        response = await client.patch(
            f"/projects/{project_id}/status", json={"status": step_status}, headers=headers
        )
        assert response.status_code == 200, response.text


def _change_order_payload(**overrides):
    payload = {
        "description": "Add a skylight in the master bath",
        "cost_delta": "1500.00",
        "schedule_impact_days": 3,
    }
    payload.update(overrides)
    return payload


async def _create_change_order(client, headers, project_id, **overrides):
    response = await client.post(
        f"/projects/{project_id}/change-orders",
        json=_change_order_payload(**overrides),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _approve_change_order(client, headers, change_order_id, **overrides):
    """Identical shape to test_change_orders.py's `_approve_change_order`
    helper of the same name — duplicated rather than imported, matching
    this codebase's established each-test-file-owns-its-own-helper-set
    convention. This is (judgment call #2) the lightest path anywhere in
    this codebase to a real, persisted Esignature row."""
    payload = {
        "signer_name": "Jane Client",
        "signer_email": "jane-client@example.test",
    }
    payload.update(overrides)
    return await client.post(
        f"/change-orders/{change_order_id}/approve",
        data=payload,
        files={"signature_artifact": ("signature.png", b"fake-signature-bytes", "image/png")},
        headers=headers,
    )


# --- Header-spoofing, `esignatures` and `change_orders` ---------------------


async def test_esignature_header_spoofing_via_x_tenant_id_is_blocked(client):
    """Mirrors this file's own
    test_estimate_header_spoofing_via_x_tenant_id_is_blocked, through an
    Esignatures-scoped route. The X-Tenant-ID membership check in
    app/core/deps.py is route-agnostic and runs before any RLS policy is
    evaluated, so this is expected to already hold — confirming it
    explicitly here matters because /esignatures is its own router with its
    own dependency wiring, not yet exercised by any prior task's isolation
    coverage. Drives a real Esignature into existence via Company B's
    ChangeOrder approve flow (judgment call #2)."""
    a = await _register_and_login(client, "Company A", "spoof-esig-a@acme.test")
    b = await _register_and_login(client, "Company B", "spoof-esig-b@acme.test")
    client_b = await _invite_and_login_as(client, b, "client", "spoof-esig-b-client@acme.test")
    project_b = await _create_project(client, b["headers"])
    await _advance_project_to_active(client, b["headers"], project_b["id"])
    change_order_b = await _create_change_order(client, b["headers"], project_b["id"])
    approve = await _approve_change_order(client, client_b["headers"], change_order_b["id"])
    assert approve.status_code == 200, approve.text
    esignature_id = approve.json()["esignature_id"]

    response = await client.get(
        f"/esignatures/{esignature_id}",
        headers={**a["headers"], "X-Tenant-ID": b["company_id"]},
    )
    assert response.status_code == 403  # membership check rejects the spoofed claim


async def test_change_order_send_for_signature_header_spoofing_via_x_tenant_id_is_blocked(client):
    """Mirrors test_esignature_header_spoofing_via_x_tenant_id_is_blocked
    above, through `POST /change-orders/{id}/send-for-signature` instead —
    the only ID-addressed change_orders route available for this check
    (judgment call #5: no singular `GET /change-orders/{id}` route exists),
    and safe to call with no side effects even on success (Task 2.22's own
    "pure validation gate" design)."""
    a = await _register_and_login(client, "Company A", "spoof-co-a@acme.test")
    b = await _register_and_login(client, "Company B", "spoof-co-b@acme.test")
    project_b = await _create_project(client, b["headers"])
    await _advance_project_to_active(client, b["headers"], project_b["id"])
    change_order_b = await _create_change_order(client, b["headers"], project_b["id"])

    response = await client.post(
        f"/change-orders/{change_order_b['id']}/send-for-signature",
        headers={**a["headers"], "X-Tenant-ID": b["company_id"]},
    )
    assert response.status_code == 403  # membership check rejects the spoofed claim


async def test_change_order_send_for_signature_genuinely_cross_tenant_returns_404(client):
    """Closes the gap judgment call #6 identifies: test_change_orders.py's
    three 404 tests for the send-for-signature/approve/reject route family
    all use the all-zeros nonexistent UUID, never a real, unrelated
    company's change_order id — so `_get_change_order_or_404`'s own
    cross-tenant behavior (a distinct code path from `_get_project_or_404`,
    already proven for create/list) had never actually been exercised.
    Company A, with its own legitimate (non-spoofed) headers, attempts
    send-for-signature against Company B's real change_order id."""
    a = await _register_and_login(client, "Company A", "cross-co-sfs-a@acme.test")
    b = await _register_and_login(client, "Company B", "cross-co-sfs-b@acme.test")
    project_b = await _create_project(client, b["headers"])
    await _advance_project_to_active(client, b["headers"], project_b["id"])
    change_order_b = await _create_change_order(client, b["headers"], project_b["id"])

    response = await client.post(
        f"/change-orders/{change_order_b['id']}/send-for-signature",
        headers=a["headers"],
    )
    assert response.status_code == 404


# --- RLS-disable/re-enable proof, `change_orders` only (judgment call #7) ---


async def test_rls_policy_itself_blocks_cross_tenant_change_order_visibility(client):
    """Mirrors test_rls_policy_itself_blocks_cross_tenant_estimate_visibility
    (this file's own Task 2.16 section) exactly, adapted to
    `change_orders` — the ONE new RLS-disable/re-enable proof this task's
    spec calls for (see this section's own header comment for the
    "representative of the shared policy shape, and the markup_profiles
    discrepancy" rationale). Connects as app_user directly (bypassing the
    FastAPI app, and therefore `_get_change_order_or_404` entirely) to
    prove the POLICY itself, not application-layer filtering, blocks a
    genuinely unrelated tenant from seeing another tenant's change_order
    row. Then disables RLS as the table owner and confirms the identical
    query starts returning the row, showing the policy, not luck, was
    responsible. Then ALWAYS restores RLS in a finally, even if an
    assertion above fails partway through, so this test can never leave the
    database in an insecure state for any test that runs after it — same
    two-level try/finally discipline as every other RLS-disable/re-enable
    proof in this codebase."""
    a = await _register_and_login(client, "Company A", "rls-co-a@acme.test")
    b = await _register_and_login(client, "Company B", "rls-co-b@acme.test")
    project_b = await _create_project(client, b["headers"])
    await _advance_project_to_active(client, b["headers"], project_b["id"])
    change_order_b = await _create_change_order(client, b["headers"], project_b["id"])
    change_order_b_id = change_order_b["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        # set_config(), not `SET app.current_tenant = $1` — see
        # set_current_tenant's docstring in app/db.py (Task 3) for why a
        # bound parameter there is a syntax error.
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
        )
        visible_as_a = await app_conn.fetchrow(
            "SELECT id FROM change_orders WHERE id = $1", change_order_b_id
        )
        assert visible_as_a is None, (
            "RLS should block Company A's session from seeing Company B's "
            "change_order"
        )
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        await owner_conn.execute("ALTER TABLE change_orders DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(APP_CONN_DSN)
        try:
            await app_conn2.execute(
                "SELECT set_config('app.current_tenant', $1, false)", a["company_id"]
            )
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM change_orders WHERE id = $1", change_order_b_id
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's change_order row should "
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
            await owner_conn.execute("ALTER TABLE change_orders ENABLE ROW LEVEL SECURITY")
        finally:
            await owner_conn.close()


# --- Parent/child hierarchy visibility, `estimates` and `change_orders` -----
# (judgment call #8, Phase 1's Task 1.17 precedent) --------------------------


async def test_parent_admin_can_see_child_branch_estimate_and_change_order(client):
    """Task 2.7's/2.20's migrations gave `estimates`/`change_orders` the
    identical `get_all_descendant_ids()` tenant_isolation policy shape used
    for `leads`/`projects`, but nothing has exercised that mechanism for
    either table specifically until this task. Builds a real Project,
    MarkupProfile, Estimate, and ChangeOrder through the real API, acting AS
    the child branch (X-Tenant-ID, backed by a genuine company_users row
    from `_create_child_with_membership` — not header-spoofing). Then
    confirms the parent admin's own token — still scoped to the parent's
    own company_id, no header switching involved — can see both rows via
    `GET /estimates/{id}` and `GET /projects/{id}/change-orders` (list).
    Mirrors test_tenant_isolation_phase1.py's
    test_parent_admin_can_see_child_branch_leads."""
    parent = await _register_and_login(client, "Parent Co", "parent-hier-admin@acme.test")
    child_id = await _create_child_with_membership(client, parent, "Seattle Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}

    project = await _create_project(client, child_headers)
    await _advance_project_to_active(client, child_headers, project["id"])
    markup = await _create_markup_profile(client, child_headers)
    estimate = await _create_estimate(
        client, child_headers, project_id=project["id"], markup_profile_id=markup["id"]
    )
    change_order = await _create_change_order(client, child_headers, project["id"])

    get_estimate = await client.get(f"/estimates/{estimate['id']}", headers=parent["headers"])
    assert get_estimate.status_code == 200, get_estimate.text
    assert get_estimate.json()["id"] == estimate["id"]

    list_change_orders = await client.get(
        f"/projects/{project['id']}/change-orders", headers=parent["headers"]
    )
    assert list_change_orders.status_code == 200, list_change_orders.text
    ids = {item["id"] for item in list_change_orders.json()["items"]}
    assert change_order["id"] in ids


async def test_sibling_branches_cannot_see_each_others_estimate_or_change_order(client):
    """Grants the parent admin real company_users rows in BOTH sibling
    branches directly via SQL (`_create_child_with_membership`, twice) so
    X-Tenant-ID genuinely switches the active tenant context to either
    branch rather than merely attempting to spoof it — same setup this
    file's own Task 2.6 sibling-branch catalog test and
    test_tenant_isolation_phase1.py's
    test_sibling_branches_cannot_see_each_others_leads use. Each branch
    builds its own Project/MarkupProfile/Estimate/ChangeOrder through the
    real API. Acting as Branch A, Branch B's Estimate is invisible (404 via
    `GET /estimates/{id}`), and Branch B's Project — and therefore its
    Change Order list nested under it — is equally invisible (404): the
    project itself 404s before the change_orders table's own RLS check is
    ever reached, the same "nested resource 404s on the invisible parent"
    pattern every other nested route in this codebase follows.

    The ChangeOrder half additionally probes `POST
    /change-orders/{id}/send-for-signature` (`_get_change_order_or_404`)
    directly against Branch B's own change_order id, not just the
    list-nested-under-an-invisible-project path above — this is the same
    singular, ID-addressed route
    `test_change_order_send_for_signature_genuinely_cross_tenant_returns_404`
    already uses elsewhere in this file, and including it here brings the
    ChangeOrder half of this test to the same rigor as the Estimate half
    (which goes through `GET /estimates/{id}`'s own RLS-backed path
    directly, not just a nested list). Checked symmetrically in both
    directions."""
    parent = await _register_and_login(client, "Parent Co", "sib-hier-admin@acme.test")
    child_a_id = await _create_child_with_membership(client, parent, "Branch A")
    child_b_id = await _create_child_with_membership(client, parent, "Branch B")
    headers_a = {**parent["headers"], "X-Tenant-ID": child_a_id}
    headers_b = {**parent["headers"], "X-Tenant-ID": child_b_id}

    project_a = await _create_project(client, headers_a)
    await _advance_project_to_active(client, headers_a, project_a["id"])
    markup_a = await _create_markup_profile(client, headers_a)
    estimate_a = await _create_estimate(
        client, headers_a, project_id=project_a["id"], markup_profile_id=markup_a["id"]
    )
    change_order_a = await _create_change_order(client, headers_a, project_a["id"])

    project_b = await _create_project(client, headers_b)
    await _advance_project_to_active(client, headers_b, project_b["id"])
    markup_b = await _create_markup_profile(client, headers_b)
    estimate_b = await _create_estimate(
        client, headers_b, project_id=project_b["id"], markup_profile_id=markup_b["id"]
    )
    change_order_b = await _create_change_order(client, headers_b, project_b["id"])

    # --- Acting as Branch A: Branch B's rows are invisible. -----------------
    get_estimate_b_as_a = await client.get(f"/estimates/{estimate_b['id']}", headers=headers_a)
    assert get_estimate_b_as_a.status_code == 404

    list_change_orders_b_as_a = await client.get(
        f"/projects/{project_b['id']}/change-orders", headers=headers_a
    )
    assert list_change_orders_b_as_a.status_code == 404

    sfs_change_order_b_as_a = await client.post(
        f"/change-orders/{change_order_b['id']}/send-for-signature", headers=headers_a
    )
    assert sfs_change_order_b_as_a.status_code == 404

    # --- Symmetric: acting as Branch B, Branch A's rows are equally
    # invisible. ---------------------------------------------------------
    get_estimate_a_as_b = await client.get(f"/estimates/{estimate_a['id']}", headers=headers_b)
    assert get_estimate_a_as_b.status_code == 404

    list_change_orders_a_as_b = await client.get(
        f"/projects/{project_a['id']}/change-orders", headers=headers_b
    )
    assert list_change_orders_a_as_b.status_code == 404

    sfs_change_order_a_as_b = await client.post(
        f"/change-orders/{change_order_a['id']}/send-for-signature", headers=headers_b
    )
    assert sfs_change_order_a_as_b.status_code == 404
