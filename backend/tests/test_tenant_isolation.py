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
        "token": body["access_token"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


async def test_company_a_can_read_its_own_company(client):
    a = await _register_and_login(client, "Company A", "admin-a@test.com")
    response = await client.get(f"/companies/{a['company_id']}", headers=a["headers"])
    assert response.status_code == 200
    assert response.json()["id"] == a["company_id"]


async def test_company_a_cannot_read_company_b_by_direct_id(client):
    a = await _register_and_login(client, "Company A", "admin-a@test.com")
    b = await _register_and_login(client, "Company B", "admin-b@test.com")

    response = await client.get(f"/companies/{b['company_id']}", headers=a["headers"])
    assert response.status_code == 404  # never 200, never leaks existence via a different code


async def test_company_a_cannot_impersonate_company_b_via_header(client):
    a = await _register_and_login(client, "Company A", "admin-a@test.com")
    b = await _register_and_login(client, "Company B", "admin-b@test.com")

    response = await client.get(
        f"/companies/{b['company_id']}",
        headers={**a["headers"], "X-Tenant-ID": b["company_id"]},
    )
    assert response.status_code == 403  # membership check rejects the spoofed claim


async def test_malformed_tenant_header_is_rejected(client):
    a = await _register_and_login(client, "Company A", "admin-a@test.com")

    response = await client.get(
        f"/companies/{a['company_id']}",
        headers={**a["headers"], "X-Tenant-ID": "not-a-uuid"},
    )
    assert response.status_code in (400, 401, 403, 422)  # must not be 200
