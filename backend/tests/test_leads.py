import asyncpg

from tests.conftest import TEST_DATABASE_URL


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
    """Creates a real, non-admin membership through the actual invitation
    API (Phase 0 Task 14) rather than inserting company_users rows directly
    via SQL — this branch already has an API-driven path to a non-admin/PM
    user (see test_invitations.py::test_non_admin_cannot_create_invitations),
    so the SQL-insert fallback test_deps.py used before that flow existed
    isn't needed here."""
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


async def test_admin_can_create_lead(client):
    admin = await _register_and_login(client, "Acme Construction", "admin@acme.test")

    response = await client.post("/leads", json=_lead_payload(), headers=admin["headers"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["contact_name"] == "Jane Homeowner"
    assert body["status"] == "new"
    assert body["company_id"] == admin["company_id"]
    assert body["estimated_value"] == "15000.00"


async def test_project_manager_can_create_lead(client):
    admin = await _register_and_login(client, "Acme Construction", "pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "pm@acme.test")

    response = await client.post("/leads", json=_lead_payload(), headers=pm["headers"])
    assert response.status_code == 201, response.text


async def test_create_lead_writes_an_audit_log_entry(client):
    admin = await _register_and_login(client, "Acme Construction", "audit-admin@acme.test")

    response = await client.post("/leads", json=_lead_payload(), headers=admin["headers"])
    lead_id = response.json()["id"]

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        rows = await conn.fetch(
            "SELECT action, entity_type, entity_id FROM audit_log WHERE company_id = $1",
            admin["company_id"],
        )
    finally:
        await conn.close()

    matching = [r for r in rows if r["action"] == "lead.created" and str(r["entity_id"]) == lead_id]
    assert len(matching) == 1
    assert matching[0]["entity_type"] == "lead"


async def test_create_lead_rejects_invalid_payload(client):
    admin = await _register_and_login(client, "Acme Construction", "invalid-admin@acme.test")

    response = await client.post(
        "/leads",
        json=_lead_payload(contact_name="J", email="not-an-email"),
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_non_admin_pm_cannot_create_lead(client):
    admin = await _register_and_login(client, "Acme Construction", "blocked-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "crew@acme.test")

    response = await client.post("/leads", json=_lead_payload(), headers=field_crew["headers"])
    assert response.status_code == 403


async def test_non_admin_pm_cannot_list_leads(client):
    admin = await _register_and_login(client, "Acme Construction", "blocked-admin2@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client@acme.test")

    response = await client.get("/leads", headers=client_role["headers"])
    assert response.status_code == 403


async def test_non_admin_pm_cannot_get_lead(client):
    admin = await _register_and_login(client, "Acme Construction", "blocked-admin3@acme.test")
    create = await client.post("/leads", json=_lead_payload(), headers=admin["headers"])
    lead_id = create.json()["id"]
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "crew2@acme.test")

    response = await client.get(f"/leads/{lead_id}", headers=field_crew["headers"])
    assert response.status_code == 403


async def test_list_leads_returns_created_leads(client):
    admin = await _register_and_login(client, "Acme Construction", "list-admin@acme.test")
    await client.post("/leads", json=_lead_payload(project_name="Kitchen"), headers=admin["headers"])
    await client.post("/leads", json=_lead_payload(project_name="Bathroom"), headers=admin["headers"])

    response = await client.get("/leads", headers=admin["headers"])
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None
    project_names = {item["project_name"] for item in body["items"]}
    assert project_names == {"Kitchen", "Bathroom"}


async def test_list_leads_filters_by_status(client):
    admin = await _register_and_login(client, "Acme Construction", "filter-admin@acme.test")
    await client.post("/leads", json=_lead_payload(project_name="A"), headers=admin["headers"])
    await client.post("/leads", json=_lead_payload(project_name="B"), headers=admin["headers"])

    # All leads are created with status="new" — filtering on that status
    # should return both; filtering on any other valid status returns none.
    response_new = await client.get("/leads", params={"status": "new"}, headers=admin["headers"])
    assert response_new.status_code == 200
    assert len(response_new.json()["items"]) == 2

    response_won = await client.get("/leads", params={"status": "won"}, headers=admin["headers"])
    assert response_won.status_code == 200
    assert response_won.json()["items"] == []


async def test_list_leads_rejects_invalid_status_filter(client):
    admin = await _register_and_login(client, "Acme Construction", "badstatus-admin@acme.test")

    response = await client.get("/leads", params={"status": "not_a_status"}, headers=admin["headers"])
    assert response.status_code == 422


async def test_list_leads_pagination_walks_every_row_exactly_once(client):
    """Seeds 5 leads and walks the list with limit=2 (3 pages: 2, 2, 1),
    following next_cursor each time, and asserts the full walk returns
    every seeded lead exactly once — no skips, no duplicates — which is the
    property offset-based pagination can silently violate under concurrent
    inserts (see app/core/pagination.py's module docstring)."""
    admin = await _register_and_login(client, "Acme Construction", "page-admin@acme.test")

    created_ids = []
    for i in range(5):
        response = await client.post(
            "/leads", json=_lead_payload(project_name=f"Project {i}"), headers=admin["headers"]
        )
        created_ids.append(response.json()["id"])

    seen_ids = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/leads", params=params, headers=admin["headers"])
        assert response.status_code == 200
        body = response.json()
        pages += 1
        assert len(body["items"]) <= 2
        seen_ids.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 10  # guard against an infinite loop if cursor logic regresses

    assert pages == 3
    assert sorted(seen_ids) == sorted(created_ids)
    assert len(seen_ids) == len(set(seen_ids))  # no duplicates


async def test_list_leads_pagination_breaks_ties_on_identical_created_at(client):
    """Directly forces two rows to share the exact same created_at (bypassing
    the ORM's default via a raw UPDATE through the RLS-exempt owner
    connection) to prove the (created_at, id) composite cursor — not
    created_at alone — is what makes pagination correct. Sequential HTTP
    requests almost always get distinct microsecond timestamps, which would
    let a created_at-only cursor pass this suite by accident; this test
    removes that accident by construction."""
    admin = await _register_and_login(client, "Acme Construction", "tie-admin@acme.test")

    first = await client.post(
        "/leads", json=_lead_payload(project_name="Tie A"), headers=admin["headers"]
    )
    second = await client.post(
        "/leads", json=_lead_payload(project_name="Tie B"), headers=admin["headers"]
    )
    first_id, second_id = first.json()["id"], second.json()["id"]

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        shared_created_at = await conn.fetchval(
            "SELECT created_at FROM leads WHERE id = $1", first_id
        )
        await conn.execute(
            "UPDATE leads SET created_at = $1 WHERE id = $2", shared_created_at, second_id
        )
        confirm = await conn.fetch(
            "SELECT DISTINCT created_at FROM leads WHERE id = ANY($1::uuid[])",
            [first_id, second_id],
        )
    finally:
        await conn.close()
    assert len(confirm) == 1  # sanity: both rows now share one exact timestamp

    seen_ids = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/leads", params=params, headers=admin["headers"])
        assert response.status_code == 200
        body = response.json()
        pages += 1
        seen_ids.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 10

    assert pages == 2
    assert sorted(seen_ids) == sorted([first_id, second_id])
    assert len(seen_ids) == len(set(seen_ids))


async def test_list_leads_pagination_resumes_after_cursor_row_deleted(client):
    """A cursor encodes a sort position (created_at, id), not a lookup of a
    specific row — app/core/pagination.py's paginate() is documented to
    resume cleanly if the row the cursor was issued for no longer exists by
    the time the next page is fetched. Deletes the first-page row directly
    (there's no DELETE /leads/{id} route — leads can't be deleted via the
    API by business rule, so this uses the RLS-exempt owner connection, the
    same pattern the tie-breaker test above uses) and confirms the second
    page still returns the remaining row rather than erroring or skipping."""
    admin = await _register_and_login(client, "Acme Construction", "resume-admin@acme.test")

    first = await client.post(
        "/leads", json=_lead_payload(project_name="Will Be Deleted"), headers=admin["headers"]
    )
    second = await client.post(
        "/leads", json=_lead_payload(project_name="Survives"), headers=admin["headers"]
    )
    first_id, second_id = first.json()["id"], second.json()["id"]

    page_one = await client.get("/leads", params={"limit": 1}, headers=admin["headers"])
    assert page_one.status_code == 200
    body = page_one.json()
    assert [item["id"] for item in body["items"]] == [first_id]
    cursor = body["next_cursor"]
    assert cursor is not None

    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        await conn.execute("DELETE FROM leads WHERE id = $1", first_id)
    finally:
        await conn.close()

    page_two = await client.get("/leads", params={"limit": 1, "cursor": cursor}, headers=admin["headers"])
    assert page_two.status_code == 200
    body_two = page_two.json()
    assert [item["id"] for item in body_two["items"]] == [second_id]
    assert body_two["next_cursor"] is None


async def test_list_leads_pagination_default_limit_and_max(client):
    admin = await _register_and_login(client, "Acme Construction", "limit-admin@acme.test")
    await client.post("/leads", json=_lead_payload(), headers=admin["headers"])

    over_max = await client.get("/leads", params={"limit": 101}, headers=admin["headers"])
    assert over_max.status_code == 422

    zero_limit = await client.get("/leads", params={"limit": 0}, headers=admin["headers"])
    assert zero_limit.status_code == 422


async def test_list_leads_rejects_malformed_cursor(client):
    admin = await _register_and_login(client, "Acme Construction", "cursor-admin@acme.test")

    response = await client.get("/leads", params={"cursor": "not-a-real-cursor"}, headers=admin["headers"])
    assert response.status_code == 400


async def test_get_own_lead(client):
    admin = await _register_and_login(client, "Acme Construction", "get-admin@acme.test")
    create = await client.post("/leads", json=_lead_payload(), headers=admin["headers"])
    lead_id = create.json()["id"]

    response = await client.get(f"/leads/{lead_id}", headers=admin["headers"])
    assert response.status_code == 200
    assert response.json()["id"] == lead_id


async def test_get_nonexistent_lead_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "nonexistent-admin@acme.test")

    response = await client.get(
        "/leads/00000000-0000-0000-0000-000000000000", headers=admin["headers"]
    )
    assert response.status_code == 404


async def test_get_cross_tenant_lead_returns_404(client):
    """Mirrors test_tenant_isolation.py's
    test_company_a_cannot_read_company_b_by_direct_id pattern exactly."""
    a = await _register_and_login(client, "Company A", "cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "cross-b@acme.test")

    create = await client.post("/leads", json=_lead_payload(), headers=b["headers"])
    lead_id = create.json()["id"]

    response = await client.get(f"/leads/{lead_id}", headers=a["headers"])
    assert response.status_code == 404  # never 200, never leaks existence via a different code


async def test_list_leads_is_tenant_scoped(client):
    a = await _register_and_login(client, "Company A", "list-tenant-a@acme.test")
    b = await _register_and_login(client, "Company B", "list-tenant-b@acme.test")

    await client.post("/leads", json=_lead_payload(project_name="A's Lead"), headers=a["headers"])
    await client.post("/leads", json=_lead_payload(project_name="B's Lead"), headers=b["headers"])

    response = await client.get("/leads", headers=a["headers"])
    assert response.status_code == 200
    project_names = {item["project_name"] for item in response.json()["items"]}
    assert project_names == {"A's Lead"}
