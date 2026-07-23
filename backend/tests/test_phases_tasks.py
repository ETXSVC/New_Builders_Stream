"""Task 1.14: `POST /projects/{id}/phases`, `POST /projects/{id}/tasks`,
`PATCH /tasks/{id}`.

Helper duplication (`_register_and_login`/`_invite_and_login_as`/
`_project_payload`) follows the established per-test-file convention (see
test_leads.py, test_projects.py) rather than sharing them via conftest.py.
"""
import asyncpg

from tests.conftest import TEST_DATABASE_URL, set_subscription_tier

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


async def _add_membership_directly(user_id, company_id, role):
    """Test-setup plumbing, identical rationale to
    test_subcontractor_assignments.py's own helper of the same name."""
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO company_users (company_id, user_id, role, created_at) "
            "VALUES ($1, $2, $3, now())",
            company_id,
            user_id,
            role,
        )
    finally:
        await conn.close()


async def _create_child_with_membership(client, parent, name, role="admin"):
    """Identical to test_subcontractor_assignments.py's helper of the same
    name — duplicated rather than imported across test modules, matching
    this codebase's existing convention."""
    create = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": name},
        headers=parent["headers"],
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


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


# --- Phase update (PATCH /projects/{id}/phases/{phase_id}) -------------------


async def test_admin_can_update_phase_name_and_sequence(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-update-admin@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id, name="Foundation", sequence=0)

    response = await client.patch(
        f"/projects/{project_id}/phases/{phase_id}",
        json={"name": "Foundation & Framing", "sequence": 2},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Foundation & Framing"
    assert body["sequence"] == 2


async def test_project_manager_can_update_phase(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-update-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "phase-update-pm@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    response = await client.patch(
        f"/projects/{project_id}/phases/{phase_id}",
        json={"name": "Renamed by PM"},
        headers=pm["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed by PM"


async def test_update_phase_only_touches_supplied_fields(client):
    """PATCH semantics: omitting `sequence` must leave it unchanged."""
    admin = await _register_and_login(client, "Acme Construction", "phase-update-partial@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id, name="Foundation", sequence=5)

    response = await client.patch(
        f"/projects/{project_id}/phases/{phase_id}",
        json={"name": "Foundation Renamed"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Foundation Renamed"
    assert body["sequence"] == 5


async def test_field_crew_cannot_update_phase(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-update-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "phase-update-fc@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    response = await client.patch(
        f"/projects/{project_id}/phases/{phase_id}",
        json={"name": "Hijacked"},
        headers=field_crew["headers"],
    )
    assert response.status_code == 403


async def test_update_phase_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "phase-update-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "phase-update-cross-b@acme.test")
    project_id = await _create_project(client, b)
    phase_id = await _create_phase(client, b, project_id)

    response = await client.patch(
        f"/projects/{project_id}/phases/{phase_id}",
        json={"name": "Hijacked"},
        headers=a["headers"],
    )
    assert response.status_code == 404


async def test_update_phase_belonging_to_a_different_project_returns_404(client):
    """A real, same-tenant Phase, but addressed via the WRONG project_id in
    the URL — must 404, not silently update a phase the URL doesn't
    actually name (same "path must match the exact resource" convention
    _get_phase_or_404's own docstring establishes)."""
    admin = await _register_and_login(client, "Acme Construction", "phase-update-wrongproj@acme.test")
    project_a = await _create_project(client, admin, name="Project A")
    project_b = await _create_project(client, admin, name="Project B", site_address="2 Main St")
    phase_id = await _create_phase(client, admin, project_a)

    response = await client.patch(
        f"/projects/{project_b}/phases/{phase_id}",
        json={"name": "Hijacked"},
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_update_nonexistent_phase_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-update-noexist@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.patch(
        f"/projects/{project_id}/phases/00000000-0000-0000-0000-000000000000",
        json={"name": "Ghost"},
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_update_phase_rejects_invalid_payload(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-update-invalid@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    response = await client.patch(
        f"/projects/{project_id}/phases/{phase_id}",
        json={"name": ""},
        headers=admin["headers"],
    )
    assert response.status_code == 422


# --- Phase deletion (DELETE /projects/{id}/phases/{phase_id}) ----------------


async def test_admin_can_delete_phase_and_its_tasks_cascade(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-delete-admin@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    await _create_task(client, admin, project_id, phase_id, name="Pour footings")

    response = await client.delete(
        f"/projects/{project_id}/phases/{phase_id}", headers=admin["headers"]
    )
    assert response.status_code == 204

    listed = await client.get(f"/projects/{project_id}/phases", headers=admin["headers"])
    assert listed.json()["items"] == []

    # The phase itself is gone — a second PATCH/DELETE against the same id
    # now 404s, proving it wasn't a soft-delete/no-op.
    second_delete = await client.delete(
        f"/projects/{project_id}/phases/{phase_id}", headers=admin["headers"]
    )
    assert second_delete.status_code == 404


async def test_project_manager_can_delete_phase(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-delete-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "phase-delete-pm@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    response = await client.delete(
        f"/projects/{project_id}/phases/{phase_id}", headers=pm["headers"]
    )
    assert response.status_code == 204


async def test_field_crew_cannot_delete_phase(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-delete-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "phase-delete-fc@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    response = await client.delete(
        f"/projects/{project_id}/phases/{phase_id}", headers=field_crew["headers"]
    )
    assert response.status_code == 403


async def test_delete_phase_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "phase-delete-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "phase-delete-cross-b@acme.test")
    project_id = await _create_project(client, b)
    phase_id = await _create_phase(client, b, project_id)

    response = await client.delete(
        f"/projects/{project_id}/phases/{phase_id}", headers=a["headers"]
    )
    assert response.status_code == 404


async def test_delete_nonexistent_phase_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "phase-delete-noexist@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.delete(
        f"/projects/{project_id}/phases/00000000-0000-0000-0000-000000000000",
        headers=admin["headers"],
    )
    assert response.status_code == 404


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


async def test_admin_can_patch_task_name_and_due_date(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-namedue-admin@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(client, admin, project_id, phase_id, name="Pour footings")

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"name": "Pour footings (revised)", "due_date": "2026-09-15"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Pour footings (revised)"
    assert body["due_date"] == "2026-09-15"


async def test_field_crew_cannot_patch_task_name_even_on_own_task(client):
    """Same field-level restriction test_field_crew_cannot_patch_assignee_id_
    even_on_own_task exercises for `assignee_id`, extended to the newer
    `name` field — adding fields to TaskUpdateRequest must not widen
    field_crew's own allowed set past `status`."""
    admin = await _register_and_login(client, "Acme Construction", "patch-namefc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "patch-namefc-fc@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(
        client, admin, project_id, phase_id, assignee_id=field_crew["user_id"]
    )

    response = await client.patch(
        f"/tasks/{task_id}",
        json={"name": "Renamed by field crew"},
        headers=field_crew["headers"],
    )
    assert response.status_code == 403


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


# --- Task deletion (DELETE /tasks/{task_id}) ---------------------------------


async def test_admin_can_delete_task(client):
    admin = await _register_and_login(client, "Acme Construction", "task-delete-admin@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(client, admin, project_id, phase_id)

    response = await client.delete(f"/tasks/{task_id}", headers=admin["headers"])
    assert response.status_code == 204

    listed = await client.get(f"/projects/{project_id}/phases", headers=admin["headers"])
    assert listed.json()["items"][0]["tasks"] == []

    # A second delete of the same, now-gone task 404s — proving this wasn't
    # a soft-delete/no-op.
    second_delete = await client.delete(f"/tasks/{task_id}", headers=admin["headers"])
    assert second_delete.status_code == 404


async def test_project_manager_can_delete_task(client):
    admin = await _register_and_login(client, "Acme Construction", "task-delete-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "task-delete-pm@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(client, admin, project_id, phase_id)

    response = await client.delete(f"/tasks/{task_id}", headers=pm["headers"])
    assert response.status_code == 204


async def test_field_crew_cannot_delete_task_even_their_own(client):
    """Narrower than patch_task's RBAC: field_crew's only grant is updating
    `status` on a task assigned to them — deleting isn't part of that
    grant, even for a task genuinely assigned to the caller."""
    admin = await _register_and_login(client, "Acme Construction", "task-delete-fc-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "task-delete-fc@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)
    task_id = await _create_task(
        client, admin, project_id, phase_id, assignee_id=field_crew["user_id"]
    )

    response = await client.delete(f"/tasks/{task_id}", headers=field_crew["headers"])
    assert response.status_code == 403


async def test_accountant_and_client_cannot_delete_task(client):
    admin = await _register_and_login(client, "Acme Construction", "task-delete-acct-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "task-delete-acct@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "task-delete-client@acme.test")
    project_id = await _create_project(client, admin)
    phase_id = await _create_phase(client, admin, project_id)

    for actor in (accountant, client_role):
        task_id = await _create_task(client, admin, project_id, phase_id)
        response = await client.delete(f"/tasks/{task_id}", headers=actor["headers"])
        assert response.status_code == 403


async def test_delete_task_cross_tenant_returns_404(client):
    a = await _register_and_login(client, "Company A", "task-delete-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "task-delete-cross-b@acme.test")
    project_id = await _create_project(client, b)
    phase_id = await _create_phase(client, b, project_id)
    task_id = await _create_task(client, b, project_id, phase_id)

    response = await client.delete(f"/tasks/{task_id}", headers=a["headers"])
    assert response.status_code == 404


async def test_delete_nonexistent_task_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "task-delete-noexist-admin@acme.test")

    response = await client.delete(
        "/tasks/00000000-0000-0000-0000-000000000000", headers=admin["headers"]
    )
    assert response.status_code == 404


# --- Phase list (GET /projects/{id}/phases) ----------------------------------


async def test_list_phases_returns_phases_with_nested_tasks_in_sequence_order(client):
    admin = await _register_and_login(client, "Phase List Co", "phase-list@acme.test")
    project_id = await _create_project(
        client, admin, name="Phase List Project", site_address="2 Main St"
    )

    await _create_phase(client, admin, project_id, name="Second", sequence=2)
    first_id = await _create_phase(client, admin, project_id, name="First", sequence=1)
    await _create_task(client, admin, project_id, first_id, name="In First")

    listed = await client.get(f"/projects/{project_id}/phases", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [p["name"] for p in items] == ["First", "Second"]
    assert [t["name"] for t in items[0]["tasks"]] == ["In First"]
    assert items[1]["tasks"] == []


# =============================================================================
# company_id sourcing: parent-company session (unswitched headers) creating a
# Phase/Task against a child-branch Project. Same empirical shape as
# test_tenant_isolation_phase2.py's own
# test_creating_change_order_under_child_branch_project_uses_project_company_id
# and test_subcontractor_assignments.py's own
# test_creating_assignment_under_child_branch_project_and_subcontractor_uses_child_company_id.
# =============================================================================


async def test_creating_phase_under_child_branch_project_uses_child_company_id(client):
    """The new Phase's company_id must come from project.company_id (the
    CHILD), never current.company_id (the PARENT acting session). The
    Project is created under the CHILD branch (via X-Tenant-ID-switched
    headers, backed by a genuine company_users row); the Phase is then
    created using the PARENT's own DEFAULT headers — deliberately NOT
    X-Tenant-ID-switched — so RLS's get_all_descendant_ids() grant alone is
    what makes the child's Project visible/writable to this session, which
    is the only way current.company_id (parent) and project.company_id
    (child) genuinely diverge without an explicit header switch."""
    parent = await _register_and_login(client, "Parent Co", "phase-parent-co@acme.test")
    await set_subscription_tier(parent["company_id"], "enterprise")
    child_id = await _create_child_with_membership(client, parent, "Seattle Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}
    child_actor = {"headers": child_headers}

    project_id = await _create_project(client, child_actor)

    # Deliberately the parent's own default headers, NOT X-Tenant-ID-switched
    # to the child.
    response = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": "Foundation", "sequence": 0},
        headers=parent["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["company_id"] == child_id, (
        "Phase created against a child-branch Project must belong to the "
        "PROJECT's own company (the child), not the acting session's "
        f"company (the parent) — got {body['company_id']!r}, expected "
        f"child_id={child_id!r}"
    )

    # Read it back via the child's own tenant context to confirm it's
    # genuinely visible there too, not just correctly labeled.
    listed = await client.get(f"/projects/{project_id}/phases", headers=child_headers)
    assert listed.status_code == 200, listed.text
    assert [p["id"] for p in listed.json()["items"]] == [body["id"]]


async def test_creating_task_under_child_branch_project_uses_child_company_id(client):
    """Same bug class as test_creating_phase_under_child_branch_project_uses_child_company_id
    above, for the sibling create_task route: the new Task's company_id
    must come from project.company_id (the CHILD), never
    current.company_id (the PARENT acting session)."""
    parent = await _register_and_login(client, "Parent Co", "task-parent-co@acme.test")
    await set_subscription_tier(parent["company_id"], "enterprise")
    child_id = await _create_child_with_membership(client, parent, "Seattle Branch")
    child_headers = {**parent["headers"], "X-Tenant-ID": child_id}
    child_actor = {"headers": child_headers}

    project_id = await _create_project(client, child_actor)
    phase_id = await _create_phase(client, child_actor, project_id)

    # Deliberately the parent's own default headers, NOT X-Tenant-ID-switched
    # to the child.
    response = await client.post(
        f"/projects/{project_id}/tasks",
        json={"name": "Pour footings", "phase_id": phase_id},
        headers=parent["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["company_id"] == child_id, (
        "Task created against a child-branch Project must belong to the "
        "PROJECT's own company (the child), not the acting session's "
        f"company (the parent) — got {body['company_id']!r}, expected "
        f"child_id={child_id!r}"
    )

    # Read it back via the child's own tenant context to confirm it's
    # genuinely visible there too, not just correctly labeled.
    listed = await client.get(f"/projects/{project_id}/phases", headers=child_headers)
    assert listed.status_code == 200, listed.text
    tasks = listed.json()["items"][0]["tasks"]
    assert [t["id"] for t in tasks] == [body["id"]]
