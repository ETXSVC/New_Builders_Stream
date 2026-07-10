"""Task 1.13: Project Status State Machine (`PATCH /projects/{id}/status`).

Structurally mirrors test_lead_state_machine.py (Task 1.5) as closely as the
underlying resource allows — table-driven legal/illegal transitions, an
explicit hand-authored expected-edges set that locks in the actual business
decision documented in app/services/project_transitions.py's module
docstring, audit log correctness, no-op resubmission, RBAC, cross-tenant
404, and an updated_at bump check. Also adds a dedicated suspend/resume
round-trip test, since `suspended -> active` is the one edge the plan calls
out explicitly as needing deliberate reasoning (reversibility), not just a
parametrized case among many.
"""
import json
from datetime import datetime

import asyncpg
import pytest

from app.services.project_transitions import PROJECT_TRANSITIONS
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


async def _create_project(client, headers, **overrides):
    response = await client.post("/projects", json=_project_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


# Shortest legal-transition path (list of intermediate statuses, in order)
# from a freshly created ("draft") project to each status, used to put a
# project into the right precondition state before exercising the
# transition under test. Follows project_transitions.PROJECT_TRANSITIONS'
# single linear spine (no branch choices needed to reach any given status
# except "active", which is also reachable via "suspended" -> "active" but
# the shortest/first-reached path is the forward one below).
_PRECONDITION_PATH = {
    "draft": [],
    "pre_construction": ["pre_construction"],
    "active": ["pre_construction", "active"],
    "suspended": ["pre_construction", "active", "suspended"],
    "completed": ["pre_construction", "active", "completed"],
    "archived": ["pre_construction", "active", "completed", "archived"],
}


async def _advance_to(client, headers, project_id, path):
    response = None
    for step_status in path:
        response = await client.patch(
            f"/projects/{project_id}/status", json={"status": step_status}, headers=headers
        )
        assert response.status_code == 200, response.text
    return response


async def _fetch_audit_rows(company_id):
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        return await conn.fetch(
            "SELECT action, entity_id, log_metadata FROM audit_log WHERE company_id = $1",
            company_id,
        )
    finally:
        await conn.close()


def _decode_metadata(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


# Explicit, hand-authored expected transition table. Deliberately NOT derived
# from PROJECT_TRANSITIONS itself — this locks in the actual business
# decision documented in app/services/project_transitions.py's module
# docstring, so an accidental future edit to that table trips this assertion
# directly, rather than the parametrized behavioral tests below just
# silently following whatever the table happens to say.
_EXPECTED_LEGAL_EDGES = {
    ("draft", "pre_construction"),
    ("pre_construction", "active"),
    ("active", "suspended"),
    ("active", "completed"),
    ("suspended", "active"),
    ("suspended", "completed"),
    ("completed", "archived"),
}


def test_transition_table_matches_the_documented_business_decision():
    actual_edges = {(frm, to) for frm, tos in PROJECT_TRANSITIONS.items() for to in tos}
    assert actual_edges == _EXPECTED_LEGAL_EDGES
    assert PROJECT_TRANSITIONS["archived"] == frozenset()
    assert PROJECT_TRANSITIONS["draft"] == frozenset({"pre_construction"})


@pytest.mark.parametrize("from_status,to_status", sorted(_EXPECTED_LEGAL_EDGES))
async def test_legal_transition_succeeds_and_is_audited(client, from_status, to_status):
    admin = await _register_and_login(
        client, "Acme Construction", f"legal-{from_status}-{to_status}@acme.test"
    )
    project = await _create_project(client, admin["headers"])
    await _advance_to(
        client, admin["headers"], project["id"], _PRECONDITION_PATH[from_status]
    )

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": to_status, "reason": "advancing per test"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == to_status

    rows = await _fetch_audit_rows(admin["company_id"])
    matching = [
        r
        for r in rows
        if r["action"] == "project.status_changed" and str(r["entity_id"]) == project["id"]
    ]
    assert matching, "expected at least one project.status_changed audit row"
    # Reaching multi-hop preconditions (e.g. "completed") writes one audit
    # row per hop; the transition under test here is always the LAST hop
    # applied.
    last_metadata = _decode_metadata(matching[-1]["log_metadata"])
    assert last_metadata == {"from": from_status, "to": to_status, "reason": "advancing per test"}


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("draft", "completed"),  # skips pre_construction/active/suspended entirely
        ("draft", "active"),  # skips pre_construction
        ("draft", "suspended"),  # skips pre_construction/active
        ("draft", "archived"),  # skips everything
        ("pre_construction", "suspended"),  # skips active (the explicitly-flagged ambiguity)
        ("pre_construction", "completed"),  # skips active
        ("active", "archived"),  # skips completed
        ("suspended", "archived"),  # skips completed
        ("completed", "active"),  # no legal transition back out of completed except archived
        ("completed", "suspended"),
        ("archived", "active"),  # terminal state: no legal outgoing transition
        ("archived", "draft"),  # terminal state: no legal outgoing transition
    ],
)
async def test_illegal_transition_returns_409_with_no_state_change(client, from_status, to_status):
    admin = await _register_and_login(
        client, "Acme Construction", f"illegal-{from_status}-{to_status}@acme.test"
    )
    project = await _create_project(client, admin["headers"])
    await _advance_to(
        client, admin["headers"], project["id"], _PRECONDITION_PATH[from_status]
    )

    rows_before = await _fetch_audit_rows(admin["company_id"])
    status_changed_before = [r for r in rows_before if r["action"] == "project.status_changed"]

    response = await client.patch(
        f"/projects/{project['id']}/status", json={"status": to_status}, headers=admin["headers"]
    )
    assert response.status_code == 409, response.text

    get_response = await client.get(f"/projects/{project['id']}", headers=admin["headers"])
    assert get_response.json()["status"] == from_status  # unchanged

    rows_after = await _fetch_audit_rows(admin["company_id"])
    status_changed_after = [r for r in rows_after if r["action"] == "project.status_changed"]
    assert len(status_changed_after) == len(status_changed_before)  # no new audit row


async def test_suspend_resume_round_trip(client):
    """The one edge the plan explicitly calls out as needing deliberate
    reasoning: `active -> suspended -> active` must work as a genuine
    round-trip, not just a one-way parametrized case. Confirms both hops
    succeed, the project ends up back in "active", and both transitions are
    independently audited (two distinct audit rows, not one)."""
    admin = await _register_and_login(client, "Acme Construction", "suspend-resume@acme.test")
    project = await _create_project(client, admin["headers"])
    await _advance_to(client, admin["headers"], project["id"], ["pre_construction", "active"])

    suspend = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "suspended", "reason": "weather delay"},
        headers=admin["headers"],
    )
    assert suspend.status_code == 200, suspend.text
    assert suspend.json()["status"] == "suspended"

    resume = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "active", "reason": "weather cleared"},
        headers=admin["headers"],
    )
    assert resume.status_code == 200, resume.text
    assert resume.json()["status"] == "active"

    # Confirm the project can still proceed onward after resuming (i.e. the
    # round trip didn't leave it in some inconsistent state) — suspend a
    # second time then move on to completed.
    suspend_again = await client.patch(
        f"/projects/{project['id']}/status", json={"status": "suspended"}, headers=admin["headers"]
    )
    assert suspend_again.status_code == 200, suspend_again.text
    complete = await client.patch(
        f"/projects/{project['id']}/status", json={"status": "completed"}, headers=admin["headers"]
    )
    assert complete.status_code == 200, complete.text

    rows = await _fetch_audit_rows(admin["company_id"])
    matching = [
        r
        for r in rows
        if r["action"] == "project.status_changed" and str(r["entity_id"]) == project["id"]
    ]
    edges = [(_decode_metadata(r["log_metadata"])["from"], _decode_metadata(r["log_metadata"])["to"]) for r in matching]
    assert ("active", "suspended") in edges
    assert ("suspended", "active") in edges
    assert edges.count(("active", "suspended")) == 2  # suspended twice in this test


async def test_status_resubmission_is_a_noop(client):
    admin = await _register_and_login(client, "Acme Construction", "resubmit-noop@acme.test")
    project = await _create_project(client, admin["headers"])

    response = await client.patch(
        f"/projects/{project['id']}/status", json={"status": "draft"}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "draft"

    rows = await _fetch_audit_rows(admin["company_id"])
    matching = [
        r
        for r in rows
        if r["action"] == "project.status_changed" and str(r["entity_id"]) == project["id"]
    ]
    assert matching == []


async def test_audit_log_entry_includes_reason(client):
    admin = await _register_and_login(client, "Acme Construction", "audit-reason@acme.test")
    project = await _create_project(client, admin["headers"])

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "pre_construction", "reason": "permits approved"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text

    rows = await _fetch_audit_rows(admin["company_id"])
    matching = [
        r
        for r in rows
        if r["action"] == "project.status_changed" and str(r["entity_id"]) == project["id"]
    ]
    assert len(matching) == 1
    assert _decode_metadata(matching[0]["log_metadata"]) == {
        "from": "draft",
        "to": "pre_construction",
        "reason": "permits approved",
    }


async def test_audit_log_entry_reason_defaults_to_null_when_omitted(client):
    admin = await _register_and_login(client, "Acme Construction", "audit-no-reason@acme.test")
    project = await _create_project(client, admin["headers"])

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "pre_construction"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text

    rows = await _fetch_audit_rows(admin["company_id"])
    matching = [
        r
        for r in rows
        if r["action"] == "project.status_changed" and str(r["entity_id"]) == project["id"]
    ]
    assert len(matching) == 1
    assert _decode_metadata(matching[0]["log_metadata"]) == {
        "from": "draft",
        "to": "pre_construction",
        "reason": None,
    }


async def test_updated_at_actually_bumps_after_a_status_change(client):
    admin = await _register_and_login(client, "Acme Construction", "updated-at@acme.test")
    project = await _create_project(client, admin["headers"])
    original_updated_at = datetime.fromisoformat(project["updated_at"])

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "pre_construction"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    new_updated_at = datetime.fromisoformat(response.json()["updated_at"])

    assert new_updated_at > original_updated_at


async def test_invalid_status_value_returns_422(client):
    admin = await _register_and_login(client, "Acme Construction", "invalid-status@acme.test")
    project = await _create_project(client, admin["headers"])

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "not_a_real_status"},
        headers=admin["headers"],
    )
    assert response.status_code == 422, response.text


# --- RBAC ---------------------------------------------------------------


async def test_field_crew_cannot_patch_project_status(client):
    """The plan explicitly calls for confirming field_crew specifically is
    blocked, not just "some non-admin role" — field_crew has read-assigned
    access to Project Management per the RBAC matrix, which makes it the
    most plausible role to accidentally over-grant write access to."""
    admin = await _register_and_login(client, "Acme Construction", "rbac-fc-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "rbac-fc@acme.test")

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "pre_construction"},
        headers=field_crew["headers"],
    )
    assert response.status_code == 403


async def test_accountant_cannot_patch_project_status(client):
    admin = await _register_and_login(client, "Acme Construction", "rbac-acct-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    accountant = await _invite_and_login_as(client, admin, "accountant", "rbac-acct@acme.test")

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "pre_construction"},
        headers=accountant["headers"],
    )
    assert response.status_code == 403


async def test_client_cannot_patch_project_status(client):
    admin = await _register_and_login(client, "Acme Construction", "rbac-client-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    client_role = await _invite_and_login_as(client, admin, "client", "rbac-client@acme.test")

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "pre_construction"},
        headers=client_role["headers"],
    )
    assert response.status_code == 403


async def test_project_manager_can_patch_project_status(client):
    admin = await _register_and_login(client, "Acme Construction", "pm-status-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    pm = await _invite_and_login_as(client, admin, "project_manager", "pm-status@acme.test")

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "pre_construction"},
        headers=pm["headers"],
    )
    assert response.status_code == 200, response.text


# --- Existence / tenant isolation ----------------------------------------


async def test_patch_status_nonexistent_project_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "status-404@acme.test")

    response = await client.patch(
        "/projects/00000000-0000-0000-0000-000000000000",
        json={"status": "pre_construction"},
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_patch_status_cross_tenant_project_returns_404(client):
    a = await _register_and_login(client, "Company A", "status-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "status-cross-b@acme.test")

    project = await _create_project(client, b["headers"])

    response = await client.patch(
        f"/projects/{project['id']}/status",
        json={"status": "pre_construction"},
        headers=a["headers"],
    )
    assert response.status_code == 404
