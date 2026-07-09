"""Task 1.16: `POST /projects/{id}/daily-logs`, `GET /projects/{id}/daily-logs`.

Helper duplication (`_register_and_login`/`_invite_and_login_as`/
`_project_payload`) follows the established per-test-file convention (see
test_leads.py, test_projects.py, test_documents.py) rather than sharing
them via conftest.py.
"""
import asyncpg
import pytest

from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")
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

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        user_id = await conn.fetchval("SELECT id FROM users WHERE email = $1", email)
    finally:
        await conn.close()

    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}, "user_id": str(user_id)}


def _project_payload(**overrides):
    payload = {
        "name": "Kitchen Remodel",
        "site_address": "123 Main St",
        "projected_start_date": "2026-08-01",
    }
    payload.update(overrides)
    return payload


async def _create_project(client, admin, **overrides):
    response = await client.post("/projects", json=_project_payload(**overrides), headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _daily_log_payload(**overrides):
    payload = {
        "log_date": "2026-08-15",
        "weather": "Sunny, 75F",
        "notes": "Poured foundation footings, crew of 4.",
    }
    payload.update(overrides)
    return payload


async def _create_daily_log(client, actor, project_id, **overrides):
    return await client.post(
        f"/projects/{project_id}/daily-logs",
        json=_daily_log_payload(**overrides),
        headers=actor["headers"],
    )


async def _assign_field_crew_to_project(client, admin, project_id, field_crew_user_id):
    """Field crew's project visibility is scoped via an assigned task
    (_with_field_crew_scope in app/routers/projects.py), not a direct
    project-level grant — matches test_documents.py's own helper of the
    same name."""
    phase = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": "Foundation", "sequence": 0},
        headers=admin["headers"],
    )
    assert phase.status_code == 201, phase.text
    task = await client.post(
        f"/projects/{project_id}/tasks",
        json={
            "name": "Pour footings",
            "phase_id": phase.json()["id"],
            "assignee_id": field_crew_user_id,
        },
        headers=admin["headers"],
    )
    assert task.status_code == 201, task.text


# --- Create -------------------------------------------------------------


async def test_project_manager_can_create_daily_log(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "dl-pm@acme.test")
    project_id = await _create_project(client, admin)

    response = await _create_daily_log(client, pm, project_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["author_id"] == pm["user_id"]
    assert body["log_date"] == "2026-08-15"
    assert body["weather"] == "Sunny, 75F"
    assert body["notes"] == "Poured foundation footings, crew of 4."
    assert body["project_id"] == project_id
    assert body["company_id"] == admin["company_id"]


async def test_create_daily_log_with_optional_fields_omitted(client):
    # weather/notes are both `str | None` on DailyLogCreateRequest — confirm
    # omitting them round-trips as null, not an empty string or a 422.
    admin = await _register_and_login(client, "Acme Construction", "dl-optional-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.post(
        f"/projects/{project_id}/daily-logs",
        json={"log_date": "2026-08-15"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["weather"] is None
    assert body["notes"] is None


async def test_cannot_spoof_author_id_via_request_payload(client):
    """DailyLogCreateRequest has no author_id field at all, so this should
    be structurally impossible — verified empirically by passing an
    unexpected `author_id` in the raw JSON body and confirming it's simply
    ignored (Pydantic drops unknown fields by default) rather than accepted
    and used. This is a role-independent schema/router property, not an
    RBAC check — the actor here is a project_manager, not the `client`
    role (see test_client_cannot_create_daily_log below for that role's
    actual write-access test); named to avoid confusion with the `client`
    HTTP-test-client fixture."""
    admin = await _register_and_login(client, "Acme Construction", "dl-override-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "dl-override-pm@acme.test")
    project_id = await _create_project(client, admin)

    payload = _daily_log_payload()
    payload["author_id"] = "00000000-0000-0000-0000-000000000000"
    response = await client.post(
        f"/projects/{project_id}/daily-logs", json=payload, headers=pm["headers"]
    )
    assert response.status_code == 201, response.text
    assert response.json()["author_id"] == pm["user_id"]
    assert response.json()["author_id"] != "00000000-0000-0000-0000-000000000000"


async def test_field_crew_can_create_daily_log_for_assigned_project(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "dl-fc@acme.test")
    project_id = await _create_project(client, admin)
    await _assign_field_crew_to_project(client, admin, project_id, field_crew["user_id"])

    response = await _create_daily_log(client, field_crew, project_id)
    assert response.status_code == 201, response.text
    assert response.json()["author_id"] == field_crew["user_id"]


async def test_field_crew_cannot_create_daily_log_for_unassigned_project(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-fc-none-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "dl-fc-none@acme.test")
    project_id = await _create_project(client, admin)

    response = await _create_daily_log(client, field_crew, project_id)
    assert response.status_code == 404


async def test_client_cannot_create_daily_log(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-client-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "dl-client@acme.test")
    project_id = await _create_project(client, admin)

    response = await _create_daily_log(client, client_role, project_id)
    assert response.status_code == 403


async def test_accountant_cannot_create_daily_log(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-acct-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "dl-acct@acme.test")
    project_id = await _create_project(client, admin)

    response = await _create_daily_log(client, accountant, project_id)
    assert response.status_code == 403


# --- Immutability ---------------------------------------------------------


async def test_daily_logs_are_immutable_at_the_database_level(client):
    """Proves the DB-level immutability mechanism itself, not just app-layer
    behavior — same discipline as test_communication_logs.py's Task 1.7
    immutability test and Task 1.15's documents REVOKE. Connects as
    app_user directly (bypassing the API entirely) and attempts a raw
    UPDATE and a raw DELETE against a real daily_logs row, confirming the
    migration's `REVOKE UPDATE, DELETE ON daily_logs FROM app_user` (design
    decision #6) blocks both with a real permission-denied error from
    Postgres, not a silent no-op."""
    admin = await _register_and_login(client, "Acme Construction", "dl-immutable-admin@acme.test")
    project_id = await _create_project(client, admin)
    create = await _create_daily_log(client, admin, project_id)
    assert create.status_code == 201, create.text
    log_id = create.json()["id"]

    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", admin["company_id"]
        )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app_conn.execute(
                "UPDATE daily_logs SET notes = 'tampered' WHERE id = $1", log_id
            )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app_conn.execute("DELETE FROM daily_logs WHERE id = $1", log_id)
    finally:
        await app_conn.close()

    # Sanity check: the row is untouched (owner connection bypasses the
    # grant either way — this confirms the attempts above didn't partially
    # apply before raising).
    owner_conn = await asyncpg.connect(OWNER_DSN)
    try:
        row = await owner_conn.fetchrow("SELECT notes FROM daily_logs WHERE id = $1", log_id)
    finally:
        await owner_conn.close()
    assert row is not None
    assert row["notes"] == "Poured foundation footings, crew of 4."


# --- Cross-tenant -----------------------------------------------------------


async def test_create_daily_log_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "dl-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "dl-cross-b@acme.test")
    project_id = await _create_project(client, b)

    response = await _create_daily_log(client, a, project_id)
    assert response.status_code == 404


async def test_list_daily_logs_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "dl-list-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "dl-list-cross-b@acme.test")
    project_id = await _create_project(client, b)
    create = await _create_daily_log(client, b, project_id)
    assert create.status_code == 201, create.text

    response = await client.get(f"/projects/{project_id}/daily-logs", headers=a["headers"])
    assert response.status_code == 404


async def test_create_and_list_daily_log_nonexistent_project_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-nonexistent-admin@acme.test")
    nonexistent_project_id = "00000000-0000-0000-0000-000000000000"

    create = await _create_daily_log(client, admin, nonexistent_project_id)
    assert create.status_code == 404

    listing = await client.get(f"/projects/{nonexistent_project_id}/daily-logs", headers=admin["headers"])
    assert listing.status_code == 404


# --- List -------------------------------------------------------------------


async def test_list_daily_logs_returns_all_created(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-list-admin@acme.test")
    project_id = await _create_project(client, admin)

    first = await _create_daily_log(client, admin, project_id, log_date="2026-08-01", notes="Day 1")
    assert first.status_code == 201, first.text
    second = await _create_daily_log(client, admin, project_id, log_date="2026-08-02", notes="Day 2")
    assert second.status_code == 201, second.text
    third = await _create_daily_log(client, admin, project_id, log_date="2026-08-03", notes="Day 3")
    assert third.status_code == 201, third.text

    response = await client.get(f"/projects/{project_id}/daily-logs", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert "items" in body and "next_cursor" in body
    assert len(body["items"]) == 3
    notes = {item["notes"] for item in body["items"]}
    assert notes == {"Day 1", "Day 2", "Day 3"}
    assert body["next_cursor"] is None


async def test_list_daily_logs_empty_project_returns_empty_list(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-list-empty-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(f"/projects/{project_id}/daily-logs", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


async def test_list_daily_logs_paginates_with_cursor(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-list-page-admin@acme.test")
    project_id = await _create_project(client, admin)

    created_ids = []
    for day in range(1, 6):
        response = await _create_daily_log(
            client, admin, project_id, log_date=f"2026-08-{day:02d}", notes=f"Day {day}"
        )
        assert response.status_code == 201, response.text
        created_ids.append(response.json()["id"])

    seen_ids = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(
            f"/projects/{project_id}/daily-logs", params=params, headers=admin["headers"]
        )
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


# --- List: RBAC --------------------------------------------------------------


async def test_admin_pm_accountant_can_list_daily_logs(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-list-rbac-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "dl-list-rbac-pm@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "dl-list-rbac-acct@acme.test")
    project_id = await _create_project(client, admin)
    create = await _create_daily_log(client, admin, project_id)
    assert create.status_code == 201, create.text

    for actor in (admin, pm, accountant):
        response = await client.get(f"/projects/{project_id}/daily-logs", headers=actor["headers"])
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1


async def test_field_crew_can_list_daily_logs_for_assigned_project(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-list-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "dl-list-fc@acme.test")
    project_id = await _create_project(client, admin)
    await _assign_field_crew_to_project(client, admin, project_id, field_crew["user_id"])
    create = await _create_daily_log(client, admin, project_id)
    assert create.status_code == 201, create.text

    response = await client.get(f"/projects/{project_id}/daily-logs", headers=field_crew["headers"])
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 1


async def test_field_crew_cannot_list_daily_logs_for_unassigned_project(client):
    admin = await _register_and_login(client, "Acme Construction", "dl-list-fc-none-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "dl-list-fc-none@acme.test")
    project_id = await _create_project(client, admin)
    create = await _create_daily_log(client, admin, project_id)
    assert create.status_code == 201, create.text

    response = await client.get(f"/projects/{project_id}/daily-logs", headers=field_crew["headers"])
    assert response.status_code == 404


async def test_client_cannot_list_daily_logs(client):
    """Per design decision #8/list_documents's own precedent, `client` gets
    no list-shaped route at all — only the single sanitized GET
    /projects/{id} dashboard. GET /projects/{id}/daily-logs blocks `client`
    with a 403 at the require_role dependency layer, same as list_documents
    (see this router's docstring on list_daily_logs for the full
    justification of why _LIST_ROLES, not _GET_ROLES, is used here)."""
    admin = await _register_and_login(client, "Acme Construction", "dl-list-client-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "dl-list-client@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(f"/projects/{project_id}/daily-logs", headers=client_role["headers"])
    assert response.status_code == 403
