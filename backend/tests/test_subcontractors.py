"""Task 3.4: `POST/GET /subcontractors`, `GET /subcontractors/{id}`.

Helper duplication (`_register_and_login`/`_invite_and_login_as`) follows
the established per-test-file convention (see test_change_orders.py,
test_leads.py, test_projects.py) rather than sharing them via conftest.py.
"""


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


def _subcontractor_payload(**overrides):
    payload = {
        "name": "Ace Plumbing Co",
        "trade": "plumbing",
        "contact_email": "contact@aceplumbing.test",
    }
    payload.update(overrides)
    return payload


async def _create_subcontractor(client, actor, **overrides):
    return await client.post(
        "/subcontractors", json=_subcontractor_payload(**overrides), headers=actor["headers"]
    )


# --- Create: happy path + RBAC ---------------------------------------------


async def test_admin_can_create_subcontractor(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-admin-create@acme.test")

    response = await _create_subcontractor(client, admin)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["company_id"] == admin["company_id"]
    assert body["name"] == "Ace Plumbing Co"
    assert body["trade"] == "plumbing"
    assert body["contact_email"] == "contact@aceplumbing.test"
    assert "id" in body and "created_at" in body


async def test_create_subcontractor_optional_fields_may_be_omitted(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-admin-optional@acme.test")

    payload = {"name": "Bare Bones Electric"}
    response = await client.post("/subcontractors", json=payload, headers=admin["headers"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Bare Bones Electric"
    assert body["trade"] is None
    assert body["contact_email"] is None


async def test_project_manager_cannot_create_subcontractor(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-pm-403@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "sub-pm-403-pm@acme.test")

    response = await _create_subcontractor(client, pm)
    assert response.status_code == 403


async def test_accountant_cannot_create_subcontractor(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-acct-403@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "sub-acct-403-a@acme.test")

    response = await _create_subcontractor(client, accountant)
    assert response.status_code == 403


async def test_field_crew_cannot_create_subcontractor(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-fc-403@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "sub-fc-403-fc@acme.test")

    response = await _create_subcontractor(client, field_crew)
    assert response.status_code == 403


async def test_client_cannot_create_subcontractor(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-client-403@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "sub-client-403-c@acme.test")

    response = await _create_subcontractor(client, client_role)
    assert response.status_code == 403


# --- List: happy path + RBAC ------------------------------------------------


async def test_admin_pm_accountant_can_list_subcontractors(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-list-rbac-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "sub-list-rbac-pm@acme.test")
    accountant = await _invite_and_login_as(
        client, admin, "accountant", "sub-list-rbac-acct@acme.test"
    )
    create = await _create_subcontractor(client, admin)
    assert create.status_code == 201, create.text

    for actor in (admin, pm, accountant):
        response = await client.get("/subcontractors", headers=actor["headers"])
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1


async def test_field_crew_cannot_list_subcontractors(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-list-fc-403@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "sub-list-fc-403-fc@acme.test")

    response = await client.get("/subcontractors", headers=field_crew["headers"])
    assert response.status_code == 403


async def test_client_cannot_list_subcontractors(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-list-client-403@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "sub-list-client-403-c@acme.test"
    )

    response = await client.get("/subcontractors", headers=client_role["headers"])
    assert response.status_code == 403


async def test_list_subcontractors_empty_returns_empty_list(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-list-empty@acme.test")

    response = await client.get("/subcontractors", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


async def test_list_subcontractors_paginates_with_cursor(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-list-page@acme.test")

    created_ids = []
    for i in range(5):
        response = await _create_subcontractor(client, admin, name=f"Sub {i}")
        assert response.status_code == 201, response.text
        created_ids.append(response.json()["id"])

    seen_ids = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/subcontractors", params=params, headers=admin["headers"])
        assert response.status_code == 200, response.text
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


# --- Get-by-id: happy path + cross-tenant/nonexistent 404 ------------------


async def test_get_subcontractor_by_id_succeeds(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-get-ok@acme.test")
    create = await _create_subcontractor(client, admin)
    assert create.status_code == 201, create.text
    subcontractor_id = create.json()["id"]

    response = await client.get(f"/subcontractors/{subcontractor_id}", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == subcontractor_id
    assert body["name"] == "Ace Plumbing Co"


async def test_get_subcontractor_cross_tenant_returns_404(client):
    a = await _register_and_login(client, "Company A", "sub-get-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "sub-get-cross-b@acme.test")
    create = await _create_subcontractor(client, b)
    assert create.status_code == 201, create.text
    subcontractor_id = create.json()["id"]

    response = await client.get(f"/subcontractors/{subcontractor_id}", headers=a["headers"])
    assert response.status_code == 404


async def test_get_subcontractor_nonexistent_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "sub-get-nonexistent@acme.test")

    response = await client.get(
        "/subcontractors/00000000-0000-0000-0000-000000000000", headers=admin["headers"]
    )
    assert response.status_code == 404
