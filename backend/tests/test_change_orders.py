"""Task 2.21: `POST /projects/{id}/change-orders`, `GET
/projects/{id}/change-orders`.

Helper duplication (`_register_and_login`/`_invite_and_login_as`/
`_project_payload`/`_create_project`) follows the established
per-test-file convention (see test_leads.py, test_projects.py,
test_daily_logs.py) rather than sharing them via conftest.py.

Projects are driven through their REAL status state machine via `PATCH
/projects/{id}/status` to reach `active`/`completed`, not a direct-SQL
shortcut — unlike `test_estimates.py`'s `_set_estimate_status_directly`,
which exists only because Estimate has no route that can reach certain
statuses (e.g. `sent`) without an e-signature flow that doesn't exist yet
at that point in the plan. Project already has a fully-built, legal
`PATCH /projects/{id}/status` route covering every status in
`PROJECT_TRANSITIONS` (Task 1.13), so there's no such gap here — using the
real route is both possible and the more faithful test setup, matching
`test_project_state_machine.py`'s own `_advance_to` precedent.
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


# Shortest legal-transition path from a freshly created ("draft") project to
# each status, copied from test_project_state_machine.py's own
# _PRECONDITION_PATH — that file is the source of truth for
# PROJECT_TRANSITIONS' actual shape, this is just the two paths this test
# file needs.
_PRECONDITION_PATH = {
    "pre_construction": ["pre_construction"],
    "active": ["pre_construction", "active"],
    "suspended": ["pre_construction", "active", "suspended"],
    "completed": ["pre_construction", "active", "completed"],
    "archived": ["pre_construction", "active", "completed", "archived"],
}


async def _advance_project_to(client, admin, project_id, status_name):
    for step_status in _PRECONDITION_PATH[status_name]:
        response = await client.patch(
            f"/projects/{project_id}/status", json={"status": step_status}, headers=admin["headers"]
        )
        assert response.status_code == 200, response.text


def _change_order_payload(**overrides):
    payload = {
        "description": "Add a skylight in the master bath",
        "cost_delta": "1500.00",
        "schedule_impact_days": 3,
    }
    payload.update(overrides)
    return payload


async def _create_change_order(client, actor, project_id, **overrides):
    return await client.post(
        f"/projects/{project_id}/change-orders",
        json=_change_order_payload(**overrides),
        headers=actor["headers"],
    )


# --- Create: happy path -------------------------------------------------


async def test_admin_can_create_change_order_against_active_project(client):
    admin = await _register_and_login(client, "Acme Construction", "co-admin-active@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    response = await _create_change_order(client, admin, project_id)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["project_id"] == project_id
    assert body["company_id"] == admin["company_id"]
    assert body["description"] == "Add a skylight in the master bath"
    assert body["cost_delta"] == "1500.00"
    assert body["schedule_impact_days"] == 3
    assert body["status"] == "pending"
    assert body["esignature_id"] is None
    assert "id" in body and "created_at" in body


async def test_project_manager_can_create_change_order_against_active_project(client):
    admin = await _register_and_login(client, "Acme Construction", "co-pm-active@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "co-pm@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    response = await _create_change_order(client, pm, project_id)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"


async def test_create_change_order_negative_cost_delta_is_allowed(client):
    # US-3.6: a Change Order can be a credit (negative cost_delta) as well
    # as an add — no sign validation anywhere in the stack.
    admin = await _register_and_login(client, "Acme Construction", "co-credit@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    response = await _create_change_order(client, admin, project_id, cost_delta="-500.00")
    assert response.status_code == 201, response.text
    assert response.json()["cost_delta"] == "-500.00"


async def test_create_change_order_schedule_impact_days_defaults_to_zero(client):
    admin = await _register_and_login(client, "Acme Construction", "co-default-days@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    payload = _change_order_payload()
    del payload["schedule_impact_days"]
    response = await client.post(
        f"/projects/{project_id}/change-orders", json=payload, headers=admin["headers"]
    )
    assert response.status_code == 201, response.text
    assert response.json()["schedule_impact_days"] == 0


async def test_cannot_spoof_status_via_request_payload(client):
    """ChangeOrderCreateRequest has no `status` field at all, so this
    should be structurally impossible — verified empirically by passing an
    unexpected `status` in the raw JSON body and confirming it's ignored
    (Pydantic drops unknown fields by default) rather than accepted."""
    admin = await _register_and_login(client, "Acme Construction", "co-spoof-status@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    payload = _change_order_payload()
    payload["status"] = "approved"
    response = await client.post(
        f"/projects/{project_id}/change-orders", json=payload, headers=admin["headers"]
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"


# --- Create: only legal against an active Project (409) -----------------


async def test_create_change_order_against_draft_project_returns_409(client):
    admin = await _register_and_login(client, "Acme Construction", "co-draft-409@acme.test")
    project_id = await _create_project(client, admin)
    # Freshly created project is "draft" — no transition applied.

    response = await _create_change_order(client, admin, project_id)
    assert response.status_code == 409, response.text


async def test_create_change_order_against_completed_project_returns_409(client):
    admin = await _register_and_login(client, "Acme Construction", "co-completed-409@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "completed")

    response = await _create_change_order(client, admin, project_id)
    assert response.status_code == 409, response.text


async def test_create_change_order_against_pre_construction_project_returns_409(client):
    """The 409 gate is a blanket `!= "active"` check (app/routers/
    change_orders.py), not a hardcoded list of illegal statuses — but the
    plan's own test list only explicitly names `draft`/`completed` as
    representative illegal states. This and the two tests below round out
    coverage of every remaining status in Project.VALID_STATUSES
    (app/models/project.py) so the blanket check is exercised at each of
    its non-active values, not just two of five."""
    admin = await _register_and_login(client, "Acme Construction", "co-preconstr-409@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "pre_construction")

    response = await _create_change_order(client, admin, project_id)
    assert response.status_code == 409, response.text


async def test_create_change_order_against_suspended_project_returns_409(client):
    admin = await _register_and_login(client, "Acme Construction", "co-suspended-409@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "suspended")

    response = await _create_change_order(client, admin, project_id)
    assert response.status_code == 409, response.text


async def test_create_change_order_against_archived_project_returns_409(client):
    admin = await _register_and_login(client, "Acme Construction", "co-archived-409@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "archived")

    response = await _create_change_order(client, admin, project_id)
    assert response.status_code == 409, response.text


# --- Create: RBAC ----------------------------------------------------------


async def test_field_crew_cannot_create_change_order(client):
    admin = await _register_and_login(client, "Acme Construction", "co-fc-403@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "co-fc@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    response = await _create_change_order(client, field_crew, project_id)
    assert response.status_code == 403


async def test_accountant_cannot_create_change_order(client):
    admin = await _register_and_login(client, "Acme Construction", "co-acct-403@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "co-acct@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    response = await _create_change_order(client, accountant, project_id)
    assert response.status_code == 403


async def test_client_cannot_create_change_order(client):
    admin = await _register_and_login(client, "Acme Construction", "co-client-403@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "co-client@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    response = await _create_change_order(client, client_role, project_id)
    assert response.status_code == 403


# --- Create: cross-tenant ----------------------------------------------


async def test_create_change_order_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "co-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "co-cross-b@acme.test")
    project_id = await _create_project(client, b)
    await _advance_project_to(client, b, project_id, "active")

    response = await _create_change_order(client, a, project_id)
    assert response.status_code == 404


async def test_create_change_order_nonexistent_project_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "co-nonexistent@acme.test")
    nonexistent_project_id = "00000000-0000-0000-0000-000000000000"

    response = await _create_change_order(client, admin, nonexistent_project_id)
    assert response.status_code == 404


# --- List -----------------------------------------------------------------


async def test_list_change_orders_scoped_to_correct_project(client):
    admin = await _register_and_login(client, "Acme Construction", "co-list-scope@acme.test")
    project_a = await _create_project(client, admin, name="Project A")
    project_b = await _create_project(client, admin, name="Project B")
    await _advance_project_to(client, admin, project_a, "active")
    await _advance_project_to(client, admin, project_b, "active")

    create_a = await _create_change_order(client, admin, project_a, description="A's change order")
    assert create_a.status_code == 201, create_a.text
    create_b = await _create_change_order(client, admin, project_b, description="B's change order")
    assert create_b.status_code == 201, create_b.text

    response_a = await client.get(f"/projects/{project_a}/change-orders", headers=admin["headers"])
    assert response_a.status_code == 200, response_a.text
    items_a = response_a.json()["items"]
    assert len(items_a) == 1
    assert items_a[0]["description"] == "A's change order"

    response_b = await client.get(f"/projects/{project_b}/change-orders", headers=admin["headers"])
    assert response_b.status_code == 200, response_b.text
    items_b = response_b.json()["items"]
    assert len(items_b) == 1
    assert items_b[0]["description"] == "B's change order"


async def test_list_change_orders_empty_project_returns_empty_list(client):
    admin = await _register_and_login(client, "Acme Construction", "co-list-empty@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(f"/projects/{project_id}/change-orders", headers=admin["headers"])
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_cursor": None}


async def test_list_change_orders_paginates_with_cursor(client):
    admin = await _register_and_login(client, "Acme Construction", "co-list-page@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    created_ids = []
    for i in range(5):
        response = await _create_change_order(client, admin, project_id, description=f"CO {i}")
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
            f"/projects/{project_id}/change-orders", params=params, headers=admin["headers"]
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


# --- List: RBAC --------------------------------------------------------


async def test_admin_pm_accountant_can_list_change_orders(client):
    admin = await _register_and_login(client, "Acme Construction", "co-list-rbac@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "co-list-rbac-pm@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "co-list-rbac-acct@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")
    create = await _create_change_order(client, admin, project_id)
    assert create.status_code == 201, create.text

    for actor in (admin, pm, accountant):
        response = await client.get(f"/projects/{project_id}/change-orders", headers=actor["headers"])
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1


async def test_field_crew_cannot_list_change_orders(client):
    admin = await _register_and_login(client, "Acme Construction", "co-list-fc-403@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "co-list-fc@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(f"/projects/{project_id}/change-orders", headers=field_crew["headers"])
    assert response.status_code == 403


async def test_client_cannot_list_change_orders(client):
    """Deliberate, temporary scope decision for this task (see
    `_READ_ROLES`'s own comment, app/routers/change_orders.py — that's
    where the full rationale lives, not `list_change_orders`'s docstring):
    `client` has no `sent`-equivalent Change Order status to scope
    visibility by yet, so it's excluded from `_READ_ROLES` entirely and
    blocked with a 403, not a filtered/empty 200."""
    admin = await _register_and_login(client, "Acme Construction", "co-list-client-403@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "co-list-client@acme.test")
    project_id = await _create_project(client, admin)

    response = await client.get(f"/projects/{project_id}/change-orders", headers=client_role["headers"])
    assert response.status_code == 403


async def test_list_change_orders_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "co-list-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "co-list-cross-b@acme.test")
    project_id = await _create_project(client, b)
    await _advance_project_to(client, b, project_id, "active")
    create = await _create_change_order(client, b, project_id)
    assert create.status_code == 201, create.text

    response = await client.get(f"/projects/{project_id}/change-orders", headers=a["headers"])
    assert response.status_code == 404


async def test_list_change_orders_nonexistent_project_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "co-list-nonexistent@acme.test")
    nonexistent_project_id = "00000000-0000-0000-0000-000000000000"

    response = await client.get(
        f"/projects/{nonexistent_project_id}/change-orders", headers=admin["headers"]
    )
    assert response.status_code == 404
