"""POST/GET /projects/{id}/materials, GET /materials, PATCH /materials/{id},
POST /materials/{id}/receipts (app/routers/bom_lines.py).
"""

from tests.conftest import set_subscription_tier


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
    await set_subscription_tier(register.json()["company_id"], "pro")
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _create_project(client, headers, name="Kitchen Remodel"):
    response = await client.post(
        "/projects", json={"name": name, "site_address": "1 Main St"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_manual_create_and_list_project_materials(client):
    admin = await _register_and_login(client, "Acme Construction", "admin@acme.test")
    project = await _create_project(client, admin["headers"])

    create = await client.post(
        f"/projects/{project['id']}/materials",
        json={"description": "Drywall sheets", "unit": "sheet", "quantity": "20.00"},
        headers=admin["headers"],
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["description"] == "Drywall sheets"
    assert body["source"] == "manual"
    assert body["cost_catalog_item_id"] is None
    assert body["ordered"] is False
    assert body["quantity_received"] == "0" or body["quantity_received"] == "0.00"
    assert body["status"] == "needed"

    listed = await client.get(f"/projects/{project['id']}/materials", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) == 1


async def test_manual_create_against_unknown_project_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "unknown-admin@acme.test")

    response = await client.post(
        "/projects/00000000-0000-0000-0000-000000000000/materials",
        json={"description": "Ghost", "unit": "each", "quantity": "1.00"},
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_company_wide_materials_list_spans_projects(client):
    admin = await _register_and_login(client, "Acme Construction", "spans-admin@acme.test")
    project_a = await _create_project(client, admin["headers"], name="Job A")
    project_b = await _create_project(client, admin["headers"], name="Job B")

    for project, description in ((project_a, "Framing lumber"), (project_b, "Deck boards")):
        response = await client.post(
            f"/projects/{project['id']}/materials",
            json={"description": description, "unit": "each", "quantity": "5.00"},
            headers=admin["headers"],
        )
        assert response.status_code == 201, response.text

    listed = await client.get("/materials", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    descriptions = {item["description"] for item in listed.json()["items"]}
    assert descriptions == {"Framing lumber", "Deck boards"}


async def test_mark_ordered_and_assign_vendor(client):
    admin = await _register_and_login(client, "Acme Construction", "order-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    vendor = await client.post("/vendors", json={"name": "ABC Lumber"}, headers=admin["headers"])
    line = await client.post(
        f"/projects/{project['id']}/materials",
        json={"description": "Framing lumber", "unit": "board_ft", "quantity": "100.00"},
        headers=admin["headers"],
    )
    line_id = line.json()["id"]

    patched = await client.patch(
        f"/materials/{line_id}",
        json={"ordered": True, "vendor_id": vendor.json()["id"]},
        headers=admin["headers"],
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["ordered"] is True
    assert body["ordered_at"] is not None
    assert body["vendor_id"] == vendor.json()["id"]
    assert body["status"] == "ordered"


async def test_mark_ordered_twice_does_not_reset_timestamp(client):
    admin = await _register_and_login(client, "Acme Construction", "twice-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    line = await client.post(
        f"/projects/{project['id']}/materials",
        json={"description": "Drywall", "unit": "sheet", "quantity": "10.00"},
        headers=admin["headers"],
    )
    line_id = line.json()["id"]

    first = await client.patch(f"/materials/{line_id}", json={"ordered": True}, headers=admin["headers"])
    second = await client.patch(f"/materials/{line_id}", json={"ordered": True}, headers=admin["headers"])
    assert first.json()["ordered_at"] == second.json()["ordered_at"]


async def test_patch_unknown_vendor_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "badvendor-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    line = await client.post(
        f"/projects/{project['id']}/materials",
        json={"description": "Drywall", "unit": "sheet", "quantity": "10.00"},
        headers=admin["headers"],
    )
    line_id = line.json()["id"]

    response = await client.patch(
        f"/materials/{line_id}",
        json={"vendor_id": "00000000-0000-0000-0000-000000000000"},
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_record_receipt_updates_quantity_received_and_status(client):
    admin = await _register_and_login(client, "Acme Construction", "receipt-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    line = await client.post(
        f"/projects/{project['id']}/materials",
        json={"description": "Drywall", "unit": "sheet", "quantity": "20.00"},
        headers=admin["headers"],
    )
    line_id = line.json()["id"]

    partial = await client.post(
        f"/materials/{line_id}/receipts", json={"quantity": "8.00"}, headers=admin["headers"]
    )
    assert partial.status_code == 201, partial.text

    after_partial = await client.get(f"/projects/{project['id']}/materials", headers=admin["headers"])
    partial_body = after_partial.json()["items"][0]
    assert partial_body["quantity_received"] == "8.00"
    assert partial_body["status"] == "partially_received"

    full = await client.post(
        f"/materials/{line_id}/receipts", json={"quantity": "12.00"}, headers=admin["headers"]
    )
    assert full.status_code == 201, full.text

    after_full = await client.get(f"/projects/{project['id']}/materials", headers=admin["headers"])
    full_body = after_full.json()["items"][0]
    assert full_body["quantity_received"] == "20.00"
    assert full_body["status"] == "received"


async def test_starter_tier_cannot_create_material(client):
    admin = await _register_and_login(client, "Acme Construction", "starter-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    await set_subscription_tier(admin["company_id"], "starter")

    response = await client.post(
        f"/projects/{project['id']}/materials",
        json={"description": "Drywall", "unit": "sheet", "quantity": "1.00"},
        headers=admin["headers"],
    )
    assert response.status_code == 403
