"""Task 1.12: `POST/GET /projects`, `GET /projects/{id}`, `PATCH /projects/{id}`.

This file predates the Task 1.14 phases/tasks routes, so it seeds `phases`
and `tasks` rows directly via the RLS-exempt owner connection rather than
through the API — the same "test setup, not a runtime code path" rationale
test_tenant_isolation_phase1.py uses for `_insert_lead_directly`. Now that
those routes exist (see test_phases_tasks.py), this direct-seeding approach
is kept here unchanged since these rows are incidental fixture data for
this file's own project-route tests, not what's under test.
"""
import uuid

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
    """Same pattern as test_leads.py's helper of the same name, extended to
    also return the invited user's id (queried directly — neither the
    invitation-accept nor the login response carries the new user's id, and
    this file needs it to seed `tasks.assignee_id` rows for the field_crew
    RBAC-scoping test)."""
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


async def _seed_phase(project_id, company_id, name="Foundation", sequence=0):
    phase_id = str(uuid.uuid4())
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO phases (id, project_id, company_id, name, sequence) VALUES ($1, $2, $3, $4, $5)",
            phase_id,
            project_id,
            company_id,
            name,
            sequence,
        )
    finally:
        await conn.close()
    return phase_id


async def _seed_task(phase_id, company_id, name="Pour footings", assignee_id=None, task_status="open"):
    task_id = str(uuid.uuid4())
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO tasks (id, phase_id, company_id, name, assignee_id, status) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            task_id,
            phase_id,
            company_id,
            name,
            uuid.UUID(assignee_id) if assignee_id else None,
            task_status,
        )
    finally:
        await conn.close()
    return task_id


# --- Create -------------------------------------------------------------


async def test_admin_can_create_project(client):
    admin = await _register_and_login(client, "Acme Construction", "admin@acme.test")

    response = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Kitchen Remodel"
    assert body["site_address"] == "123 Main St"
    assert body["status"] == "draft"
    assert body["company_id"] == admin["company_id"]
    assert body["lead_id"] is None


async def test_project_manager_can_create_project(client):
    admin = await _register_and_login(client, "Acme Construction", "pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "pm@acme.test")

    response = await client.post("/projects", json=_project_payload(), headers=pm["headers"])
    assert response.status_code == 201, response.text


async def test_create_project_rejects_invalid_payload(client):
    admin = await _register_and_login(client, "Acme Construction", "invalid-admin@acme.test")

    response = await client.post(
        "/projects",
        json=_project_payload(name="", site_address=""),
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_non_admin_pm_cannot_create_project(client):
    admin = await _register_and_login(client, "Acme Construction", "blocked-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "crew@acme.test")

    response = await client.post("/projects", json=_project_payload(), headers=field_crew["headers"])
    assert response.status_code == 403


# --- List -----------------------------------------------------------------


async def test_list_projects_returns_created_projects(client):
    admin = await _register_and_login(client, "Acme Construction", "list-admin@acme.test")
    await client.post("/projects", json=_project_payload(name="Kitchen"), headers=admin["headers"])
    await client.post("/projects", json=_project_payload(name="Bathroom"), headers=admin["headers"])

    response = await client.get("/projects", headers=admin["headers"])
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    names = {item["name"] for item in body["items"]}
    assert names == {"Kitchen", "Bathroom"}


async def test_list_projects_filters_by_status(client):
    admin = await _register_and_login(client, "Acme Construction", "filter-admin@acme.test")
    await client.post("/projects", json=_project_payload(name="A"), headers=admin["headers"])
    await client.post("/projects", json=_project_payload(name="B"), headers=admin["headers"])

    # All created projects start as status="draft".
    response_draft = await client.get("/projects", params={"status": "draft"}, headers=admin["headers"])
    assert response_draft.status_code == 200
    assert len(response_draft.json()["items"]) == 2

    response_active = await client.get("/projects", params={"status": "active"}, headers=admin["headers"])
    assert response_active.status_code == 200
    assert response_active.json()["items"] == []


async def test_list_projects_rejects_invalid_status_filter(client):
    admin = await _register_and_login(client, "Acme Construction", "badstatus-admin@acme.test")

    response = await client.get("/projects", params={"status": "not_a_status"}, headers=admin["headers"])
    assert response.status_code == 422


async def test_accountant_can_list_projects(client):
    admin = await _register_and_login(client, "Acme Construction", "acct-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "acct@acme.test")
    await client.post("/projects", json=_project_payload(), headers=admin["headers"])

    response = await client.get("/projects", headers=accountant["headers"])
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_client_can_list_projects_sanitized(client):
    """CRM+PM frontend spec, Decision 2 item 6 — deliberately REVERSES the
    earlier `test_client_cannot_list_projects` (403) decision: without a
    list route, `client` could GET a project by id but had no route that
    would ever tell them the id. The list serves `client` the same
    sanitized per-project shape the detail route already does."""
    admin = await _register_and_login(client, "Client List Co", "client-list-admin@acme.test")
    created = await client.post(
        "/projects",
        json={"name": "Visible To Client", "site_address": "1 Main St"},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text

    member = await _invite_and_login_as(client, admin, "client", "client-list-user@acme.test")
    listed = await client.get("/projects", headers=member["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "Visible To Client"
    # Sanitized shape: counts present, internal fields absent.
    assert item["phase_count"] == 0
    assert item["task_count"] == 0
    assert item["completed_task_count"] == 0
    assert "lead_id" not in item
    assert "company_id" not in item


async def test_list_projects_as_field_crew_shows_only_assigned(client):
    """The genuinely tricky RBAC-shape test: seeds one project with a task
    assigned to the field_crew user, and a second project whose only task is
    assigned to someone else (plus a third, fully unassigned task on the
    first project, to make sure the EXISTS join isn't accidentally requiring
    *every* task to be assigned to qualify). Confirms field_crew's list
    shows only the first project."""
    admin = await _register_and_login(client, "Acme Construction", "fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "fc@acme.test")
    other_worker = await _invite_and_login_as(client, admin, "field_crew", "fc-other@acme.test")

    assigned_project = await client.post(
        "/projects", json=_project_payload(name="Assigned To Me"), headers=admin["headers"]
    )
    unassigned_project = await client.post(
        "/projects", json=_project_payload(name="Not Mine"), headers=admin["headers"]
    )
    assigned_project_id = assigned_project.json()["id"]
    unassigned_project_id = unassigned_project.json()["id"]

    phase_a = await _seed_phase(assigned_project_id, admin["company_id"])
    await _seed_task(phase_a, admin["company_id"], name="My Task", assignee_id=field_crew["user_id"])
    await _seed_task(phase_a, admin["company_id"], name="Unassigned Task", assignee_id=None)

    phase_b = await _seed_phase(unassigned_project_id, admin["company_id"])
    await _seed_task(phase_b, admin["company_id"], name="Someone Else's Task", assignee_id=other_worker["user_id"])

    response = await client.get("/projects", headers=field_crew["headers"])
    assert response.status_code == 200
    body = response.json()
    project_ids = {item["id"] for item in body["items"]}
    assert project_ids == {assigned_project_id}


# --- Get single -------------------------------------------------------------


async def test_get_project_full_response_as_admin(client):
    admin = await _register_and_login(client, "Acme Construction", "get-admin@acme.test")
    create = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    project_id = create.json()["id"]

    response = await client.get(f"/projects/{project_id}", headers=admin["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == project_id
    assert body["company_id"] == admin["company_id"]
    assert body["lead_id"] is None
    assert "phase_count" not in body


async def test_get_project_client_dashboard_shape(client):
    """Verifies the client-role shape both structurally (omits `lead_id`/
    `company_id`, includes the three progress counts) AND numerically (the
    counts must be correct, not just present) — seeds 2 phases and 3 tasks
    (1 done, 2 not-done) and checks the exact numbers come back."""
    admin = await _register_and_login(client, "Acme Construction", "dash-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "dash-client@acme.test")

    create = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    project_id = create.json()["id"]

    phase_1 = await _seed_phase(project_id, admin["company_id"], name="Foundation", sequence=0)
    phase_2 = await _seed_phase(project_id, admin["company_id"], name="Framing", sequence=1)
    await _seed_task(phase_1, admin["company_id"], name="Pour footings", task_status="done")
    await _seed_task(phase_1, admin["company_id"], name="Backfill", task_status="open")
    await _seed_task(phase_2, admin["company_id"], name="Frame walls", task_status="in_progress")

    response = await client.get(f"/projects/{project_id}", headers=client_role["headers"])
    assert response.status_code == 200
    body = response.json()

    assert "lead_id" not in body
    assert "company_id" not in body
    assert body["id"] == project_id
    assert body["name"] == "Kitchen Remodel"
    assert body["status"] == "draft"
    assert body["site_address"] == "123 Main St"
    assert body["phase_count"] == 2
    assert body["task_count"] == 3
    assert body["completed_task_count"] == 1


async def test_get_project_client_dashboard_shape_with_zero_phases_and_tasks(client):
    """A freshly-created project (no phases/tasks seeded at all) must still
    return numeric 0s, not null or a validation error — proves the COUNT
    queries handle the empty case correctly rather than only the populated
    one above."""
    admin = await _register_and_login(client, "Acme Construction", "dash-zero-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "dash-zero-client@acme.test")

    create = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    project_id = create.json()["id"]

    response = await client.get(f"/projects/{project_id}", headers=client_role["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["phase_count"] == 0
    assert body["task_count"] == 0
    assert body["completed_task_count"] == 0


async def test_get_project_as_field_crew_with_assigned_task_succeeds(client):
    """Symmetric to test_list_projects_as_field_crew_shows_only_assigned,
    but for single-item GET — RBAC matrix's "Read assigned" for field_crew
    is unqualified, not list-route-only (see _get_project_or_404)."""
    admin = await _register_and_login(client, "Acme Construction", "get-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "get-fc@acme.test")

    create = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    project_id = create.json()["id"]
    phase = await _seed_phase(project_id, admin["company_id"])
    await _seed_task(phase, admin["company_id"], assignee_id=field_crew["user_id"])

    response = await client.get(f"/projects/{project_id}", headers=field_crew["headers"])
    assert response.status_code == 200
    assert response.json()["id"] == project_id


async def test_get_project_as_field_crew_without_assigned_task_returns_404(client):
    """The gap this fix closes: a field_crew user with no task on a project
    must not be able to fetch it by id, even though they CAN see other
    projects (via an assigned task elsewhere) — same 404 as a genuinely
    nonexistent project, not a 403, so existence isn't distinguishable."""
    admin = await _register_and_login(client, "Acme Construction", "get-fc2-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "get-fc2@acme.test")
    other_worker = await _invite_and_login_as(client, admin, "field_crew", "get-fc2-other@acme.test")

    not_mine = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    not_mine_id = not_mine.json()["id"]
    phase = await _seed_phase(not_mine_id, admin["company_id"])
    await _seed_task(phase, admin["company_id"], assignee_id=other_worker["user_id"])

    response = await client.get(f"/projects/{not_mine_id}", headers=field_crew["headers"])
    assert response.status_code == 404


async def test_get_project_as_field_crew_with_no_phases_returns_404(client):
    """A project with zero phases/tasks has no possible assignment, so it
    must 404 for field_crew rather than the EXISTS subquery accidentally
    evaluating true on an empty join."""
    admin = await _register_and_login(client, "Acme Construction", "get-fc3-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "get-fc3@acme.test")

    create = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    project_id = create.json()["id"]

    response = await client.get(f"/projects/{project_id}", headers=field_crew["headers"])
    assert response.status_code == 404


async def test_get_nonexistent_project_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "nonexistent-admin@acme.test")

    response = await client.get(
        "/projects/00000000-0000-0000-0000-000000000000", headers=admin["headers"]
    )
    assert response.status_code == 404


async def test_get_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "cross-b@acme.test")

    create = await client.post("/projects", json=_project_payload(), headers=b["headers"])
    project_id = create.json()["id"]

    response = await client.get(f"/projects/{project_id}", headers=a["headers"])
    assert response.status_code == 404


# --- Patch ------------------------------------------------------------------


async def test_admin_can_patch_project(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-admin@acme.test")
    create = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    project_id = create.json()["id"]

    response = await client.patch(
        f"/projects/{project_id}",
        json={"site_address": "456 New Ave", "name": "Kitchen Remodel v2"},
        headers=admin["headers"],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["site_address"] == "456 New Ave"
    assert body["name"] == "Kitchen Remodel v2"


async def test_patch_project_ignores_status_field(client):
    """ProjectPatchRequest has no `status` field (design decision #3) — a
    caller sending `status` here has no schema field for it to land in and
    it's silently ignored, not applied."""
    admin = await _register_and_login(client, "Acme Construction", "patch-status-admin@acme.test")
    create = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    project_id = create.json()["id"]

    response = await client.patch(
        f"/projects/{project_id}",
        json={"status": "active"},
        headers=admin["headers"],
    )
    assert response.status_code == 200
    assert response.json()["status"] == "draft"


async def test_non_admin_pm_cannot_patch_project(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-blocked-admin@acme.test")
    create = await client.post("/projects", json=_project_payload(), headers=admin["headers"])
    project_id = create.json()["id"]
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "patch-crew@acme.test")

    response = await client.patch(
        f"/projects/{project_id}",
        json={"name": "Hijacked"},
        headers=field_crew["headers"],
    )
    assert response.status_code == 403


async def test_patch_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "patch-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "patch-cross-b@acme.test")

    create = await client.post("/projects", json=_project_payload(), headers=b["headers"])
    project_id = create.json()["id"]

    response = await client.patch(
        f"/projects/{project_id}",
        json={"name": "Should Not Apply"},
        headers=a["headers"],
    )
    assert response.status_code == 404
