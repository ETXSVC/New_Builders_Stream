import json
from datetime import datetime

import asyncpg
import pytest

from app.core import events
from app.services.lead_transitions import LEAD_TRANSITIONS
from tests.conftest import TEST_DATABASE_URL


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


# Shortest legal-transition path (list of intermediate statuses, in order)
# from a freshly created ("new") lead to each status, used to put a lead into
# the right precondition state before exercising the transition under test.
_PRECONDITION_PATH = {
    "new": [],
    "contacted": ["contacted"],
    "estimating": ["contacted", "estimating"],
    "qualified": ["contacted", "estimating", "qualified"],
    "won": ["contacted", "estimating", "qualified", "won"],
    "lost": ["lost"],
}


async def _advance_to(client, headers, lead_id, path):
    response = None
    for step_status in path:
        response = await client.patch(
            f"/leads/{lead_id}", json={"status": step_status}, headers=headers
        )
        assert response.status_code == 200, response.text
    return response


async def _fetch_audit_rows(company_id):
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
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
# from LEAD_TRANSITIONS itself — this locks in the actual business decision
# documented in app/services/lead_transitions.py's module docstring, so an
# accidental future edit to that table trips this assertion directly, rather
# than the parametrized behavioral tests below just silently following
# whatever the table happens to say.
_EXPECTED_LEGAL_EDGES = {
    ("new", "contacted"),
    ("new", "lost"),
    ("contacted", "estimating"),
    ("contacted", "lost"),
    ("estimating", "qualified"),
    ("estimating", "lost"),
    ("qualified", "won"),
    ("qualified", "lost"),
}


def test_transition_table_matches_the_documented_business_decision():
    actual_edges = {(frm, to) for frm, tos in LEAD_TRANSITIONS.items() for to in tos}
    assert actual_edges == _EXPECTED_LEGAL_EDGES
    assert LEAD_TRANSITIONS["won"] == frozenset()
    assert LEAD_TRANSITIONS["lost"] == frozenset()


@pytest.mark.parametrize("from_status,to_status", sorted(_EXPECTED_LEGAL_EDGES))
async def test_legal_transition_succeeds_and_is_audited(client, from_status, to_status):
    admin = await _register_and_login(
        client, "Acme Construction", f"legal-{from_status}-{to_status}@acme.test"
    )
    lead = await _create_lead(client, admin["headers"])
    await _advance_to(client, admin["headers"], lead["id"], _PRECONDITION_PATH[from_status])

    response = await client.patch(
        f"/leads/{lead['id']}", json={"status": to_status}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == to_status

    rows = await _fetch_audit_rows(admin["company_id"])
    matching = [
        r
        for r in rows
        if r["action"] == "lead.status_changed" and str(r["entity_id"]) == lead["id"]
    ]
    assert matching, "expected at least one lead.status_changed audit row"
    # Reaching multi-hop preconditions (e.g. "won") writes one audit row per
    # hop; the transition under test here is always the LAST hop applied.
    last_metadata = _decode_metadata(matching[-1]["log_metadata"])
    assert last_metadata == {"from": from_status, "to": to_status}


@pytest.mark.parametrize(
    "from_status,to_status",
    [
        ("new", "won"),  # skips contacted/estimating/qualified entirely
        ("new", "qualified"),  # skips contacted/estimating
        ("won", "contacted"),  # terminal state: no legal outgoing transition
        ("won", "lost"),  # terminal state: no legal outgoing transition
        ("lost", "won"),  # terminal state: no legal outgoing transition
        ("lost", "contacted"),  # terminal state: no legal outgoing transition
    ],
)
async def test_illegal_transition_returns_409_with_no_state_change(client, from_status, to_status):
    admin = await _register_and_login(
        client, "Acme Construction", f"illegal-{from_status}-{to_status}@acme.test"
    )
    lead = await _create_lead(client, admin["headers"])
    await _advance_to(client, admin["headers"], lead["id"], _PRECONDITION_PATH[from_status])

    rows_before = await _fetch_audit_rows(admin["company_id"])
    status_changed_before = [r for r in rows_before if r["action"] == "lead.status_changed"]

    response = await client.patch(
        f"/leads/{lead['id']}", json={"status": to_status}, headers=admin["headers"]
    )
    assert response.status_code == 409, response.text

    get_response = await client.get(f"/leads/{lead['id']}", headers=admin["headers"])
    assert get_response.json()["status"] == from_status  # unchanged

    rows_after = await _fetch_audit_rows(admin["company_id"])
    status_changed_after = [r for r in rows_after if r["action"] == "lead.status_changed"]
    assert len(status_changed_after) == len(status_changed_before)  # no new audit row


async def test_plain_field_only_patch_succeeds_without_status_audit_entry(client):
    admin = await _register_and_login(client, "Acme Construction", "field-only@acme.test")
    lead = await _create_lead(client, admin["headers"])

    response = await client.patch(
        f"/leads/{lead['id']}",
        json={"notes": "Called back, wants a quote", "phone": "555-0199"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["notes"] == "Called back, wants a quote"
    assert body["phone"] == "555-0199"
    assert body["status"] == "new"  # untouched

    rows = await _fetch_audit_rows(admin["company_id"])
    matching = [
        r
        for r in rows
        if r["action"] == "lead.status_changed" and str(r["entity_id"]) == lead["id"]
    ]
    assert matching == []


async def test_combined_field_and_status_patch_applies_both_atomically(client):
    admin = await _register_and_login(client, "Acme Construction", "combined-ok@acme.test")
    lead = await _create_lead(client, admin["headers"])

    response = await client.patch(
        f"/leads/{lead['id']}",
        json={"status": "contacted", "notes": "Spoke on the phone, interested"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "contacted"
    assert body["notes"] == "Spoke on the phone, interested"

    rows = await _fetch_audit_rows(admin["company_id"])
    matching = [
        r
        for r in rows
        if r["action"] == "lead.status_changed" and str(r["entity_id"]) == lead["id"]
    ]
    assert len(matching) == 1
    assert _decode_metadata(matching[0]["log_metadata"]) == {"from": "new", "to": "contacted"}


async def test_combined_field_and_illegal_status_patch_applies_neither(client):
    """The field-change half of a combined PATCH must not silently apply
    when the status-change half is illegal — one transaction, one outcome
    (Inherited Invariant #4)."""
    admin = await _register_and_login(client, "Acme Construction", "combined-fail@acme.test")
    lead = await _create_lead(client, admin["headers"], notes="original notes")

    response = await client.patch(
        f"/leads/{lead['id']}",
        json={"status": "won", "notes": "should never be persisted"},  # new -> won skips stages
        headers=admin["headers"],
    )
    assert response.status_code == 409, response.text

    get_response = await client.get(f"/leads/{lead['id']}", headers=admin["headers"])
    body = get_response.json()
    assert body["status"] == "new"
    assert body["notes"] == "original notes"


async def test_updated_at_actually_bumps_after_a_patch(client):
    admin = await _register_and_login(client, "Acme Construction", "updated-at@acme.test")
    lead = await _create_lead(client, admin["headers"])
    original_updated_at = datetime.fromisoformat(lead["updated_at"])

    response = await client.patch(
        f"/leads/{lead['id']}",
        json={"notes": "a genuinely different note"},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    new_updated_at = datetime.fromisoformat(response.json()["updated_at"])

    assert new_updated_at > original_updated_at


async def test_transition_into_won_calls_publish_with_the_expected_payload(client):
    """Task 1.5's own instruction is to keep the publish() call itself in
    this task even though nothing consumes LEAD_WON until a later task —
    this proves it's a real, wired-up function call (not a comment/TODO) by
    registering a temporary handler on the live app.core.events dispatcher
    and confirming it actually fires with the right payload on a
    qualified -> won transition."""
    admin = await _register_and_login(client, "Acme Construction", "publish-check@acme.test")
    lead = await _create_lead(client, admin["headers"], project_name="Deck Build")
    await _advance_to(
        client, admin["headers"], lead["id"], _PRECONDITION_PATH["qualified"]
    )

    received: list[dict] = []

    async def _capture_handler(**payload):
        received.append(payload)

    # No manual deregister needed here — conftest.py's autouse
    # _clean_event_registry fixture clears app.core.events' registry after
    # every test unconditionally, including if this test fails before
    # reaching this point.
    events.register("LEAD_WON", _capture_handler)
    response = await client.patch(
        f"/leads/{lead['id']}", json={"status": "won"}, headers=admin["headers"]
    )
    assert response.status_code == 200, response.text

    assert len(received) == 1
    payload = received[0]
    assert str(payload["lead_id"]) == lead["id"]
    assert str(payload["company_id"]) == admin["company_id"]
    assert payload["contact_name"] == "Jane Homeowner"
    assert payload["project_name"] == "Deck Build"


async def test_non_admin_pm_cannot_patch_lead(client):
    admin = await _register_and_login(client, "Acme Construction", "rbac-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])

    invite = await client.post(
        "/invitations",
        json={"email": "rbac-crew@acme.test", "role": "field_crew"},
        headers=admin["headers"],
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Field Crew", "password": "anothersecret123"},
    )
    assert accept.status_code == 200, accept.text
    login = await client.post(
        "/auth/login", json={"email": "rbac-crew@acme.test", "password": "anothersecret123"}
    )
    field_crew_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.patch(
        f"/leads/{lead['id']}", json={"notes": "should be blocked"}, headers=field_crew_headers
    )
    assert response.status_code == 403


async def test_patch_nonexistent_lead_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "patch-404@acme.test")

    response = await client.patch(
        "/leads/00000000-0000-0000-0000-000000000000",
        json={"notes": "irrelevant"},
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_patch_cross_tenant_lead_returns_404(client):
    a = await _register_and_login(client, "Company A", "patch-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "patch-cross-b@acme.test")

    lead = await _create_lead(client, b["headers"])

    response = await client.patch(
        f"/leads/{lead['id']}", json={"notes": "not yours"}, headers=a["headers"]
    )
    assert response.status_code == 404
