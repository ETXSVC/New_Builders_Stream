"""Task 1.18: `LEAD_WON` -> draft Project wiring.

`app.services.lead_won_handler.handle_lead_won` isn't live for any request
unless something registers it against `app.core.events` first, and
`tests/conftest.py`'s autouse `_clean_event_registry` fixture clears that
process-global registry before *and* after every test (see that fixture's
own docstring, and `app/core/event_handlers.py`'s). Every test below that
needs the real draft-a-Project behavior calls `register_event_handlers()`
itself, right at the top — same discipline
`tests/test_lead_state_machine.py::test_transition_into_won_calls_publish_with_the_expected_payload`
already established for a hand-rolled capture handler.

`tests/test_lead_state_machine.py` already covers: the transition state
machine itself, and that `publish("LEAD_WON", ...)` fires with the
documented payload on `won`. This file builds on that now that a real
consumer exists — it doesn't re-test the state machine or the payload
shape, only what the handler *does* with that payload.
"""

import json
import uuid

import asyncpg
import pytest

from app.core import events
from app.core.event_handlers import register_event_handlers
from app.models import Project
from tests.conftest import TEST_DATABASE_URL, set_subscription_tier

ADMIN_CONN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


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
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text
    body = login.json()
    # Tier gating (Task 5.7): child-branch creation is Enterprise-gated;
    # registration can only produce trialing/pro.
    await set_subscription_tier(register.json()["company_id"], "enterprise")
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


def _lead_payload(**overrides):
    payload = {
        "contact_name": "Jane Homeowner",
        "project_name": "Kitchen Remodel",
        "email": "jane@example.test",
        "phone": "555-0100",
        "project_type": "residential",
        "estimated_value": "15000.00",
        "notes": "Prefers morning calls",
    }
    payload.update(overrides)
    return payload


async def _create_lead(client, headers, **overrides):
    response = await client.post("/leads", json=_lead_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _advance_to_qualified(client, headers, lead_id):
    for step_status in ("contacted", "estimating", "qualified"):
        response = await client.patch(
            f"/leads/{lead_id}", json={"status": step_status}, headers=headers
        )
        assert response.status_code == 200, response.text


async def _fetch_lead_status(lead_id) -> str:
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        return await conn.fetchval("SELECT status FROM leads WHERE id = $1", lead_id)
    finally:
        await conn.close()


async def _fetch_projects_for_lead(lead_id):
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        return await conn.fetch(
            "SELECT id, company_id, lead_id, name, site_address, status "
            "FROM projects WHERE lead_id = $1",
            lead_id,
        )
    finally:
        await conn.close()


async def _fetch_audit_rows(company_id):
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        return await conn.fetch(
            "SELECT action, entity_id, actor_id, log_metadata FROM audit_log WHERE company_id = $1",
            company_id,
        )
    finally:
        await conn.close()


def _decode_metadata(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


async def test_won_transition_drafts_exactly_one_project_with_carried_over_fields(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Acme Construction", "won-drafts@acme.test")
    lead = await _create_lead(
        client,
        admin["headers"],
        contact_name="Jane Homeowner",
        project_name="Kitchen Remodel",
    )
    await _advance_to_qualified(client, admin["headers"], lead["id"])

    response = await client.patch(
        f"/leads/{lead['id']}", json={"status": "won"}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text

    projects = await _fetch_projects_for_lead(lead["id"])
    assert len(projects) == 1, "expected exactly one drafted Project"
    project = projects[0]
    assert str(project["company_id"]) == admin["company_id"]
    assert str(project["lead_id"]) == lead["id"]
    assert project["name"] == "Kitchen Remodel"
    assert project["site_address"] == ""
    assert project["status"] == "draft"

    rows = await _fetch_audit_rows(admin["company_id"])
    drafted = [r for r in rows if r["action"] == "project.drafted_from_lead"]
    assert len(drafted) == 1
    assert str(drafted[0]["entity_id"]) == str(project["id"])
    assert str(drafted[0]["actor_id"]) == admin["user_id"]
    assert _decode_metadata(drafted[0]["log_metadata"]) == {"lead_id": lead["id"]}


async def test_handler_failure_rolls_back_the_lead_status_change_too(client):
    """Mirrors tests/test_deps.py's
    test_get_current_user_rolls_back_when_route_handler_raises: a write made
    after `yield` (here, inside a LEAD_WON handler invoked from within the
    route handler, rather than the route handler's own body) must not
    survive if something later in the same request raises. Registers a
    handler that does a real partial write (a Project row) and then raises,
    to prove even a handler's own partial side effects roll back — not just
    the Lead status change that triggered it."""
    register_event_handlers()

    async def _failing_handler(*, session, lead_id, company_id, contact_name, project_name, actor_id, **_):
        project = Project(
            company_id=company_id,
            lead_id=lead_id,
            name=project_name,
            site_address="",
            status="draft",
        )
        session.add(project)
        await session.flush()
        raise RuntimeError("simulated LEAD_WON handler failure")

    # Registering a second handler for the same event appends to the list
    # (app/core/events.py: "in registration order") — the real handler runs
    # first and drafts a Project, then this one raises, simulating "the
    # handler" failing after partially succeeding, without needing to mock
    # out handle_lead_won itself.
    events.register("LEAD_WON", _failing_handler)

    admin = await _register_and_login(client, "Acme Construction", "won-rollback@acme.test")
    lead = await _create_lead(client, admin["headers"])
    await _advance_to_qualified(client, admin["headers"], lead["id"])

    rows_before = await _fetch_audit_rows(admin["company_id"])

    # httpx's ASGITransport re-raises unhandled app exceptions by default
    # (tests/test_deps.py's precedent) rather than turning them into a 500
    # Response, so the failure mode under test is a raised exception here,
    # not a status code.
    with pytest.raises(RuntimeError, match="simulated LEAD_WON handler failure"):
        await client.patch(
            f"/leads/{lead['id']}", json={"status": "won"}, headers=admin["headers"]
        )

    # Fresh connection, not the request's own (now-rolled-back) session —
    # same pattern as every other rollback-regression test in this suite.
    status_in_db = await _fetch_lead_status(lead["id"])
    assert status_in_db == "qualified", "Lead status must not have persisted as 'won'"

    projects = await _fetch_projects_for_lead(lead["id"])
    assert projects == [], "the partially-drafted Project must not have persisted either"

    rows_after = await _fetch_audit_rows(admin["company_id"])
    # No new audit rows at all from the failed attempt — neither
    # lead.status_changed (written before publish() was called, in the same
    # transaction) nor project.drafted_from_lead (written by the real
    # handler before the second, failing handler raised).
    assert len(rows_after) == len(rows_before)


@pytest.mark.parametrize(
    "target_status,path",
    [
        ("contacted", ["contacted"]),
        ("estimating", ["contacted", "estimating"]),
        ("qualified", ["contacted", "estimating", "qualified"]),
        ("lost", ["lost"]),
    ],
)
async def test_non_won_transitions_do_not_draft_a_project(client, target_status, path):
    register_event_handlers()

    admin = await _register_and_login(
        client, "Acme Construction", f"no-draft-{target_status}@acme.test"
    )
    lead = await _create_lead(client, admin["headers"])

    response = None
    for step_status in path:
        response = await client.patch(
            f"/leads/{lead['id']}", json={"status": step_status}, headers=admin["headers"]
        )
        assert response.status_code == 200, response.text
    assert response.json()["status"] == target_status

    projects = await _fetch_projects_for_lead(lead["id"])
    assert projects == []


async def _insert_lead_directly(company_id, **overrides) -> str:
    """Seeds a Lead row scoped to an arbitrary company_id via the
    RLS-exempt owner connection — same rationale as
    test_tenant_isolation_phase1.py's own `_insert_lead_directly`: there's
    no legitimate way to create a Lead "as" a child-branch company through
    the API, so this is the only way to get a real row under a child branch.
    Duplicated locally rather than imported, matching this file's own
    per-file helper convention (see this module's docstring)."""
    lead_id = str(uuid.uuid4())
    fields = {
        "contact_name": "Branch Contact",
        "project_name": "Branch Remodel",
        "email": "branch@example.test",
        "project_type": "residential",
        "status": "qualified",
    }
    fields.update(overrides)
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        await conn.execute(
            "INSERT INTO leads (id, company_id, contact_name, project_name, email, "
            "project_type, status) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            lead_id,
            company_id,
            fields["contact_name"],
            fields["project_name"],
            fields["email"],
            fields["project_type"],
            fields["status"],
        )
    finally:
        await conn.close()
    return lead_id


async def test_drafted_project_belongs_to_the_leads_own_company_not_the_actors(client):
    """Regression test for the company_id bug found during this task's spec
    review — see handle_lead_won's docstring (app/services/lead_won_handler.py)
    for the full rationale. Reproduces Task 1.17's hierarchical-visibility
    scenario: a parent admin wins a Lead seeded under a child branch, via
    their own token with no header spoofing."""
    register_event_handlers()

    parent = await _register_and_login(client, "Parent Co", "won-hierarchy-parent@acme.test")
    create_child = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": "Seattle Branch"},
        headers=parent["headers"],
    )
    assert create_child.status_code == 201, create_child.text
    child_id = create_child.json()["id"]

    child_lead_id = await _insert_lead_directly(
        child_id, project_name="Seattle Kitchen", status="qualified"
    )

    # Parent's own token, no X-Tenant-ID header at all — RLS's hierarchical
    # policy already makes the child branch's Lead visible/actionable to a
    # parent-context session, same visibility Task 1.17 tested for `leads`.
    response = await client.patch(
        f"/leads/{child_lead_id}", json={"status": "won"}, headers=parent["headers"]
    )
    assert response.status_code == 200, response.text

    projects = await _fetch_projects_for_lead(child_lead_id)
    assert len(projects) == 1, "expected exactly one drafted Project"
    project = projects[0]
    assert str(project["company_id"]) == child_id, (
        f"drafted Project company_id={project['company_id']} must match the "
        f"Lead's own company_id={child_id} (the branch that owns the Lead), "
        f"not the acting parent's company_id={parent['company_id']}"
    )

    rows = await _fetch_audit_rows(child_id)
    drafted = [r for r in rows if r["action"] == "project.drafted_from_lead"]
    assert len(drafted) == 1, "the drafted-project audit entry must be filed under the branch's own company_id"
