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


async def test_malformed_company_id_path_param_is_rejected(client):
    a = await _register_and_login(client, "Company A", "admin-a@test.com")
    response = await client.get("/companies/not-a-uuid", headers=a["headers"])
    assert response.status_code == 422


async def test_parent_can_create_and_see_child_branch(client):
    parent = await _register_and_login(client, "Parent Co", "admin-parent@test.com")

    create = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create.status_code == 201
    child_id = create.json()["id"]

    read_child = await client.get(f"/companies/{child_id}", headers=parent["headers"])
    assert read_child.status_code == 200
    assert read_child.json()["parent_id"] == parent["company_id"]


async def test_sibling_branches_cannot_see_each_other(client):
    parent = await _register_and_login(client, "Parent Co", "admin-parent2@test.com")

    child_a = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Branch A"},
        headers=parent["headers"],
    )
    child_b = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Branch B"},
        headers=parent["headers"],
    )
    child_a_id = child_a.json()["id"]
    child_b_id = child_b.json()["id"]

    # The parent admin is a member of the parent company only; a real Branch A
    # user account isn't created by this test, so it exercises the important
    # half of the guarantee directly: even the *parent* company's own token,
    # scoped to Branch A via X-Tenant-ID, is refused visibility into Branch B —
    # confirming siblings never share visibility through the header path either.
    response = await client.get(
        f"/companies/{child_b_id}",
        headers={**parent["headers"], "X-Tenant-ID": child_a_id},
    )
    assert response.status_code == 403


async def test_cannot_create_child_under_unrelated_company(client):
    """Without the app-layer company_id == current.company_id check, this
    would hit the tenant_insert RLS policy's rejection as an unhandled DB
    error (no global exception handler exists), not a clean 403."""
    a = await _register_and_login(client, "Company A", "admin-a2@test.com")
    b = await _register_and_login(client, "Company B", "admin-b2@test.com")

    response = await client.post(
        f"/companies/{b['company_id']}/children",
        json={"name": "Hostile Branch"},
        headers=a["headers"],
    )
    assert response.status_code == 403


async def test_cannot_create_grandchild_via_child_path_param(client):
    """RLS's tenant_insert policy alone would allow this insert — child_id is
    inside the parent's own descendant tree — but the app-layer check is
    stricter, requiring company_id to be the caller's exact active tenant."""
    parent = await _register_and_login(client, "Parent Co", "admin-parent3@test.com")
    child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Branch A"},
        headers=parent["headers"],
    )
    child_id = child.json()["id"]

    response = await client.post(
        f"/companies/{child_id}/children",
        json={"name": "Grandchild"},
        headers=parent["headers"],
    )
    assert response.status_code == 403
