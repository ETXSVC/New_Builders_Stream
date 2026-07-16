"""Task 2.5: `POST/GET /catalogs/items` router tests
(`app/routers/catalogs.py`).

Every scenario here goes through real HTTP calls via the `client` fixture,
same discipline as `test_leads.py`/`test_tenant_isolation_phase1.py` —
direct owner-connection SQL is used ONLY for out-of-band setup the API
genuinely cannot do yet.

The one piece of out-of-band setup this file needs: giving a single admin
user membership in BOTH a parent company and a child branch it creates, so
that user can act as the child (via the `X-Tenant-ID` header, same spoofing
mechanism `test_tenant_isolation.py`'s sibling-branch test already relies
on) and create a real override there. `POST /companies/{id}/children`
(Phase 1) creates the child `companies` row but does NOT grant the creating
admin membership in it (confirmed by
`test_tenant_isolation.py::test_sibling_branches_cannot_see_each_other`'s own
comment: "The parent admin is a member of the parent company only") — and
nothing in Phase 0/1's API surface can invite a user into an
already-existing company by id (`POST /invitations` scopes the new
invitation to the CALLER's own active tenant, `current.company_id`, which
would itself require already having membership in the child to invite
someone into it — a chicken-and-egg problem outside this task's scope to
solve). `_add_membership_directly` closes that one gap directly via the
RLS-exempt owner connection, exactly the same category of "test-setup
plumbing unrelated to what's actually under test" `test_cost_catalog_inheritance.py`'s
module docstring already describes for its own company-hierarchy seeding.
"""
import asyncpg

from tests.conftest import TEST_DATABASE_URL, set_subscription_tier

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


async def _invite_and_login_as(client, admin, role, email):
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


async def _add_membership_directly(user_id, company_id, role):
    """See this module's docstring — closes a gap in the API surface (no
    route lets an existing user be added to a company they didn't register
    or get invited into) that has nothing to do with what this file tests."""
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
    parent admin membership in it directly (see module docstring), so the
    SAME admin token can act as either company by adding `X-Tenant-ID`."""
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


# =============================================================================
# Plain create
# =============================================================================


async def test_admin_can_create_catalog_item(client):
    admin = await _register_and_login(client, "Acme Construction", "admin@acme.test")

    response = await client.post("/catalogs/items", json=_item_payload(), headers=admin["headers"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["category"] == "framing"
    assert body["name"] == "2x4 Lumber"
    assert body["unit_rate"] == "5.00"
    assert body["company_id"] == admin["company_id"]
    assert body["parent_catalog_item_id"] is None
    assert body["is_override"] is False


async def test_project_manager_can_create_catalog_item(client):
    admin = await _register_and_login(client, "Acme Construction", "pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "pm@acme.test")

    response = await client.post("/catalogs/items", json=_item_payload(), headers=pm["headers"])
    assert response.status_code == 201, response.text


async def test_non_admin_pm_cannot_create_catalog_item(client):
    admin = await _register_and_login(client, "Acme Construction", "blocked-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "crew@acme.test")

    response = await client.post("/catalogs/items", json=_item_payload(), headers=field_crew["headers"])
    assert response.status_code == 403


async def test_accountant_cannot_create_catalog_item(client):
    """Accountant gets Read-only per the RBAC matrix's Estimation row, not
    write access."""
    admin = await _register_and_login(client, "Acme Construction", "acct-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "acct@acme.test")

    response = await client.post("/catalogs/items", json=_item_payload(), headers=accountant["headers"])
    assert response.status_code == 403


async def test_client_role_cannot_create_or_list_catalog_items(client):
    """The RBAC matrix's Estimation row gives `client` only "Approve/reject
    own estimate (e-sign)" — an estimate-specific grant, not any Cost Catalog
    access at all. Mirrors `test_markup_profiles.py`'s
    `test_client_role_cannot_list_markup_profiles` for this router's other
    resource."""
    admin = await _register_and_login(client, "Acme Construction", "client-role-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client-role@acme.test")

    create_response = await client.post(
        "/catalogs/items", json=_item_payload(), headers=client_role["headers"]
    )
    assert create_response.status_code == 403

    list_response = await client.get("/catalogs/items", headers=client_role["headers"])
    assert list_response.status_code == 403


async def test_accountant_can_list_catalog_items(client):
    admin = await _register_and_login(client, "Acme Construction", "acctlist-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "acctlist@acme.test")
    await client.post("/catalogs/items", json=_item_payload(), headers=admin["headers"])

    response = await client.get("/catalogs/items", headers=accountant["headers"])
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_create_catalog_item_rejects_invalid_payload(client):
    admin = await _register_and_login(client, "Acme Construction", "invalid-admin@acme.test")

    response = await client.post(
        "/catalogs/items", json=_item_payload(category=""), headers=admin["headers"]
    )
    assert response.status_code == 422


# =============================================================================
# Override create
# =============================================================================


async def test_create_override_of_parent_item_leaves_parent_unaffected(client):
    """US-4.6's explicit acceptance criterion, exercised at the HTTP layer
    (the resolution-service-level version already lives in
    test_cost_catalog_inheritance.py::test_parents_own_view_is_unaffected_by_childs_override)."""
    parent = await _register_and_login(client, "Parent Co", "parent-admin@acme.test")
    child_id = await _create_child_with_membership(client, parent, "Seattle Branch")

    parent_create = await client.post(
        "/catalogs/items",
        json=_item_payload(unit_rate="5.00"),
        headers=parent["headers"],
    )
    assert parent_create.status_code == 201, parent_create.text
    parent_item_id = parent_create.json()["id"]

    override = await client.post(
        f"/catalogs/items/{parent_item_id}/override",
        json=_item_payload(unit_rate="6.50"),
        headers={**parent["headers"], "X-Tenant-ID": child_id},
    )
    assert override.status_code == 201, override.text
    override_body = override.json()
    assert override_body["parent_catalog_item_id"] == parent_item_id
    assert override_body["company_id"] == child_id
    assert override_body["unit_rate"] == "6.50"
    assert override_body["is_override"] is True

    # The parent's own resolved view still shows its ORIGINAL row, not the
    # child's override leaking upward.
    parent_list = await client.get("/catalogs/items", headers=parent["headers"])
    assert parent_list.status_code == 200
    parent_items = parent_list.json()["items"]
    assert len(parent_items) == 1
    assert parent_items[0]["id"] == parent_item_id
    assert parent_items[0]["unit_rate"] == "5.00"


async def test_override_of_invisible_item_returns_404(client):
    """The bidirectional RLS policy's `WITH CHECK` only constrains the new
    row's own company_id, not the FK target's visibility — this is the
    application-layer check (`resolve_visible_catalog_items`) that fills
    that gap. Uses a genuinely UNRELATED tenant (no ancestor/descendant
    relationship at all) as the "cross-tenant 404 on direct catalog-item ID
    access" scenario the task spec calls for."""
    a = await _register_and_login(client, "Company A", "cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "cross-b@acme.test")

    b_item = await client.post("/catalogs/items", json=_item_payload(), headers=b["headers"])
    b_item_id = b_item.json()["id"]

    response = await client.post(
        f"/catalogs/items/{b_item_id}/override",
        json=_item_payload(unit_rate="9.99"),
        headers=a["headers"],
    )
    assert response.status_code == 404


async def test_override_of_nonexistent_item_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "nonexistent-admin@acme.test")

    response = await client.post(
        "/catalogs/items/00000000-0000-0000-0000-000000000000/override",
        json=_item_payload(),
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_grandchild_overriding_original_grandparent_id_returns_404_once_parent_has_overridden(
    client,
):
    """HTTP-layer companion to
    `test_cost_catalog_inheritance.py::test_grandchild_overriding_parents_override_sees_closest_not_grandparents_original`
    (which exercises `resolve_visible_catalog_items` directly) — this drives
    the actual `POST /catalogs/items/{id}/override` route through the same
    three-generation scenario, since the route's own `resolved_ids`
    membership check (app/routers/catalogs.py) has no dedicated regression
    test of its own. Once Mid has overridden Grandparent's item, Leaf's
    resolved view no longer contains the original grandparent id at all —
    only Mid's override id — so overriding via the original id must 404,
    while overriding via Mid's override id must succeed."""
    grandparent = await _register_and_login(client, "Grandparent Co", "grandparent-admin@acme.test")
    mid_id = await _create_child_with_membership(client, grandparent, "Mid Branch")
    leaf_id = await _create_child_with_membership(
        client,
        {
            "company_id": mid_id,
            "headers": {**grandparent["headers"], "X-Tenant-ID": mid_id},
            "user_id": grandparent["user_id"],
        },
        "Leaf Branch",
    )

    grandparent_create = await client.post(
        "/catalogs/items", json=_item_payload(unit_rate="5.00"), headers=grandparent["headers"]
    )
    grandparent_item_id = grandparent_create.json()["id"]

    mid_override = await client.post(
        f"/catalogs/items/{grandparent_item_id}/override",
        json=_item_payload(unit_rate="6.00"),
        headers={**grandparent["headers"], "X-Tenant-ID": mid_id},
    )
    assert mid_override.status_code == 201, mid_override.text
    mid_override_id = mid_override.json()["id"]

    via_original = await client.post(
        f"/catalogs/items/{grandparent_item_id}/override",
        json=_item_payload(unit_rate="7.00"),
        headers={**grandparent["headers"], "X-Tenant-ID": leaf_id},
    )
    assert via_original.status_code == 404

    via_mid_override = await client.post(
        f"/catalogs/items/{mid_override_id}/override",
        json=_item_payload(unit_rate="7.00"),
        headers={**grandparent["headers"], "X-Tenant-ID": leaf_id},
    )
    assert via_mid_override.status_code == 201, via_mid_override.text
    assert via_mid_override.json()["parent_catalog_item_id"] == mid_override_id


async def test_non_admin_pm_cannot_create_override(client):
    parent = await _register_and_login(client, "Parent Co", "override-blocked@acme.test")
    child_id = await _create_child_with_membership(client, parent, "Branch", role="field_crew")

    parent_create = await client.post("/catalogs/items", json=_item_payload(), headers=parent["headers"])
    parent_item_id = parent_create.json()["id"]

    response = await client.post(
        f"/catalogs/items/{parent_item_id}/override",
        json=_item_payload(unit_rate="7.00"),
        headers={**parent["headers"], "X-Tenant-ID": child_id},
    )
    assert response.status_code == 403


# =============================================================================
# GET /catalogs/items — resolved (deduped, override-preferred) list
# =============================================================================


async def test_list_shows_resolved_deduped_override_preferred_view(client):
    parent = await _register_and_login(client, "Parent Co", "list-parent@acme.test")
    child_id = await _create_child_with_membership(client, parent, "Branch")

    await client.post(
        "/catalogs/items",
        json=_item_payload(name="2x4 Lumber", unit_rate="5.00"),
        headers=parent["headers"],
    )
    concrete = await client.post(
        "/catalogs/items",
        json=_item_payload(name="Concrete Mix", category="concrete", unit_rate="120.00"),
        headers=parent["headers"],
    )

    # Child overrides only the lumber item, seen via X-Tenant-ID spoofing the
    # same real HTTP session into the child's tenant context.
    parent_items = (await client.get("/catalogs/items", headers=parent["headers"])).json()["items"]
    lumber_id = next(item["id"] for item in parent_items if item["name"] == "2x4 Lumber")
    override = await client.post(
        f"/catalogs/items/{lumber_id}/override",
        json=_item_payload(name="2x4 Lumber", unit_rate="6.50"),
        headers={**parent["headers"], "X-Tenant-ID": child_id},
    )
    assert override.status_code == 201, override.text
    override_id = override.json()["id"]

    child_list = await client.get(
        "/catalogs/items", headers={**parent["headers"], "X-Tenant-ID": child_id}
    )
    assert child_list.status_code == 200
    items = child_list.json()["items"]

    # Exactly two conceptual items — the child's own override (not the
    # parent's original lumber row) plus the parent's untouched concrete row.
    assert len(items) == 2
    by_name = {item["name"]: item for item in items}
    assert by_name["2x4 Lumber"]["id"] == override_id
    assert by_name["2x4 Lumber"]["unit_rate"] == "6.50"
    assert by_name["Concrete Mix"]["id"] == concrete.json()["id"]


async def test_list_filters_by_category_post_resolution(client):
    admin = await _register_and_login(client, "Acme Construction", "catfilter-admin@acme.test")
    await client.post(
        "/catalogs/items", json=_item_payload(name="2x4 Lumber", category="framing"), headers=admin["headers"]
    )
    await client.post(
        "/catalogs/items",
        json=_item_payload(name="Concrete Mix", category="concrete"),
        headers=admin["headers"],
    )

    response = await client.get(
        "/catalogs/items", params={"category": "concrete"}, headers=admin["headers"]
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "Concrete Mix"


async def test_list_filters_by_search_post_resolution(client):
    admin = await _register_and_login(client, "Acme Construction", "searchfilter-admin@acme.test")
    await client.post(
        "/catalogs/items", json=_item_payload(name="2x4 Lumber"), headers=admin["headers"]
    )
    await client.post(
        "/catalogs/items", json=_item_payload(name="Concrete Mix", category="concrete"), headers=admin["headers"]
    )

    response = await client.get(
        "/catalogs/items", params={"search": "lumber"}, headers=admin["headers"]
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["name"] == "2x4 Lumber"

    # Case-insensitive
    response_upper = await client.get(
        "/catalogs/items", params={"search": "LUMBER"}, headers=admin["headers"]
    )
    assert len(response_upper.json()["items"]) == 1


async def test_list_catalog_items_is_tenant_scoped(client):
    a = await _register_and_login(client, "Company A", "list-tenant-a@acme.test")
    b = await _register_and_login(client, "Company B", "list-tenant-b@acme.test")

    await client.post("/catalogs/items", json=_item_payload(name="A's Item"), headers=a["headers"])
    await client.post("/catalogs/items", json=_item_payload(name="B's Item"), headers=b["headers"])

    response = await client.get("/catalogs/items", headers=a["headers"])
    assert response.status_code == 200
    names = {item["name"] for item in response.json()["items"]}
    assert names == {"A's Item"}


async def test_list_catalog_items_pagination_walks_every_row_exactly_once(client):
    admin = await _register_and_login(client, "Acme Construction", "page-admin@acme.test")

    created_ids = []
    for i in range(5):
        response = await client.post(
            "/catalogs/items", json=_item_payload(name=f"Item {i}"), headers=admin["headers"]
        )
        created_ids.append(response.json()["id"])

    seen_ids = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/catalogs/items", params=params, headers=admin["headers"])
        assert response.status_code == 200
        body = response.json()
        pages += 1
        assert len(body["items"]) <= 2
        seen_ids.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 10

    assert pages == 3
    assert sorted(seen_ids) == sorted(created_ids)
    assert len(seen_ids) == len(set(seen_ids))


async def test_list_catalog_items_pagination_combines_with_category_filter(client):
    """`_paginate_resolved_items` sorts/paginates AFTER `list_catalog_items`
    has already applied the `category` filter (app/routers/catalogs.py) —
    this walks a cursor across several pages of a FILTERED result set and
    confirms every matching item is seen exactly once, with no non-matching
    "noise" item ever leaking into a page."""
    admin = await _register_and_login(client, "Acme Construction", "filterpage-admin@acme.test")

    matching_ids = []
    for i in range(5):
        response = await client.post(
            "/catalogs/items",
            json=_item_payload(name=f"Framing Item {i}", category="framing"),
            headers=admin["headers"],
        )
        matching_ids.append(response.json()["id"])
    for i in range(3):
        await client.post(
            "/catalogs/items",
            json=_item_payload(name=f"Concrete Item {i}", category="concrete"),
            headers=admin["headers"],
        )

    seen_ids = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2, "category": "framing"}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/catalogs/items", params=params, headers=admin["headers"])
        assert response.status_code == 200
        body = response.json()
        pages += 1
        assert len(body["items"]) <= 2
        assert all(item["category"] == "framing" for item in body["items"])
        seen_ids.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 10

    assert sorted(seen_ids) == sorted(matching_ids)
    assert len(seen_ids) == len(set(seen_ids))


async def test_list_catalog_items_rejects_malformed_cursor(client):
    admin = await _register_and_login(client, "Acme Construction", "badcursor-admin@acme.test")

    response = await client.get(
        "/catalogs/items", params={"cursor": "not-a-real-cursor"}, headers=admin["headers"]
    )
    assert response.status_code == 400
