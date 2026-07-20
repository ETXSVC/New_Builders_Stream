"""CRM+PM frontend spec Decision 2 item 4: GET /tasks?assignee=me — the
cross-project task list behind the frontend's My Tasks view."""
import asyncpg

from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


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
    """Same pattern as test_projects.py's helper of the same name: also
    returns the invited user's id (queried directly — neither the
    invitation-accept nor the login response carries it), needed here to
    seed/assert `tasks.assignee_id` for the field_crew ownership tests."""
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


async def _project_with_phase(client, admin, name):
    project = await client.post(
        "/projects",
        json={"name": name, "site_address": "3 Main St"},
        headers=admin["headers"],
    )
    phase = await client.post(
        f"/projects/{project.json()['id']}/phases",
        json={"name": "Phase"},
        headers=admin["headers"],
    )
    return project.json(), phase.json()


async def test_my_tasks_returns_only_own_tasks_with_project_context(client):
    admin = await _register_and_login(client, "My Tasks Co", "my-tasks-admin@acme.test")
    crew = await _invite_and_login_as(client, admin, "field_crew", "my-tasks-crew@acme.test")
    project, phase = await _project_with_phase(client, admin, "Crew Project")

    mine = await client.post(
        f"/projects/{project['id']}/tasks",
        json={"name": "Mine", "phase_id": phase["id"], "assignee_id": crew["user_id"]},
        headers=admin["headers"],
    )
    assert mine.status_code == 201, mine.text
    unassigned = await client.post(
        f"/projects/{project['id']}/tasks",
        json={"name": "Not mine", "phase_id": phase["id"]},
        headers=admin["headers"],
    )
    assert unassigned.status_code == 201

    listed = await client.get("/tasks?assignee=me", headers=crew["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [t["name"] for t in items] == ["Mine"]
    assert items[0]["project_id"] == project["id"]
    assert items[0]["project_name"] == "Crew Project"


async def test_my_tasks_rejects_unsupported_assignee_value(client):
    admin = await _register_and_login(client, "My Tasks Val Co", "my-tasks-val@acme.test")
    response = await client.get("/tasks?assignee=someone-else", headers=admin["headers"])
    assert response.status_code == 422
