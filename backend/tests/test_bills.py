"""Task 3.41 (design spec Section 4): POST/GET /bills, GET /bills/{id}."""


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
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def _create_project(client, headers):
    response = await client.post(
        "/projects", json={"name": "Bill Project", "site_address": "1 Main St", "status": "active"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_subcontractor(client, headers):
    response = await client.post(
        "/subcontractors", json={"name": "Ace Plumbing", "trade": "plumbing"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_bill_against_a_subcontractor(client):
    admin = await _register_and_login(client, "Bill Co 1", "bill-1@example.test")
    project = await _create_project(client, admin["headers"])
    subcontractor = await _create_subcontractor(client, admin["headers"])

    response = await client.post(
        "/bills",
        json={"project_id": project["id"], "subcontractor_id": subcontractor["id"], "amount": "800.00"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "unpaid"
    assert body["outstanding_balance"] == "800.00"


async def test_create_bill_against_a_free_text_vendor_with_no_project(client):
    admin = await _register_and_login(client, "Bill Co 2", "bill-2@example.test")

    response = await client.post(
        "/bills", json={"vendor_name": "City Power & Light", "amount": "150.00"}, headers=admin["headers"]
    )
    assert response.status_code == 201, response.text
    assert response.json()["project_id"] is None


async def test_create_bill_with_neither_subcontractor_nor_vendor_name_returns_422(client):
    admin = await _register_and_login(client, "Bill Co 3", "bill-3@example.test")

    response = await client.post("/bills", json={"amount": "50.00"}, headers=admin["headers"])
    assert response.status_code == 422


async def test_project_manager_cannot_create_bill(client):
    admin = await _register_and_login(client, "Bill Co 4", "bill-4@example.test")
    invite = await client.post(
        "/invitations", json={"email": "pm-bill@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-bill@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.post(
        "/bills", json={"vendor_name": "Some Vendor", "amount": "50.00"}, headers=pm_headers
    )
    assert response.status_code == 403


async def test_client_cannot_read_bills(client):
    admin = await _register_and_login(client, "Bill Co 5", "bill-5@example.test")
    invite = await client.post(
        "/invitations", json={"email": "client-bill@example.test", "role": "client"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Client User", "password": "supersecret123"},
    )
    client_login = await client.post(
        "/auth/login", json={"email": "client-bill@example.test", "password": "supersecret123"}
    )
    client_headers = {"Authorization": f"Bearer {client_login.json()['access_token']}"}

    response = await client.get("/bills", headers=client_headers)
    assert response.status_code == 403


async def test_list_bills_filtered_by_project(client):
    admin = await _register_and_login(client, "Bill Co 6", "bill-6@example.test")
    project = await _create_project(client, admin["headers"])
    await client.post(
        "/bills", json={"project_id": project["id"], "vendor_name": "Vendor X", "amount": "10.00"}, headers=admin["headers"]
    )
    await client.post("/bills", json={"vendor_name": "Overhead Vendor", "amount": "20.00"}, headers=admin["headers"])

    scoped = await client.get(f"/bills?project_id={project['id']}", headers=admin["headers"])
    assert len(scoped.json()["items"]) == 1

    all_bills = await client.get("/bills", headers=admin["headers"])
    assert len(all_bills.json()["items"]) == 2


async def test_get_bill_detail_includes_empty_payments_list(client):
    admin = await _register_and_login(client, "Bill Co 7", "bill-7@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Y", "amount": "60.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    response = await client.get(f"/bills/{bill_id}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["payments"] == []


async def test_client_cannot_read_bill_detail(client):
    """GET /bills, GET /bills/{id}, and POST /bills all share the identical
    require_role("admin", "accountant") dependency (app/routers/bills.py) —
    test_client_cannot_read_bills already proves this for the list route,
    this proves it directly for the detail route too, rather than leaving
    get_bill's own RBAC enforcement only indirectly exercised."""
    admin = await _register_and_login(client, "Bill Co 8", "bill-8@example.test")
    create = await client.post(
        "/bills", json={"vendor_name": "Vendor Z", "amount": "40.00"}, headers=admin["headers"]
    )
    bill_id = create.json()["id"]

    invite = await client.post(
        "/invitations", json={"email": "client-bill-detail@example.test", "role": "client"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Client User", "password": "supersecret123"},
    )
    client_login = await client.post(
        "/auth/login", json={"email": "client-bill-detail@example.test", "password": "supersecret123"}
    )
    client_headers = {"Authorization": f"Bearer {client_login.json()['access_token']}"}

    response = await client.get(f"/bills/{bill_id}", headers=client_headers)
    assert response.status_code == 403


async def test_get_bill_detail_returns_404_for_nonexistent_bill(client):
    admin = await _register_and_login(client, "Bill Co 9", "bill-9@example.test")

    response = await client.get(
        "/bills/00000000-0000-0000-0000-000000000000", headers=admin["headers"]
    )
    assert response.status_code == 404
