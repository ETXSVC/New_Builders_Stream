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
    # Tier gating (Task 5.5): these suites exercise Enterprise-gated
    # accounting routes; registration can only produce trialing/pro.
    await set_subscription_tier(register.json()["company_id"], "enterprise")
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def _create_project(client, headers):
    response = await client.post(
        "/projects", json={"name": "Expense Project", "site_address": "1 Main St", "status": "active"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_and_list_expenses(client):
    admin = await _register_and_login(client, "Expense Co", "expense-1@example.test")
    project = await _create_project(client, admin["headers"])

    create = await client.post(
        f"/projects/{project['id']}/expenses",
        json={"description": "Lumber run", "amount": "125.50", "incurred_on": "2026-08-01"},
        headers=admin["headers"],
    )
    assert create.status_code == 201, create.text

    listing = await client.get(f"/projects/{project['id']}/expenses", headers=admin["headers"])
    assert listing.status_code == 200, listing.text
    assert len(listing.json()["items"]) == 1
    assert listing.json()["items"][0]["description"] == "Lumber run"


async def test_project_manager_cannot_create_expense(client):
    admin = await _register_and_login(client, "Expense Co 2", "expense-2@example.test")
    project = await _create_project(client, admin["headers"])
    invite = await client.post(
        "/invitations", json={"email": "pm-expense@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-expense@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.post(
        f"/projects/{project['id']}/expenses",
        json={"description": "x", "amount": "1.00", "incurred_on": "2026-08-01"},
        headers=pm_headers,
    )
    assert response.status_code == 403


async def _invite_and_login_as(client, admin, role, email):
    invite = await client.post(
        "/invitations", json={"email": email, "role": role}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Invited User", "password": "supersecret123"},
    )
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def test_client_cannot_create_expense(client):
    """Expenses' RBAC (require_role("admin", "accountant"), expenses.py)
    excludes client the same way Bills' AP row does — test_bills.py proves
    this directly for Bills (test_client_cannot_read_bills,
    test_client_cannot_read_bill_detail); this closes the equivalent gap
    for Expenses, which was otherwise only indirectly implied by the shared
    _ROLES tuple, never directly tested."""
    admin = await _register_and_login(client, "Expense Co 3", "expense-3@example.test")
    project = await _create_project(client, admin["headers"])
    client_role = await _invite_and_login_as(client, admin, "client", "client-expense-3@example.test")

    response = await client.post(
        f"/projects/{project['id']}/expenses",
        json={"description": "x", "amount": "1.00", "incurred_on": "2026-08-01"},
        headers=client_role["headers"],
    )
    assert response.status_code == 403


async def test_client_cannot_list_expenses(client):
    admin = await _register_and_login(client, "Expense Co 4", "expense-4@example.test")
    project = await _create_project(client, admin["headers"])
    client_role = await _invite_and_login_as(client, admin, "client", "client-expense-4@example.test")

    response = await client.get(f"/projects/{project['id']}/expenses", headers=client_role["headers"])
    assert response.status_code == 403


async def test_project_manager_cannot_list_expenses(client):
    """list_expenses shares the identical require_role("admin", "accountant")
    dependency with create_expense (expenses.py) — the create-route negative
    above (test_project_manager_cannot_create_expense) never exercised the
    list route's own guard."""
    admin = await _register_and_login(client, "Expense Co 5", "expense-5@example.test")
    project = await _create_project(client, admin["headers"])
    pm = await _invite_and_login_as(client, admin, "project_manager", "pm-expense-5@example.test")

    response = await client.get(f"/projects/{project['id']}/expenses", headers=pm["headers"])
    assert response.status_code == 403


async def test_list_expenses_paginates_across_multiple_pages(client):
    """Regression coverage for the date/datetime cursor path: paginate()
    (app/core/pagination.py) type-hints created_at_col as a datetime column,
    but Expense.incurred_on is a plain Date — verified empirically during
    this task's own implementation to round-trip correctly through
    encode_cursor/decode_cursor's date.isoformat()/datetime.fromisoformat()
    pairing, but that path is only ever exercised once next_cursor is
    actually used to fetch a second page. test_create_and_list_expenses
    above only ever creates 1 row, so it never reaches this code path."""
    admin = await _register_and_login(client, "Expense Co 6", "expense-6@example.test")
    project = await _create_project(client, admin["headers"])

    for day, description in (("01", "Day 1"), ("02", "Day 2"), ("03", "Day 3")):
        response = await client.post(
            f"/projects/{project['id']}/expenses",
            json={"description": description, "amount": "10.00", "incurred_on": f"2026-08-{day}"},
            headers=admin["headers"],
        )
        assert response.status_code == 201, response.text

    page1 = await client.get(
        f"/projects/{project['id']}/expenses?limit=2", headers=admin["headers"]
    )
    assert page1.status_code == 200, page1.text
    page1_body = page1.json()
    assert len(page1_body["items"]) == 2
    assert page1_body["next_cursor"] is not None

    page2 = await client.get(
        f"/projects/{project['id']}/expenses?limit=2&cursor={page1_body['next_cursor']}",
        headers=admin["headers"],
    )
    assert page2.status_code == 200, page2.text
    page2_body = page2.json()
    assert len(page2_body["items"]) == 1
    assert page2_body["next_cursor"] is None

    all_descriptions = {item["description"] for item in page1_body["items"] + page2_body["items"]}
    assert all_descriptions == {"Day 1", "Day 2", "Day 3"}
