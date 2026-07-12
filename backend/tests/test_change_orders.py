"""Task 2.21: `POST /projects/{id}/change-orders`, `GET
/projects/{id}/change-orders`.

Task 2.22 (below the `# --- Send-for-signature/approve/reject` marker):
`POST /change-orders/{id}/send-for-signature`, `.../approve`, `.../reject` —
the same shape as Task 2.19's Estimate approval flow
(`tests/test_estimates.py`), reusing the SAME shared `capture_esignature()`
service call (`app/services/esignature.py`, Task 2.18) with
`document_type="change_order"`.

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
`test_project_state_machine.py`'s own `_advance_to` precedent. Change
Orders themselves need no such shortcut either: every status a ChangeOrder
can reach (`pending` -> `approved`/`rejected`) is reachable through this
task's own real `approve`/`reject` routes.
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


async def test_client_can_list_change_orders_scoped_to_pending(client):
    """Task 2.22 resolves the deferral `_READ_ROLES`'s own comment
    (app/routers/change_orders.py) points at this exact task: `client` now
    has a real, concrete action (`approve`/`reject`) to take on a `pending`
    ChangeOrder, so `client` is included in `_READ_ROLES` and the list is
    scoped to `status="pending"` only — the exact same shape
    `test_list_estimates_as_client_shows_sent_only` (test_estimates.py)
    proves for Estimate's own analogous `status="sent"` scoping. An
    `approved` ChangeOrder must NOT appear in a client's list."""
    admin = await _register_and_login(client, "Acme Construction", "co-list-client-scoped@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "co-list-client-scoped-c@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    pending_co = await _create_change_order(client, admin, project_id, description="Still pending")
    assert pending_co.status_code == 201, pending_co.text
    approved_co = await _create_change_order(client, admin, project_id, description="Already approved")
    assert approved_co.status_code == 201, approved_co.text
    approve_response = await _approve_change_order(
        client, client_role["headers"], approved_co.json()["id"]
    )
    assert approve_response.status_code == 200, approve_response.text

    response = await client.get(f"/projects/{project_id}/change-orders", headers=client_role["headers"])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == pending_co.json()["id"]
    assert items[0]["status"] == "pending"


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


# =============================================================================
# Task 2.22: POST /change-orders/{id}/send-for-signature, /approve, /reject
# =============================================================================


async def _fetch_audit_rows(company_id):
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        return await conn.fetch(
            "SELECT action, entity_id, log_metadata FROM audit_log WHERE company_id = $1",
            company_id,
        )
    finally:
        await conn.close()


async def _approve_change_order(
    client,
    headers,
    change_order_id,
    *,
    signer_name="Jane Client",
    signer_email="jane-client@example.test",
    content=b"fake-signature-bytes",
):
    return await client.post(
        f"/change-orders/{change_order_id}/approve",
        data={"signer_name": signer_name, "signer_email": signer_email},
        files={"signature_artifact": ("signature.png", content, "image/png")},
        headers=headers,
    )


# --- send-for-signature: pure validation gate (no status transition) ------


async def test_send_for_signature_succeeds_when_pending(client):
    """`send-for-signature` never mutates `status` at all for a ChangeOrder
    (unlike Estimate's own `"sent"` transition) — a `pending` ChangeOrder
    stays `pending` afterward; this route's entire job is the readiness
    gate itself."""
    admin = await _register_and_login(client, "Acme Construction", "co-sfs-ok-admin@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")
    created = await _create_change_order(client, admin, project_id)
    assert created.status_code == 201, created.text
    change_order_id = created.json()["id"]

    response = await client.post(
        f"/change-orders/{change_order_id}/send-for-signature", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"


async def test_send_for_signature_requires_pending_status(client):
    """409 if the ChangeOrder is no longer `pending` (e.g. already
    approved) — mirroring Estimate's own `send-for-signature` guard, just
    against `pending` instead of `sent`."""
    admin = await _register_and_login(client, "Acme Construction", "co-sfs-409-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "co-sfs-409-client@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")
    created = await _create_change_order(client, admin, project_id)
    assert created.status_code == 201, created.text
    change_order_id = created.json()["id"]

    approve_response = await _approve_change_order(client, client_role["headers"], change_order_id)
    assert approve_response.status_code == 200, approve_response.text

    response = await client.post(
        f"/change-orders/{change_order_id}/send-for-signature", headers=admin["headers"]
    )
    assert response.status_code == 409, response.text


async def test_non_write_roles_cannot_send_for_signature(client):
    admin = await _register_and_login(client, "Acme Construction", "co-sfs-rbac-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "co-sfs-rbac-client@acme.test")
    accountant = await _invite_and_login_as(
        client, admin, "accountant", "co-sfs-rbac-accountant@acme.test"
    )
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "co-sfs-rbac-crew@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    for actor in (client_role, accountant, field_crew):
        created = await _create_change_order(client, admin, project_id)
        assert created.status_code == 201, created.text
        response = await client.post(
            f"/change-orders/{created.json()['id']}/send-for-signature", headers=actor["headers"]
        )
        assert response.status_code == 403, response.text


async def test_send_for_signature_nonexistent_change_order_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "co-sfs-404-admin@acme.test")

    response = await client.post(
        "/change-orders/00000000-0000-0000-0000-000000000000/send-for-signature",
        headers=admin["headers"],
    )
    assert response.status_code == 404


# --- approve: reuses the SAME shared capture_esignature() code path -------


async def test_approve_captures_esignature_reusing_shared_capture_path(client):
    """The core proof this task asks for: `approve_change_order` calls the
    exact SAME `capture_esignature()` (Task 2.18) that `approve_estimate`
    calls — not a parallel, independently-written implementation. Proven
    two ways: (1) the resulting `Esignature.document_type` is genuinely
    `"change_order"`, fetched back via the real `GET /esignatures/{id}`
    route (Task 2.18), and (2) the immutability/REVOKE guarantee from Task
    2.17 (raw UPDATE/DELETE rejected as `app_user`) applies identically to
    this `change_order`-typed row, mirroring
    `test_raw_update_against_esignatures_as_app_user_is_rejected`/
    `test_raw_delete_against_esignatures_as_app_user_is_rejected`
    (tests/test_esignatures.py) — if approval used a different code path
    that bypassed `capture_esignature`, there would be no reason for this
    guarantee to hold for a change-order-sourced row too."""
    admin = await _register_and_login(client, "Acme Construction", "co-approve-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "co-approve-client@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")
    created = await _create_change_order(client, admin, project_id)
    assert created.status_code == 201, created.text
    change_order_id = created.json()["id"]

    response = await _approve_change_order(
        client,
        client_role["headers"],
        change_order_id,
        signer_name="Jane Client",
        signer_email="jane-client@example.test",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["esignature_id"] is not None

    # (1) The e-signature is a REAL, persisted record with document_type
    # genuinely "change_order" — not just a set FK.
    esignature_response = await client.get(
        f"/esignatures/{body['esignature_id']}", headers=admin["headers"]
    )
    assert esignature_response.status_code == 200, esignature_response.text
    esignature_body = esignature_response.json()
    assert esignature_body["signer_name"] == "Jane Client"
    assert esignature_body["signer_email"] == "jane-client@example.test"
    assert esignature_body["document_type"] == "change_order"
    assert esignature_body["company_id"] == admin["company_id"]

    # (2) The immutability/REVOKE guarantee applies identically to this
    # change_order-typed row: raw UPDATE/DELETE as app_user are rejected.
    app_conn = await asyncpg.connect(APP_CONN_DSN)
    try:
        await app_conn.execute(
            "SELECT set_config('app.current_tenant', $1, false)", admin["company_id"]
        )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app_conn.execute(
                "UPDATE esignatures SET signer_name = 'Hacked' WHERE id = $1",
                body["esignature_id"],
            )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app_conn.execute(
                "DELETE FROM esignatures WHERE id = $1", body["esignature_id"]
            )
    finally:
        await app_conn.close()

    audit_rows = await _fetch_audit_rows(admin["company_id"])
    matching = [row for row in audit_rows if row["action"] == "change_order.approved"]
    assert len(matching) == 1
    assert str(matching[0]["entity_id"]) == change_order_id


async def test_approve_requires_pending_status(client):
    admin = await _register_and_login(client, "Acme Construction", "co-approve-409-admin@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "co-approve-409-client@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")
    created = await _create_change_order(client, admin, project_id)
    assert created.status_code == 201, created.text
    change_order_id = created.json()["id"]

    first = await _approve_change_order(client, client_role["headers"], change_order_id)
    assert first.status_code == 200, first.text

    second = await _approve_change_order(client, client_role["headers"], change_order_id)
    assert second.status_code == 409, second.text


async def test_approve_nonexistent_change_order_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "co-approve-404-admin@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "co-approve-404-client@acme.test"
    )

    response = await _approve_change_order(
        client, client_role["headers"], "00000000-0000-0000-0000-000000000000"
    )
    assert response.status_code == 404


# --- reject: requires a reason, does not capture an esignature ------------


async def test_reject_requires_a_reason(client):
    admin = await _register_and_login(client, "Acme Construction", "co-reject-noreason-admin@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "co-reject-noreason-client@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")
    created = await _create_change_order(client, admin, project_id)
    assert created.status_code == 201, created.text
    change_order_id = created.json()["id"]

    response = await client.post(
        f"/change-orders/{change_order_id}/reject", json={}, headers=client_role["headers"]
    )
    assert response.status_code == 422, response.text

    # Untouched: still 'pending', not 'rejected'.
    get_response = await client.get(
        f"/projects/{project_id}/change-orders", headers=admin["headers"]
    )
    items = get_response.json()["items"]
    assert next(item for item in items if item["id"] == change_order_id)["status"] == "pending"


async def test_reject_with_reason_succeeds_and_does_not_capture_esignature(client):
    admin = await _register_and_login(client, "Acme Construction", "co-reject-ok-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "co-reject-ok-client@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")
    created = await _create_change_order(client, admin, project_id)
    assert created.status_code == 201, created.text
    change_order_id = created.json()["id"]

    response = await client.post(
        f"/change-orders/{change_order_id}/reject",
        json={"reason": "Cost is too high for this phase"},
        headers=client_role["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["esignature_id"] is None

    audit_rows = await _fetch_audit_rows(admin["company_id"])
    matching = [row for row in audit_rows if row["action"] == "change_order.rejected"]
    assert len(matching) == 1
    assert str(matching[0]["entity_id"]) == change_order_id
    metadata = matching[0]["log_metadata"]
    import json as _json

    decoded = _json.loads(metadata) if isinstance(metadata, str) else metadata
    assert decoded == {"reason": "Cost is too high for this phase"}


async def test_reject_requires_pending_status(client):
    admin = await _register_and_login(client, "Acme Construction", "co-reject-409-admin@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "co-reject-409-client@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")
    created = await _create_change_order(client, admin, project_id)
    assert created.status_code == 201, created.text
    change_order_id = created.json()["id"]

    approve_response = await _approve_change_order(client, client_role["headers"], change_order_id)
    assert approve_response.status_code == 200, approve_response.text

    response = await client.post(
        f"/change-orders/{change_order_id}/reject",
        json={"reason": "too late"},
        headers=client_role["headers"],
    )
    assert response.status_code == 409, response.text


async def test_reject_nonexistent_change_order_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "co-reject-404-admin@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "co-reject-404-client@acme.test"
    )

    response = await client.post(
        "/change-orders/00000000-0000-0000-0000-000000000000/reject",
        json={"reason": "irrelevant"},
        headers=client_role["headers"],
    )
    assert response.status_code == 404


# --- approve/reject: client-only RBAC --------------------------------------


async def test_non_client_roles_cannot_approve_or_reject(client):
    admin = await _register_and_login(client, "Acme Construction", "co-noclient-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "co-noclient-pm@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "co-noclient-acct@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "co-noclient-crew@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, "active")

    for actor in (admin, pm, accountant, field_crew):
        created = await _create_change_order(client, admin, project_id)
        assert created.status_code == 201, created.text
        change_order_id = created.json()["id"]

        approve_response = await _approve_change_order(client, actor["headers"], change_order_id)
        assert approve_response.status_code == 403, approve_response.text

        reject_response = await client.post(
            f"/change-orders/{change_order_id}/reject",
            json={"reason": "should be blocked"},
            headers=actor["headers"],
        )
        assert reject_response.status_code == 403, reject_response.text
