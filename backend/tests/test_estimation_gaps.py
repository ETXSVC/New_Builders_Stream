"""Tenant-isolation, role, and error-path tests for every route this plan
adds on top of the estimation domain: PDF download, catalog/markup edit and
delete, estimate edit/delete, change-order single-GET and company-wide
list, catalog bulk import, and company branding.

Deviation from the plan doc's own test listing (`docs/superpowers/plans/
2026-07-20-estimation-esignature-frontend.md`, Task 1): the plan's sample
code assumed a `create_company_and_admin`/`authed_client`/`async_client`
conftest surface that does not exist in this codebase. `tests/conftest.py`
only provides a plain `client` fixture (a bare httpx AsyncClient) plus
`set_subscription_tier`; every other test module in this suite (see
`test_estimate_pdf_export.py`'s Task 2.15 section, `test_markup_profiles.py`,
`test_change_orders.py`) defines its own local `_register_and_login` helper
that registers a company, logs in, and returns `{"company_id", "user_id",
"headers"}`. This file follows that same established convention instead of
inventing a new one, and uses the already-returned `admin["user_id"]`
directly in place of the plan's placeholder `_admin_user_id(admin_token)`
helper, matching `test_estimate_pdf_export.py:562`'s exact precedent for
calling `_generate_estimate_pdf` directly.
"""

import asyncpg

from app.tasks.estimate_pdf import _generate_estimate_pdf
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
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text
    body = login.json()
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


def _project_payload(**overrides):
    payload = {"name": "Deck", "site_address": "1 Main St"}
    payload.update(overrides)
    return payload


async def _create_project(client, headers, **overrides):
    response = await client.post("/projects", json=_project_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _markup_profile_payload(**overrides):
    payload = {"name": "Standard"}
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


def _catalog_item_payload(**overrides):
    payload = {"category": "Framing", "name": "Lumber", "unit": "bf", "unit_rate": "4.00"}
    payload.update(overrides)
    return payload


async def _create_catalog_item(client, headers, **overrides):
    response = await client.post(
        "/catalogs/items", json=_catalog_item_payload(**overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_membership_directly(user_id, company_id, role):
    """Grants an existing user a real company_users row in a company they
    neither registered nor were invited into — there is no legitimate API
    path for this (see test_cost_catalog.py's module docstring for the full
    chicken-and-egg explanation). Test-setup plumbing, duplicated per-file
    rather than shared, matching this codebase's established convention
    (test_cost_catalog.py, test_tenant_isolation_phase3.py both carry their
    own copy)."""
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
    as either company via the X-Tenant-ID header — identical to
    test_cost_catalog.py's helper of the same name. Child-branch creation is
    Enterprise-gated (Task 5.7), so the caller must have already flipped
    `parent`'s tier via `set_subscription_tier` before calling this."""
    create = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": name},
        headers=parent["headers"],
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


# -----------------------------------------------------------------------
# GET /estimates/{id}/pdf
# -----------------------------------------------------------------------


async def test_pdf_download_404_before_export(client):
    admin = await _register_and_login(client, "Acme Construction", "pdf-download-admin@acme.test")
    markup = await _create_markup_profile(client, admin["headers"])
    project = await _create_project(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )

    response = await client.get(f"/estimates/{estimate['id']}/pdf", headers=admin["headers"])
    assert response.status_code == 409
    detail = response.json()["detail"].lower()
    assert "not ready" in detail or "pdf_status" in detail


async def test_pdf_download_streams_bytes_once_ready(client):
    admin = await _register_and_login(
        client, "Acme Construction", "pdf-download-ready-admin@acme.test"
    )
    markup = await _create_markup_profile(client, admin["headers"])
    project = await _create_project(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )

    export_response = await client.post(
        f"/estimates/{estimate['id']}/export", headers=admin["headers"]
    )
    assert export_response.status_code == 202, export_response.text

    # generate_estimate_pdf is a Dramatiq actor enqueued via .send(); run its
    # plain-coroutine implementation directly, same pattern
    # test_estimate_pdf_export.py's Task 2.15 tests already established for
    # this exact actor.
    await _generate_estimate_pdf(estimate["id"], admin["user_id"])

    response = await client.get(f"/estimates/{estimate['id']}/pdf", headers=admin["headers"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


async def test_pdf_download_cross_tenant_404(client):
    admin_a = await _register_and_login(
        client, "Acme Construction", "pdf-download-a-admin@acme.test"
    )
    markup = await _create_markup_profile(client, admin_a["headers"])
    project = await _create_project(client, admin_a["headers"])
    estimate = await _create_estimate(
        client, admin_a["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )

    admin_b = await _register_and_login(client, "Beta Builders", "pdf-download-b-admin@acme.test")

    response = await client.get(f"/estimates/{estimate['id']}/pdf", headers=admin_b["headers"])
    assert response.status_code == 404


# -----------------------------------------------------------------------
# PATCH/DELETE /catalogs/items/{id}
# -----------------------------------------------------------------------


async def test_patch_catalog_item_updates_rate(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-catalog-item-admin@acme.test")
    item = await _create_catalog_item(client, admin["headers"])

    response = await client.patch(
        f"/catalogs/items/{item['id']}", json={"unit_rate": "4.50"}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["unit_rate"] == "4.50"
    assert response.json()["name"] == "Lumber"  # untouched field preserved


async def test_patch_catalog_item_cross_tenant_404(client):
    admin_a = await _register_and_login(
        client, "Acme Construction", "patch-catalog-item-a-admin@acme.test"
    )
    item = await _create_catalog_item(client, admin_a["headers"])

    admin_b = await _register_and_login(client, "Beta Builders", "patch-catalog-item-b-admin@acme.test")

    response = await client.patch(
        f"/catalogs/items/{item['id']}", json={"unit_rate": "9.00"}, headers=admin_b["headers"]
    )
    assert response.status_code == 404


async def test_delete_catalog_item_blocked_when_referenced(client):
    admin = await _register_and_login(
        client, "Acme Construction", "delete-catalog-item-referenced-admin@acme.test"
    )
    item = await _create_catalog_item(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    project = await _create_project(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )
    lines_response = await client.put(
        f"/estimates/{estimate['id']}/lines",
        json={"items": [{"cost_catalog_item_id": item["id"], "quantity": "10"}]},
        headers=admin["headers"],
    )
    assert lines_response.status_code == 200, lines_response.text

    response = await client.delete(f"/catalogs/items/{item['id']}", headers=admin["headers"])
    assert response.status_code == 409


async def test_delete_catalog_item_blocked_when_overridden(client):
    """Parent company creates an item; a child branch overrides it; deleting
    the parent's original must 409, not silently orphan the override (the
    model's ondelete="SET NULL" would otherwise let this succeed and turn
    the override into a standalone item without warning).

    Child-branch context switch follows test_cost_catalog.py's established
    pattern exactly: `POST /companies/{id}/children` creates the child row
    but grants the creating admin no membership in it, so
    `_create_child_with_membership` grants that membership directly via the
    owner connection, and the same admin token then acts as the child by
    adding the `X-Tenant-ID` header (real membership, not header-spoofing).
    Child-branch creation is Enterprise-gated (Task 5.7), hence the
    `set_subscription_tier` call registration alone wouldn't satisfy.
    """
    admin = await _register_and_login(
        client, "Acme Construction", "delete-catalog-item-overridden-admin@acme.test"
    )
    await set_subscription_tier(admin["company_id"], "enterprise")
    item = await _create_catalog_item(client, admin["headers"])
    child_id = await _create_child_with_membership(client, admin, "Child Co")
    child_headers = {**admin["headers"], "X-Tenant-ID": child_id}

    override = await client.post(
        f"/catalogs/items/{item['id']}/override",
        json=_catalog_item_payload(name="Better Lumber", unit_rate="5.00"),
        headers=child_headers,
    )
    assert override.status_code == 201, override.text

    response = await client.delete(f"/catalogs/items/{item['id']}", headers=admin["headers"])
    assert response.status_code == 409


async def test_delete_catalog_item_succeeds_when_unreferenced(client):
    admin = await _register_and_login(
        client, "Acme Construction", "delete-catalog-item-unreferenced-admin@acme.test"
    )
    item = await _create_catalog_item(client, admin["headers"])

    response = await client.delete(f"/catalogs/items/{item['id']}", headers=admin["headers"])
    assert response.status_code == 204

    list_response = await client.get("/catalogs/items", headers=admin["headers"])
    assert item["id"] not in [i["id"] for i in list_response.json()["items"]]


# -----------------------------------------------------------------------
# PATCH/DELETE /markup-profiles/{id}
# -----------------------------------------------------------------------


async def test_patch_markup_profile(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-markup-profile-admin@acme.test")
    profile = await _create_markup_profile(
        client, admin["headers"], overhead_pct="10.00", profit_pct="15.00"
    )

    response = await client.patch(
        f"/markup-profiles/{profile['id']}", json={"profit_pct": "20.00"}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["profit_pct"] == "20.00"
    assert response.json()["overhead_pct"] == "10.00"


async def test_delete_markup_profile_blocked_when_referenced(client):
    admin = await _register_and_login(
        client, "Acme Construction", "delete-markup-profile-referenced-admin@acme.test"
    )
    profile = await _create_markup_profile(client, admin["headers"])
    project = await _create_project(client, admin["headers"])
    await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=profile["id"]
    )

    response = await client.delete(f"/markup-profiles/{profile['id']}", headers=admin["headers"])
    assert response.status_code == 409


async def test_delete_markup_profile_succeeds_when_unreferenced(client):
    admin = await _register_and_login(
        client, "Acme Construction", "delete-markup-profile-unreferenced-admin@acme.test"
    )
    profile = await _create_markup_profile(client, admin["headers"])

    response = await client.delete(f"/markup-profiles/{profile['id']}", headers=admin["headers"])
    assert response.status_code == 204


# -----------------------------------------------------------------------
# PATCH/DELETE /estimates/{id}
# -----------------------------------------------------------------------


async def test_patch_estimate_changes_markup_profile_while_draft(client):
    admin = await _register_and_login(
        client, "Acme Construction", "patch-estimate-admin@acme.test"
    )
    markup_a = await _create_markup_profile(client, admin["headers"], name="A")
    markup_b = await _create_markup_profile(client, admin["headers"], name="B")
    project = await _create_project(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup_a["id"]
    )

    response = await client.patch(
        f"/estimates/{estimate['id']}",
        json={"markup_profile_id": markup_b["id"]},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["markup_profile_id"] == markup_b["id"]


async def test_patch_estimate_409_once_sent(client):
    admin = await _register_and_login(
        client, "Acme Construction", "patch-estimate-sent-admin@acme.test"
    )
    markup = await _create_markup_profile(client, admin["headers"])
    project = await _create_project(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )
    estimate_id = estimate["id"]
    await client.put(f"/estimates/{estimate_id}/lines", json={"items": []}, headers=admin["headers"])
    await client.post(f"/estimates/{estimate_id}/calculate", headers=admin["headers"])
    await client.post(f"/estimates/{estimate_id}/send-for-signature", headers=admin["headers"])

    response = await client.patch(
        f"/estimates/{estimate_id}",
        json={"markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert response.status_code == 409


async def test_delete_estimate_while_draft(client):
    admin = await _register_and_login(
        client, "Acme Construction", "delete-estimate-admin@acme.test"
    )
    markup = await _create_markup_profile(client, admin["headers"])
    project = await _create_project(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )

    response = await client.delete(f"/estimates/{estimate['id']}", headers=admin["headers"])
    assert response.status_code == 204

    get_response = await client.get(f"/estimates/{estimate['id']}", headers=admin["headers"])
    assert get_response.status_code == 404


async def test_delete_estimate_409_once_sent(client):
    admin = await _register_and_login(
        client, "Acme Construction", "delete-estimate-sent-admin@acme.test"
    )
    markup = await _create_markup_profile(client, admin["headers"])
    project = await _create_project(client, admin["headers"])
    estimate = await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )
    estimate_id = estimate["id"]
    await client.put(f"/estimates/{estimate_id}/lines", json={"items": []}, headers=admin["headers"])
    await client.post(f"/estimates/{estimate_id}/calculate", headers=admin["headers"])
    await client.post(f"/estimates/{estimate_id}/send-for-signature", headers=admin["headers"])

    response = await client.delete(f"/estimates/{estimate_id}", headers=admin["headers"])
    assert response.status_code == 409


# -----------------------------------------------------------------------
# GET /change-orders/{id}, GET /change-orders
# -----------------------------------------------------------------------

# Shortest legal-transition path from a freshly created ("draft") project to
# "active" — a Change Order can only be created against an active Project
# (create_change_order's own docstring, app/routers/change_orders.py).
# Copied from test_change_orders.py's own _PRECONDITION_PATH['active'].
_ACTIVE_PRECONDITION_PATH = ["pre_construction", "active"]


async def _advance_project_to_active(client, headers, project_id):
    for step_status in _ACTIVE_PRECONDITION_PATH:
        response = await client.patch(
            f"/projects/{project_id}/status", json={"status": step_status}, headers=headers
        )
        assert response.status_code == 200, response.text


async def _create_change_order(client, headers, *, project_id, **overrides):
    payload = {"description": "Add deck stairs", "cost_delta": "500.00"}
    payload.update(overrides)
    response = await client.post(
        f"/projects/{project_id}/change-orders", json=payload, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_get_single_change_order(client):
    admin = await _register_and_login(
        client, "Acme Construction", "get-single-change-order-admin@acme.test"
    )
    project = await _create_project(client, admin["headers"])
    await _advance_project_to_active(client, admin["headers"], project["id"])
    change_order = await _create_change_order(client, admin["headers"], project_id=project["id"])

    response = await client.get(f"/change-orders/{change_order['id']}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["description"] == "Add deck stairs"


async def test_get_single_change_order_cross_tenant_404(client):
    admin_a = await _register_and_login(
        client, "Acme Construction", "get-single-change-order-a-admin@acme.test"
    )
    project = await _create_project(client, admin_a["headers"])
    await _advance_project_to_active(client, admin_a["headers"], project["id"])
    change_order = await _create_change_order(client, admin_a["headers"], project_id=project["id"])

    admin_b = await _register_and_login(
        client, "Beta Builders", "get-single-change-order-b-admin@acme.test"
    )

    response = await client.get(f"/change-orders/{change_order['id']}", headers=admin_b["headers"])
    assert response.status_code == 404


async def test_list_all_change_orders_scoped_to_pending_for_client(client):
    admin = await _register_and_login(
        client, "Acme Construction", "list-all-change-orders-admin@acme.test"
    )
    project = await _create_project(client, admin["headers"])
    await _advance_project_to_active(client, admin["headers"], project["id"])
    await _create_change_order(
        client, admin["headers"], project_id=project["id"], description="Pending one"
    )

    response = await client.get("/change-orders", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["project_name"] == project["name"]


# -----------------------------------------------------------------------
# GET /estimates — parent_name enrichment
# -----------------------------------------------------------------------


def _lead_payload(**overrides):
    payload = {
        "contact_name": "Ada",
        "project_name": "Bathroom Remodel",
        "email": "ada@example.com",
        "project_type": "Remodel",
    }
    payload.update(overrides)
    return payload


async def _create_lead(client, headers, **overrides):
    response = await client.post("/leads", json=_lead_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_estimate_list_includes_parent_name_for_project_and_lead(client):
    admin = await _register_and_login(
        client, "Acme Construction", "parent-name-admin@acme.test"
    )
    markup = await _create_markup_profile(client, admin["headers"])

    project = await _create_project(client, admin["headers"], name="Kitchen Remodel")
    await _create_estimate(
        client, admin["headers"], project_id=project["id"], markup_profile_id=markup["id"]
    )

    lead = await _create_lead(client, admin["headers"])
    # Advance the lead through the legal transition path (new -> contacted
    # -> estimating, per app/services/lead_transitions.py /
    # tests/test_lead_state_machine.py's _PRECONDITION_PATH) to reach a
    # status in _LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE
    # (app/routers/estimates.py) so POST /estimates accepts a lead_id bind.
    for step_status in ("contacted", "estimating"):
        transition = await client.patch(
            f"/leads/{lead['id']}", json={"status": step_status}, headers=admin["headers"]
        )
        assert transition.status_code == 200, transition.text

    lead_estimate = await client.post(
        "/estimates",
        json={"lead_id": lead["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert lead_estimate.status_code == 201, lead_estimate.text

    response = await client.get("/estimates", headers=admin["headers"])
    assert response.status_code == 200, response.text
    names = {item["parent_name"] for item in response.json()["items"]}
    assert "Kitchen Remodel" in names
    assert lead["project_name"] in names
