"""CRM+PM frontend spec Decision 2 item 2: GET /dashboard/summary."""
from datetime import date, timedelta

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


async def test_summary_counts_open_leads_active_projects_and_due_tasks(client):
    admin = await _register_and_login(client, "Summary Co", "summary-admin@acme.test")

    lead = await client.post(
        "/leads",
        json={
            "contact_name": "Open Lead",
            "project_name": "Open Lead Project",
            "email": "open-lead@acme.test",
            "project_type": "remodel",
        },
        headers=admin["headers"],
    )
    assert lead.status_code == 201

    project = await client.post(
        "/projects",
        json={"name": "Summary Project", "site_address": "5 Main St"},
        headers=admin["headers"],
    )
    project_id = project.json()["id"]
    for target in ("pre_construction", "active"):
        moved = await client.patch(
            f"/projects/{project_id}/status", json={"status": target}, headers=admin["headers"]
        )
        assert moved.status_code == 200, moved.text

    phase = await client.post(
        f"/projects/{project_id}/phases", json={"name": "P"}, headers=admin["headers"]
    )
    due_soon = await client.post(
        f"/projects/{project_id}/tasks",
        json={
            "name": "Due soon",
            "phase_id": phase.json()["id"],
            "due_date": (date.today() + timedelta(days=2)).isoformat(),
        },
        headers=admin["headers"],
    )
    assert due_soon.status_code == 201
    far_out = await client.post(
        f"/projects/{project_id}/tasks",
        json={
            "name": "Far out",
            "phase_id": phase.json()["id"],
            "due_date": (date.today() + timedelta(days=30)).isoformat(),
        },
        headers=admin["headers"],
    )
    assert far_out.status_code == 201

    summary = await client.get("/dashboard/summary", headers=admin["headers"])
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["open_leads"] == 1
    assert body["active_projects"] == 1
    assert body["tasks_due_this_week"] == 1


async def test_summary_denied_for_field_crew(client):
    admin = await _register_and_login(client, "Summary Deny Co", "summary-deny@acme.test")
    crew = await _invite_and_login_as(client, admin, "field_crew", "summary-crew@acme.test")
    response = await client.get("/dashboard/summary", headers=crew["headers"])
    assert response.status_code == 403
