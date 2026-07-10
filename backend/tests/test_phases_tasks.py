"""Task 1.14: `POST /projects/{id}/phases`, `POST /projects/{id}/tasks`,
`PATCH /tasks/{id}`.

Helper duplication (`_register_and_login`/`_invite_and_login_as`/
`_project_payload`) follows the established per-test-file convention (see
test_leads.py, test_projects.py) rather than sharing them via conftest.py.
"""
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


async def _create_phase(client, admin, project_id, name="Foundation", sequence=0):
    response = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": name, "sequence": sequence},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_task(client, admin, project_id, phase_id, name="Pour footings", assignee_id=None):
    payload = {"name": name, "phase_id": phase_id}
    if assignee_id is not None:
        payload["assignee_id"] = assignee_id
    response = await client.post(f"/projects/{project_id}/tasks", json=payload, headers=admin["headers"])
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- Phase creation ---------------------------------------------------------


async def test_admin_can_create_phase(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": "Foundation", "sequence": 1},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Foundation"
    assert body["sequence"] == 1
    assert body["project_id"] == project_id
    assert body["company_id"] == admin["company_id"]


async def test_project_manager_can_create_phase(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "phase-pm@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": "Framing"},
        headers=pm["headers"],
    )
    assert response.status_code == 201, response.text
    # sequence omitted → schema default 0.
    assert response.json()["sequence"] == 0


async def test_field_crew_cannot_create_phase(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "phase-fc@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": "Framing"},
        headers=field_crew["headers"],
    )
    assert response.status_code == 403


async def test_accountant_and_client_cannot_create_phase(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-acct-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "phase-acct@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "phase-client@acme.test")
    project_id = await _create_project(client, admin)

    for actor in (accountant, client_role):
        response = await client.post(
            f"/projects/{project_id}/phases",
            json={"name": "Framing"},
            headers=actor["headers"],
        )
        assert response.status_code == 403


async def test_create_phase_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "phase-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "phase-cross-b@acme.test")
    project_id = await _create_project(client, b)

    response = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": "Framing"},
        headers=a["headers"],
    )
    assert response.status_code == 404


async def test_create_phase_rejects_invalid_payload(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-invalid-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": ""},
        headers=admin["headers"],
    )
    assert response.status_code == 422


# --- Task creation -----------------------------------------------------------


async def test_admin_can_create_task(client):
    admin = await _register_and_login(client, "Acme Construction", "task-admin@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    response = await client.post(
        f"/projects/{project_id}/tasks",
        json={"name": "Pour footings", "phase_id": phase_id, "due_date": "2026-09-01"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "Pour footings"
    assert body["phase_id"] == phase_id
    assert body["company_id"] == admin["company_id"]
    assert body["status"] == "open"
    assert body["assignee_id"] is None
    assert body["due_date"] == "2026-09-01"


async def test_project_manager_can_create_task_with_assignee(client):
    admin = await _register_and_login(client, "Acme Construction", "task-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "task-pm@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "task-pm-fc@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    response = await client.post(
        f"/projects/{project_id}/tasks",
        json={"name": "Frame walls", "phase_id": phase_id, "assignee_id": field_crew["user_id"]},
        headers=pm["headers"],
    )
    assert response.status_code == 201, response.text
    assert response.json()["assignee_id"] == field_crew["user_id"]


async def test_field_crew_cannot_create_task(client):
    admin = await _register_and_login(client, "Acme Construction", "task-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "task-fc@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    response = await client.post(
        f"/projects/{project_id}/tasks",
        json={"name": "Frame walls", "phase_id": phase_id},
        headers=field_crew["headers"],
    )
    assert response.status_code == 403


async def test_create_task_rejects_cross_project_phase_id(client):
    """The genuinely tricky application-layer check: phase_id must belong
    to the SAME project as the path's project_id, not merely the same
    tenant. Seeds a phase under project B, then attempts to create a task
    under project A referencing project B's phase — both projects are in
    the SAME company, so this can't be caught by tenant isolation alone."""
    admin = await _register_and_login(client, "Acme Construction", "task-xproj-admin@acme.test")
    project_a = await _create_project(client, admin, name="Project A")
    project_b = await _create_project(client, admin, name="Project B")
    phase_b = await _create_phase(client, admin, project_b)

    response = await client.post(
        f"/projects/{project_a}/tasks",
        json={"name": "Should be rejected", "phase_id": phase_b},
        headers=admin["headers"],
    )
    assert response.status_code == 422, response.text


async def test_create_task_rejects_nonexistent_phase_id(client):
    admin = await _register_and_login(client, "Acme Construction", "task-noexist-admin@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.post(
        f"/projects/{project_id}/tasks",
        json={"name": "Should be rejected", "phase_id": "00000000-0000-0000-0000-000000000000"},
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_create_task_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "task-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "task-cross-b@acme.test")
    project_id = await _create_project(client, b)
    phase_id = await _create_phase(client, b, project_id)

    response = await client.post(
        f"/projects/{project_id}/tasks",
        json={"name": "Should be rejected", "phase_id": phase_id},
        headers=a["headers"],
    )
    assert response.status_code == 404


async def test_create_task_rejects_invalid_payload(client):
    admin = await _register_and_login(client, "Acme Construction", "task-invalid-admin@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    response = await client.post(
        f"/projects/{project_id}/tasks",
        json={"name": "", "phase_id": phase_id},
        headers=admin["headers"],
    )
    assert response.status_code == 422


# --- Task patch ---------------------------------------------------------------


async def test_admin_can_patch_any_task_field(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "patch-admin-fc@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(client, admin, project_id, phase_id)

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"status": "in_progress", "assignee_id": field_crew["user_id"]},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["assignee_id"] == field_crew["user_id"]


async def test_project_manager_can_patch_any_task(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "patch-pm@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(client, admin, project_id, phase_id)

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"status": "done"},
        headers=pm["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"


async def test_patch_task_rejects_invalid_status_value(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-badstatus-admin@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(client, admin, project_id, phase_id)

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"status": "not_a_status"},
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_field_crew_can_patch_status_on_own_assigned_task(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-fc-own-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "patch-fc-own@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(
        client, admin, project_id, phase_id, assignee_id=field_crew["user_id"]
    )

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"status": "in_progress"},
        headers=field_crew["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_progress"


async def test_field_crew_cannot_patch_assignee_id_even_on_own_task(client):
    """The task IS visible to them (it's assigned to them), but assignee_id
    isn't a field they're permitted to touch — this router's chosen design
    is an explicit 403, not a silent drop of the disallowed field (see
    patch_task's docstring in app/routers/tasks.py)."""
    admin = await _register_and_login(client, "Acme Construction", "patch-fc-assignee-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "patch-fc-assignee@acme.test")
    other_crew = await _invite_and_login_as(client, admin, "field_crew", "patch-fc-assignee-other@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(
        client, admin, project_id, phase_id, assignee_id=field_crew["user_id"]
    )

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"assignee_id": other_crew["user_id"]},
        headers=field_crew["headers"],
    )
    assert response.status_code == 403

    # Also blocked when bundled alongside a legal status change in the same
    # request — the whole request is rejected, not partially applied.
    response = await client.patch(
        f"/tasks/{task_id}",
        json={"status": "in_progress", "assignee_id": other_crew["user_id"]},
        headers=field_crew["headers"],
    )
    assert response.status_code == 403

    # Confirm nothing was silently applied by either rejected request above.
    # Uses a legal status-only PATCH as the verification vehicle since
    # there's no GET /tasks/{id} in this task's scope.
    verify_patch = await client.patch(
        f"/tasks/{task_id}",
        json={"status": "done"},
        headers=field_crew["headers"],
    )
    assert verify_patch.status_code == 200
    assert verify_patch.json()["assignee_id"] == field_crew["user_id"]


async def test_field_crew_cannot_patch_task_not_assigned_to_them(client):
    """A task not assigned to this field_crew user is invisible to them —
    same 404 as a genuinely nonexistent task, not a 403 (see
    _get_task_or_404's docstring)."""
    admin = await _register_and_login(client, "Acme Construction", "patch-fc-notmine-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "patch-fc-notmine@acme.test")
    other_crew = await _invite_and_login_as(client, admin, "field_crew", "patch-fc-notmine-other@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(
        client, admin, project_id, phase_id, assignee_id=other_crew["user_id"]
    )

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"status": "in_progress"},
        headers=field_crew["headers"],
    )
    assert response.status_code == 404


async def test_field_crew_cannot_patch_unassigned_task(client):
    """A task with no assignee at all is also invisible to field_crew —
    same reasoning as the "not assigned to them" case above."""
    admin = await _register_and_login(client, "Acme Construction", "patch-fc-unassigned-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "patch-fc-unassigned@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(client, admin, project_id, phase_id)

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"status": "in_progress"},
        headers=field_crew["headers"],
    )
    assert response.status_code == 404


async def test_accountant_and_client_cannot_patch_task(client):
    """Per the RBAC matrix, accountant/client have no Project Management
    write grant beyond field_crew's narrow status-on-own-task carveout —
    neither role appears in PATCH /tasks/{id}'s require_role list at all,
    so both are blocked at the dependency layer before any ownership/field
    logic even runs."""
    admin = await _register_and_login(client, "Acme Construction", "patch-blocked-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "patch-blocked-acct@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "patch-blocked-client@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(client, admin, project_id, phase_id)

    for actor in (accountant, client_role):
        response = await client.patch(
            f"/tasks/{task_id}",
            json={"status": "in_progress"},
            headers=actor["headers"],
        )
        assert response.status_code == 403


async def test_patch_task_cross_tenant_returns_404(client):
    a = await _register_and_login(client, "Company A", "patch-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "patch-cross-b@acme.test")
    project_id = await _create_project(client, b)
    phase_id = await _create_phase(client, b, project_id)
    task_id = await _create_task(client, b, project_id, phase_id)

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"status": "in_progress"},
        headers=a["headers"],
    )
    assert response.status_code == 404


async def test_patch_nonexistent_task_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-noexist-admin@acme.test")

    response = await client.patch(
        "/tasks/00000000-0000-0000-0000-000000000000",
        json={"status": "in_progress"},
        headers=admin["headers"],
    )
    assert response.status_code == 404
