"""Task 2.5: `POST/GET /markup-profiles` router tests
(`app/routers/catalogs.py`). Plain company-scoped resource, no inheritance
concept — see `MarkupProfile`'s own model docstring and design decision #1's
closing note. Same real-HTTP-call discipline as `test_leads.py`.
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


def _profile_payload(**overrides):
    payload = {"name": "Standard Markup", "overhead_pct": "10.00", "profit_pct": "15.00"}
    payload.update(overrides)
    return payload


async def test_admin_can_create_markup_profile(client):
    admin = await _register_and_login(client, "Acme Construction", "admin@acme.test")

    response = await client.post("/markup-profiles", json=_profile_payload(), headers=admin["headers"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Standard Markup"
    assert body["overhead_pct"] == "10.00"
    assert body["profit_pct"] == "15.00"
    assert body["company_id"] == admin["company_id"]


async def test_project_manager_can_create_markup_profile(client):
    admin = await _register_and_login(client, "Acme Construction", "pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "pm@acme.test")

    response = await client.post("/markup-profiles", json=_profile_payload(), headers=pm["headers"])
    assert response.status_code == 201, response.text


async def test_create_markup_profile_uses_defaults_when_omitted(client):
    admin = await _register_and_login(client, "Acme Construction", "default-admin@acme.test")

    response = await client.post(
        "/markup-profiles", json={"name": "No Markup"}, headers=admin["headers"]
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["overhead_pct"] == "0.00" or body["overhead_pct"] == "0"
    assert body["profit_pct"] == "0.00" or body["profit_pct"] == "0"


async def test_non_admin_pm_cannot_create_markup_profile(client):
    admin = await _register_and_login(client, "Acme Construction", "blocked-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "crew@acme.test")

    response = await client.post(
        "/markup-profiles", json=_profile_payload(), headers=field_crew["headers"]
    )
    assert response.status_code == 403


async def test_accountant_cannot_create_markup_profile(client):
    admin = await _register_and_login(client, "Acme Construction", "acct-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "acct@acme.test")

    response = await client.post(
        "/markup-profiles", json=_profile_payload(), headers=accountant["headers"]
    )
    assert response.status_code == 403


async def test_accountant_can_list_markup_profiles(client):
    admin = await _register_and_login(client, "Acme Construction", "acctlist-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "acctlist@acme.test")
    await client.post("/markup-profiles", json=_profile_payload(), headers=admin["headers"])

    response = await client.get("/markup-profiles", headers=accountant["headers"])
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_client_role_cannot_list_markup_profiles(client):
    """The RBAC matrix's Estimation row gives `client` only "Approve/reject
    own estimate (e-sign)" — an estimate-specific grant, not a blanket Read
    of Cost Catalog/Markup Profile data."""
    admin = await _register_and_login(client, "Acme Construction", "client-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client@acme.test")

    response = await client.get("/markup-profiles", headers=client_role["headers"])
    assert response.status_code == 403


async def test_create_markup_profile_rejects_invalid_payload(client):
    admin = await _register_and_login(client, "Acme Construction", "invalid-admin@acme.test")

    response = await client.post(
        "/markup-profiles", json=_profile_payload(name=""), headers=admin["headers"]
    )
    assert response.status_code == 422


async def test_list_markup_profiles_returns_empty_page_when_none_created(client):
    admin = await _register_and_login(client, "Acme Construction", "empty-admin@acme.test")

    response = await client.get("/markup-profiles", headers=admin["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["next_cursor"] is None


async def test_list_markup_profiles_returns_created_profiles(client):
    admin = await _register_and_login(client, "Acme Construction", "list-admin@acme.test")
    await client.post(
        "/markup-profiles", json=_profile_payload(name="Residential"), headers=admin["headers"]
    )
    await client.post(
        "/markup-profiles", json=_profile_payload(name="Commercial"), headers=admin["headers"]
    )

    response = await client.get("/markup-profiles", headers=admin["headers"])
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None
    names = {item["name"] for item in body["items"]}
    assert names == {"Residential", "Commercial"}


async def test_list_markup_profiles_is_tenant_scoped(client):
    a = await _register_and_login(client, "Company A", "list-tenant-a@acme.test")
    b = await _register_and_login(client, "Company B", "list-tenant-b@acme.test")

    await client.post("/markup-profiles", json=_profile_payload(name="A's Profile"), headers=a["headers"])
    await client.post("/markup-profiles", json=_profile_payload(name="B's Profile"), headers=b["headers"])

    response = await client.get("/markup-profiles", headers=a["headers"])
    assert response.status_code == 200
    names = {item["name"] for item in response.json()["items"]}
    assert names == {"A's Profile"}


async def test_list_markup_profiles_pagination_walks_every_row_exactly_once(client):
    """Pages over the `id`-only cursor `_paginate_markup_profiles` uses
    (see app/routers/catalogs.py's module docstring for why) — this asserts
    the same "every row exactly once, no skips or duplicates" property
    test_leads.py's equivalent test asserts for the (created_at, id)
    composite cursor, just for a single-column cursor instead."""
    admin = await _register_and_login(client, "Acme Construction", "page-admin@acme.test")

    created_ids = []
    for i in range(5):
        response = await client.post(
            "/markup-profiles", json=_profile_payload(name=f"Profile {i}"), headers=admin["headers"]
        )
        created_ids.append(response.json()["id"])

    seen_ids = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/markup-profiles", params=params, headers=admin["headers"])
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


async def test_list_markup_profiles_pagination_default_limit_and_max(client):
    admin = await _register_and_login(client, "Acme Construction", "limit-admin@acme.test")
    await client.post("/markup-profiles", json=_profile_payload(), headers=admin["headers"])

    over_max = await client.get("/markup-profiles", params={"limit": 101}, headers=admin["headers"])
    assert over_max.status_code == 422

    zero_limit = await client.get("/markup-profiles", params={"limit": 0}, headers=admin["headers"])
    assert zero_limit.status_code == 422


async def test_list_markup_profiles_rejects_malformed_cursor(client):
    admin = await _register_and_login(client, "Acme Construction", "cursor-admin@acme.test")

    response = await client.get(
        "/markup-profiles", params={"cursor": "not-a-real-cursor"}, headers=admin["headers"]
    )
    assert response.status_code == 400
