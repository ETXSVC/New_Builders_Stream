import asyncpg
import pytest

from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL

APP_CONN_DSN = TEST_APP_DATABASE_URL.replace("+asyncpg", "")


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
    """Creates a real, non-admin membership through the actual invitation API
    (Phase 0 Task 14), matching test_leads.py's helper of the same name —
    used here for the RBAC tests below rather than inserting company_users
    rows directly via SQL."""
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


def _lead_payload(**overrides):
    payload = {
        "contact_name": "Jane Homeowner",
        "project_name": "Kitchen Remodel",
        "email": "jane@example.test",
        "phone": "555-0100",
        "project_type": "residential",
        "estimated_value": "15000.00",
        "notes": "Prefers morning calls",
    }
    payload.update(overrides)
    return payload


async def _create_lead(client, headers, **overrides):
    response = await client.post("/leads", json=_lead_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _comm_payload(**overrides):
    payload = {"channel": "call", "body": "Discussed timeline with the homeowner."}
    payload.update(overrides)
    return payload


NONEXISTENT_LEAD_ID = "00000000-0000-0000-0000-000000000000"


async def test_admin_can_create_and_list_communication_log(client):
    admin = await _register_and_login(client, "Acme Construction", "admin@acme.test")
    lead = await _create_lead(client, admin["headers"])

    response = await client.post(
        f"/leads/{lead['id']}/communications",
        json=_comm_payload(),
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["lead_id"] == lead["id"]
    assert body["company_id"] == admin["company_id"]
    assert body["channel"] == "call"
    assert body["body"] == "Discussed timeline with the homeowner."
    assert "author_id" in body

    listing = await client.get(f"/leads/{lead['id']}/communications", headers=admin["headers"])
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == body["id"]


async def test_project_manager_can_create_communication_log(client):
    admin = await _register_and_login(client, "Acme Construction", "pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "pm@acme.test")
    lead = await _create_lead(client, admin["headers"])

    response = await client.post(
        f"/leads/{lead['id']}/communications",
        json=_comm_payload(),
        headers=pm["headers"],
    )
    assert response.status_code == 201, response.text


async def test_create_communication_log_rejects_invalid_channel(client):
    admin = await _register_and_login(client, "Acme Construction", "invalid-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])

    response = await client.post(
        f"/leads/{lead['id']}/communications",
        json=_comm_payload(channel="carrier_pigeon"),
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_create_communication_log_rejects_empty_body(client):
    admin = await _register_and_login(client, "Acme Construction", "empty-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])

    response = await client.post(
        f"/leads/{lead['id']}/communications",
        json=_comm_payload(body=""),
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_non_admin_pm_cannot_create_communication_log(client):
    admin = await _register_and_login(client, "Acme Construction", "blocked-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "crew@acme.test")

    response = await client.post(
        f"/leads/{lead['id']}/communications",
        json=_comm_payload(),
        headers=field_crew["headers"],
    )
    assert response.status_code == 403


async def test_non_admin_pm_cannot_list_communication_logs(client):
    admin = await _register_and_login(client, "Acme Construction", "blocked-admin2@acme.test")
    lead = await _create_lead(client, admin["headers"])
    client_role = await _invite_and_login_as(client, admin, "client", "client@acme.test")

    response = await client.get(
        f"/leads/{lead['id']}/communications", headers=client_role["headers"]
    )
    assert response.status_code == 403


async def test_create_communication_log_under_nonexistent_lead_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "nonexistent-admin@acme.test")

    response = await client.post(
        f"/leads/{NONEXISTENT_LEAD_ID}/communications",
        json=_comm_payload(),
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_list_communication_logs_under_nonexistent_lead_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "nonexistent-admin2@acme.test")

    response = await client.get(
        f"/leads/{NONEXISTENT_LEAD_ID}/communications", headers=admin["headers"]
    )
    assert response.status_code == 404


async def test_create_communication_log_under_cross_tenant_lead_returns_404(client):
    """Mirrors test_leads.py's test_get_cross_tenant_lead_returns_404: a
    lead_id belonging to another tenant must 404, both so the caller can't
    discover its existence and — the property this task's spec calls out
    explicitly — so nobody can create a communication log under a lead_id
    they couldn't otherwise see via GET /leads/{id}."""
    a = await _register_and_login(client, "Company A", "cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "cross-b@acme.test")
    lead_b = await _create_lead(client, b["headers"])

    response = await client.post(
        f"/leads/{lead_b['id']}/communications",
        json=_comm_payload(),
        headers=a["headers"],
    )
    assert response.status_code == 404


async def test_list_communication_logs_under_cross_tenant_lead_returns_404(client):
    """Same as above for GET — a cross-tenant lead_id must 404, not return
    an empty list. An empty list would leak the asymmetric information that
    the lead_id exists but simply has no communications, which is worse
    than the uniform 404 pattern used everywhere else in this codebase."""
    a = await _register_and_login(client, "Company A", "cross-list-a@acme.test")
    b = await _register_and_login(client, "Company B", "cross-list-b@acme.test")
    lead_b = await _create_lead(client, b["headers"])
    seed = await client.post(
        f"/leads/{lead_b['id']}/communications", json=_comm_payload(), headers=b["headers"]
    )
    assert seed.status_code == 201, seed.text

    response = await client.get(
        f"/leads/{lead_b['id']}/communications", headers=a["headers"]
    )
    assert response.status_code == 404


async def test_list_communication_logs_is_chronological_oldest_first(client):
    """US-2.4: 'see a chronological history' — oldest-first is the natural
    reading order for a history log. Seeds three logs in order and confirms
    the list returns them in the same order they were created, not reversed
    and not by some other implicit ordering."""
    admin = await _register_and_login(client, "Acme Construction", "order-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])

    created_bodies = []
    for i in range(3):
        body_text = f"Entry number {i}"
        response = await client.post(
            f"/leads/{lead['id']}/communications",
            json=_comm_payload(body=body_text),
            headers=admin["headers"],
        )
        assert response.status_code == 201, response.text
        created_bodies.append(body_text)

    listing = await client.get(f"/leads/{lead['id']}/communications", headers=admin["headers"])
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [item["body"] for item in items] == created_bodies


async def test_list_communication_logs_pagination_walks_every_row_exactly_once(client):
    admin = await _register_and_login(client, "Acme Construction", "page-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])

    created_ids = []
    for i in range(5):
        response = await client.post(
            f"/leads/{lead['id']}/communications",
            json=_comm_payload(body=f"Entry {i}"),
            headers=admin["headers"],
        )
        created_ids.append(response.json()["id"])

    seen_ids = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(
            f"/leads/{lead['id']}/communications", params=params, headers=admin["headers"]
        )
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


async def test_communication_logs_are_immutable_at_the_database_level(client):
    """Proves the DB-level immutability mechanism itself, not just app-layer
    behavior — same discipline as test_rls_policy_regression.py and Phase
    0's Task 16 RLS regression tests. Connects as app_user directly
    (bypassing the API entirely) and attempts a raw UPDATE and a raw DELETE
    against a real communication_logs row, confirming Task 1.2's migration
    `REVOKE UPDATE, DELETE ON communication_logs FROM app_user` blocks both
    with a real permission-denied error from Postgres, not a silent no-op
    (e.g. an UPDATE/DELETE affecting 0 rows because of an RLS predicate,
    which would look superficially similar but proves nothing about the
    grant itself)."""
    admin = await _register_and_login(client, "Acme Construction", "immutable-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])
    create = await client.post(
        f"/leads/{lead['id']}/communications",
        json=_comm_payload(),
        headers=admin["headers"],
    )
    assert create.status_code == 201, create.text
    log_id = create.json()["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", admin["company_id"]
        )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app_conn.execute(
                "UPDATE communication_logs SET body = 'tampered' WHERE id = $1", log_id
            )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app_conn.execute("DELETE FROM communication_logs WHERE id = $1", log_id)
    finally:
        await app_conn.close()

    # Sanity check: the row is untouched (as the owner connection, which
    # bypasses the grant, would be able to see either way — this confirms
    # the UPDATE/DELETE attempts above didn't silently partially apply).
    owner_conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        row = await owner_conn.fetchrow(
            "SELECT body FROM communication_logs WHERE id = $1", log_id
        )
    finally:
        await owner_conn.close()
    assert row is not None
    assert row["body"] == "Discussed timeline with the homeowner."
