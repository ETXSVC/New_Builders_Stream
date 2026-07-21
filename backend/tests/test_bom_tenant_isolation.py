"""Tenant-isolation regression coverage for vendors, bom_lines,
bom_line_receipts — the same "company B cannot see/act on company A's
rows" proof every prior sub-project's own tenant-isolation test file
establishes for its own new tables (e.g. test_cost_catalog.py,
test_change_orders.py's isolation cases).
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


async def test_company_b_cannot_see_company_a_vendors(client):
    company_a = await _register_and_login(client, "Company A", "a-vendor@example.test")
    company_b = await _register_and_login(client, "Company B", "b-vendor@example.test")

    created = await client.post("/vendors", json={"name": "A's Vendor"}, headers=company_a["headers"])
    vendor_id = created.json()["id"]

    listed_by_b = await client.get("/vendors", headers=company_b["headers"])
    assert listed_by_b.json()["items"] == []

    patched_by_b = await client.patch(
        f"/vendors/{vendor_id}", json={"name": "Hijacked"}, headers=company_b["headers"]
    )
    assert patched_by_b.status_code == 404


async def test_company_b_cannot_see_or_act_on_company_a_materials(client):
    company_a = await _register_and_login(client, "Company A", "a-bom@example.test")
    company_b = await _register_and_login(client, "Company B", "b-bom@example.test")

    project_a = await client.post(
        "/projects", json={"name": "A's Project", "site_address": "1 A St"}, headers=company_a["headers"]
    )
    project_a_id = project_a.json()["id"]

    line = await client.post(
        f"/projects/{project_a_id}/materials",
        json={"description": "A's Lumber", "unit": "board_ft", "quantity": "10.00"},
        headers=company_a["headers"],
    )
    assert line.status_code == 201, line.text
    line_id = line.json()["id"]

    # Company B can't even see company A's project (RLS on projects), so
    # it 404s before ever reaching the materials table.
    listed_by_b = await client.get(f"/projects/{project_a_id}/materials", headers=company_b["headers"])
    assert listed_by_b.status_code == 404

    company_wide_by_b = await client.get("/materials", headers=company_b["headers"])
    assert company_wide_by_b.json()["items"] == []

    patched_by_b = await client.patch(
        f"/materials/{line_id}", json={"ordered": True}, headers=company_b["headers"]
    )
    assert patched_by_b.status_code == 404

    receipt_by_b = await client.post(
        f"/materials/{line_id}/receipts", json={"quantity": "1.00"}, headers=company_b["headers"]
    )
    assert receipt_by_b.status_code == 404


async def test_company_b_cannot_assign_company_a_vendor_to_its_own_material(client):
    company_a = await _register_and_login(client, "Company A", "a-cross@example.test")
    company_b = await _register_and_login(client, "Company B", "b-cross@example.test")

    vendor_a = await client.post("/vendors", json={"name": "A's Vendor"}, headers=company_a["headers"])
    vendor_a_id = vendor_a.json()["id"]

    project_b = await client.post(
        "/projects", json={"name": "B's Project", "site_address": "1 B St"}, headers=company_b["headers"]
    )
    line_b = await client.post(
        f"/projects/{project_b.json()['id']}/materials",
        json={"description": "B's Drywall", "unit": "sheet", "quantity": "5.00"},
        headers=company_b["headers"],
    )
    line_b_id = line_b.json()["id"]

    response = await client.patch(
        f"/materials/{line_b_id}", json={"vendor_id": vendor_a_id}, headers=company_b["headers"]
    )
    assert response.status_code == 404, "a vendor invisible under RLS must read as not-found, not 403"
