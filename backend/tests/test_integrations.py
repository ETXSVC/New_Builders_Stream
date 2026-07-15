"""Task 4.7 (design spec Section 3): GET /integrations/{provider}/connect."""


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


async def test_connect_returns_a_fake_authorization_url(client):
    admin = await _register_and_login(client, "Integ Co 1", "integ-1@example.test")

    response = await client.get("/integrations/quickbooks/connect", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authorization_url"].startswith("https://quickbooks.fake-oauth.test/")
    assert "state=" in body["authorization_url"]


async def test_connect_rejects_an_unknown_provider(client):
    admin = await _register_and_login(client, "Integ Co 2", "integ-2@example.test")

    response = await client.get("/integrations/xero/connect", headers=admin["headers"])
    assert response.status_code == 422


async def test_project_manager_cannot_connect(client):
    admin = await _register_and_login(client, "Integ Co 3", "integ-3@example.test")
    invite = await client.post(
        "/invitations", json={"email": "pm-integ@example.test", "role": "project_manager"}, headers=admin["headers"]
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "PM User", "password": "supersecret123"},
    )
    pm_login = await client.post(
        "/auth/login", json={"email": "pm-integ@example.test", "password": "supersecret123"}
    )
    pm_headers = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}

    response = await client.get("/integrations/quickbooks/connect", headers=pm_headers)
    assert response.status_code == 403
