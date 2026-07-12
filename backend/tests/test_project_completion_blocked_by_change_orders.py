"""Task 2.23: `PATCH /projects/{id}/status` -> `completed` is blocked (409)
while the Project has any `pending` Change Order, per Functional
Requirements Section 3 ("A Project cannot move to Completed while it has
open (non-approved) Change Orders"). Implemented in `update_project_status`
(`app/routers/projects.py`), NOT in `is_legal_transition()`
(`app/services/project_transitions.py`), which stays pure transition-table
data with no DB queries — same "table-driven check vs. business-rule check
in the router" split Task 1.18 established for the LEAD_WON event.

Judgment call this file exists specifically to pin down: "open
(non-approved)" is ambiguous between "pending only" and "pending or
rejected" (both are, strictly, non-approved). This file's tests assert the
resolved interpretation: only `status == "pending"` blocks completion. A
`rejected` Change Order is resolved/closed, not open, and does NOT block —
see `test_project_with_only_rejected_change_orders_can_complete_*` below,
the core assertion this task's spec review flagged as needing to be
"unambiguous and well-commented about WHY it's expected to succeed."

Helper duplication (`_register_and_login`/`_invite_and_login_as`/
`_project_payload`/`_create_project`/`_advance_project_to`/
`_create_change_order`/`_approve_change_order`) follows the established
per-test-file convention (see test_change_orders.py's own docstring, which
cites test_leads.py/test_projects.py/test_daily_logs.py for the same norm)
rather than importing across test files. Copied near-verbatim from
test_change_orders.py, which itself copied `_advance_project_to`'s shape
from test_project_state_machine.py's `_advance_to`.
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


async def _advance_project_to(client, admin, project_id, path):
    """Unlike test_change_orders.py's `_advance_project_to` (which hardcodes
    a single shortest path per target status), this takes the explicit
    `path` — this file needs BOTH `active` (`["pre_construction",
    "active"]`) and `suspended` (`["pre_construction", "active",
    "suspended"]`) as intermediate stopping points before attempting the
    `completed` transition under test, and the two paths share a prefix, so
    a single hardcoded per-status table (like test_change_orders.py's) can't
    express "stop at suspended, THEN separately attempt completed as its own
    assertion" as cleanly as just passing the path in.
    """
    response = None
    for step_status in path:
        response = await client.patch(
            f"/projects/{project_id}/status", json={"status": step_status}, headers=admin["headers"]
        )
        assert response.status_code == 200, response.text
    return response


def _change_order_payload(**overrides):
    payload = {
        "description": "Add a skylight in the master bath",
        "cost_delta": "1500.00",
        "schedule_impact_days": 3,
    }
    payload.update(overrides)
    return payload


async def _create_change_order(client, actor, project_id, **overrides):
    response = await client.post(
        f"/projects/{project_id}/change-orders",
        json=_change_order_payload(**overrides),
        headers=actor["headers"],
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _approve_change_order(
    client,
    headers,
    change_order_id,
    *,
    signer_name="Jane Client",
    signer_email="jane-client@example.test",
    content=b"fake-signature-bytes",
):
    response = await client.post(
        f"/change-orders/{change_order_id}/approve",
        data={"signer_name": signer_name, "signer_email": signer_email},
        files={"signature_artifact": ("signature.png", content, "image/png")},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _reject_change_order(client, headers, change_order_id, *, reason="Not needed"):
    response = await client.post(
        f"/change-orders/{change_order_id}/reject",
        json={"reason": reason},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _complete_project(client, admin, project_id):
    return await client.patch(
        f"/projects/{project_id}/status", json={"status": "completed"}, headers=admin["headers"]
    )


# --- (a) Common case: zero Change Orders, no regression ---------------------


async def test_project_with_no_change_orders_can_complete_from_active(client):
    admin = await _register_and_login(client, "Acme Construction", "coblock-noco-active@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


async def test_project_with_no_change_orders_can_complete_from_suspended(client):
    admin = await _register_and_login(client, "Acme Construction", "coblock-noco-suspended@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active", "suspended"])

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


# --- (b)/(c) A pending Change Order blocks completion, from both origins ----


async def test_pending_change_order_blocks_completion_from_active(client):
    admin = await _register_and_login(client, "Acme Construction", "coblock-pending-active@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])
    change_order = await _create_change_order(client, admin, project_id)
    assert change_order["status"] == "pending"

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 409, response.text
    assert "pending approval" in response.json()["detail"]
    assert "1" in response.json()["detail"]

    # Untouched: still 'active', not 'completed'.
    get_response = await client.get(f"/projects/{project_id}", headers=admin["headers"])
    assert get_response.json()["status"] == "active"


async def test_pending_change_order_blocks_completion_from_suspended(client):
    admin = await _register_and_login(
        client, "Acme Construction", "coblock-pending-suspended@acme.test"
    )
    project_id = await _create_project(client, admin)
    # Change Orders can only be created against an "active" project
    # (Task 2.21's own 409 gate), so create it BEFORE suspending.
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])
    change_order = await _create_change_order(client, admin, project_id)
    assert change_order["status"] == "pending"
    await _advance_project_to(client, admin, project_id, ["suspended"])

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 409, response.text
    assert "pending approval" in response.json()["detail"]

    get_response = await client.get(f"/projects/{project_id}", headers=admin["headers"])
    assert get_response.json()["status"] == "suspended"


# --- (d) Only-approved Change Orders do not block --------------------------


async def test_project_with_only_approved_change_orders_can_complete(client):
    admin = await _register_and_login(client, "Acme Construction", "coblock-approved-only@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "coblock-approved-only-c@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])
    change_order = await _create_change_order(client, admin, project_id)
    await _approve_change_order(client, client_role["headers"], change_order["id"])

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


# --- (e) Only-rejected Change Orders do not block: the core judgment call --


async def test_project_with_only_rejected_change_orders_can_complete_from_active(client):
    """The resolved interpretation of "open (non-approved)" (Functional
    Requirements Section 3): a `rejected` Change Order is NOT "open" — it's
    resolved/closed, in the same "no longer blocking" category as
    `approved`, even though it is literally "non-approved." Only `pending`
    Change Orders are genuinely open and block completion. This is the test
    that pins that judgment call down: if the check were instead
    implemented as the more literal `status != "approved"` (which the plan
    text's own bullet describes before its "reconsider this" correction),
    this test would fail with a 409 it should not get."""
    admin = await _register_and_login(client, "Acme Construction", "coblock-rejected-only@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "coblock-rejected-only-c@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])
    change_order = await _create_change_order(client, admin, project_id)
    await _reject_change_order(client, client_role["headers"], change_order["id"])

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


async def test_project_with_only_rejected_change_orders_can_complete_from_suspended(client):
    admin = await _register_and_login(
        client, "Acme Construction", "coblock-rejected-only-susp@acme.test"
    )
    client_role = await _invite_and_login_as(
        client, admin, "client", "coblock-rejected-only-susp-c@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])
    change_order = await _create_change_order(client, admin, project_id)
    await _reject_change_order(client, client_role["headers"], change_order["id"])
    await _advance_project_to(client, admin, project_id, ["suspended"])

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


# --- (f) Mix of approved + rejected, no pending: does not block ------------


async def test_project_with_mix_of_approved_and_rejected_change_orders_can_complete(client):
    admin = await _register_and_login(client, "Acme Construction", "coblock-mix-noblock@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "coblock-mix-noblock-c@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])

    approved_co = await _create_change_order(client, admin, project_id, description="Approved one")
    await _approve_change_order(client, client_role["headers"], approved_co["id"])
    rejected_co = await _create_change_order(client, admin, project_id, description="Rejected one")
    await _reject_change_order(client, client_role["headers"], rejected_co["id"])

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"


# --- (g) Mix INCLUDING a pending one: still blocked -------------------------


async def test_project_with_pending_among_approved_and_rejected_change_orders_is_blocked(client):
    """Proves the check counts ONLY `pending` rows and isn't confused by the
    presence of other, non-blocking (`approved`/`rejected`) rows on the same
    project — the count in the error message must reflect just the one
    truly-open Change Order, not all three."""
    admin = await _register_and_login(client, "Acme Construction", "coblock-mix-blocked@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "coblock-mix-blocked-c@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])

    approved_co = await _create_change_order(client, admin, project_id, description="Approved one")
    await _approve_change_order(client, client_role["headers"], approved_co["id"])
    rejected_co = await _create_change_order(client, admin, project_id, description="Rejected one")
    await _reject_change_order(client, client_role["headers"], rejected_co["id"])
    pending_co = await _create_change_order(client, admin, project_id, description="Still pending")
    assert pending_co["status"] == "pending"

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert "pending approval" in detail
    assert "1" in detail  # exactly one pending row, not three

    get_response = await client.get(f"/projects/{project_id}", headers=admin["headers"])
    assert get_response.json()["status"] == "active"


async def test_pending_count_in_error_message_is_accurate_for_multiple_pending(client):
    """Every other test in this file has at most one `pending` row, so the
    boolean block/no-block outcome alone wouldn't catch a regression that
    miscounts (e.g. counting approved+pending together, or always reporting
    `1`). This test creates 3 pending Change Orders plus 1 approved and 1
    rejected (noise) and asserts the reported count is exactly `3`."""
    admin = await _register_and_login(client, "Acme Construction", "coblock-count-accurate@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "coblock-count-accurate-c@acme.test"
    )
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])

    approved_co = await _create_change_order(client, admin, project_id, description="Approved one")
    await _approve_change_order(client, client_role["headers"], approved_co["id"])
    rejected_co = await _create_change_order(client, admin, project_id, description="Rejected one")
    await _reject_change_order(client, client_role["headers"], rejected_co["id"])
    for i in range(3):
        pending_co = await _create_change_order(
            client, admin, project_id, description=f"Pending {i}"
        )
        assert pending_co["status"] == "pending"

    response = await _complete_project(client, admin, project_id)
    assert response.status_code == 409, response.text
    assert "3 Change Order(s) pending approval" in response.json()["detail"]


# --- Error message is distinct from the generic illegal-transition 409 -----


async def test_change_order_block_message_differs_from_illegal_transition_message(client):
    """A reader hitting this 409 needs to understand the transition itself
    was legal (active -> completed IS a real edge in PROJECT_TRANSITIONS),
    but blocked by open Change Orders specifically — not that `completed`
    isn't reachable from `active` at all. Confirms the two 409 messages are
    textually distinguishable."""
    admin = await _register_and_login(client, "Acme Construction", "coblock-msg-diff@acme.test")
    project_id = await _create_project(client, admin)
    await _advance_project_to(client, admin, project_id, ["pre_construction", "active"])
    await _create_change_order(client, admin, project_id)

    blocked_response = await _complete_project(client, admin, project_id)
    assert blocked_response.status_code == 409, blocked_response.text
    blocked_detail = blocked_response.json()["detail"]

    # A genuinely illegal transition (draft -> archived, on a second,
    # freshly-created project) for comparison.
    other_project_id = await _create_project(client, admin, name="Second Project")
    illegal_response = await client.patch(
        f"/projects/{other_project_id}/status", json={"status": "archived"}, headers=admin["headers"]
    )
    assert illegal_response.status_code == 409, illegal_response.text
    illegal_detail = illegal_response.json()["detail"]

    assert blocked_detail != illegal_detail
    assert "Illegal project status transition" in illegal_detail
    assert "Illegal project status transition" not in blocked_detail
    assert "Change Order" in blocked_detail
