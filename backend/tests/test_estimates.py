"""Task 2.10: `POST /estimates`, `GET /estimates`, `GET /estimates/{id}`
router tests (`app/routers/estimates.py`).

Every scenario here goes through real HTTP calls via the `client` fixture,
same discipline as `test_leads.py`/`test_cost_catalog.py` — direct
owner-connection SQL is used ONLY for out-of-band setup the API genuinely
cannot do yet (see `_set_estimate_status_directly` below: Task 2.19's
`POST /estimates/{id}/send-for-signature` hasn't landed at this point in
the plan, so there is no API route that can ever produce a `status='sent'`
estimate yet, but this task's own `?status=` filter and client-scoping
logic still need to be exercised against one).
"""

from decimal import Decimal

import asyncpg

from app.core import events
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
    # Tier gating (Task 5.7): child-branch creation is Enterprise-gated;
    # registration can only produce trialing/pro.
    await set_subscription_tier(register.json()["company_id"], "enterprise")
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


# Shortest legal-transition path from a freshly created ("new") lead to each
# status — same table test_lead_state_machine.py uses.
_PRECONDITION_PATH = {
    "new": [],
    "contacted": ["contacted"],
    "estimating": ["contacted", "estimating"],
    "qualified": ["contacted", "estimating", "qualified"],
    "won": ["contacted", "estimating", "qualified", "won"],
    "lost": ["lost"],
}


async def _advance_lead_to(client, headers, lead_id, target_status):
    for step_status in _PRECONDITION_PATH[target_status]:
        response = await client.patch(
            f"/leads/{lead_id}", json={"status": step_status}, headers=headers
        )
        assert response.status_code == 200, response.text


def _project_payload(**overrides):
    payload = {
        "name": "Kitchen Remodel Project",
        "site_address": "123 Main St",
    }
    payload.update(overrides)
    return payload


async def _create_project(client, headers, **overrides):
    response = await client.post("/projects", json=_project_payload(**overrides), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def _markup_profile_payload(**overrides):
    payload = {
        "name": "Standard Markup",
        "overhead_pct": "10.00",
        "profit_pct": "15.00",
    }
    payload.update(overrides)
    return payload


async def _create_markup_profile(client, headers, **overrides):
    response = await client.post(
        "/markup-profiles", json=_markup_profile_payload(**overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _fetch_audit_rows(company_id):
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        return await conn.fetch(
            "SELECT action, entity_id, log_metadata FROM audit_log WHERE company_id = $1",
            company_id,
        )
    finally:
        await conn.close()


async def _set_estimate_status_directly(estimate_id, status_value):
    """See this module's docstring — closes a gap that has nothing to do
    with what this file tests: Task 2.19's send-for-signature route (the
    only way `status` legitimately becomes 'sent') doesn't exist yet."""
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "UPDATE estimates SET status = $1 WHERE id = $2", status_value, estimate_id
        )
    finally:
        await conn.close()


async def _set_estimate_snapshotted_directly(estimate_id):
    """Same rationale as `_set_estimate_status_directly` above:
    `is_snapshotted` only legitimately becomes `true` via Task 2.19's
    real approval flow (`send-for-signature` / approve), which doesn't
    exist yet at this point in the plan. `_set_estimate_status_directly`
    as written only touches `status`, not `is_snapshotted`, so this is a
    small variant for the one column that helper doesn't cover."""
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "UPDATE estimates SET is_snapshotted = true WHERE id = $1", estimate_id
        )
    finally:
        await conn.close()


async def _insert_line_item_directly(
    estimate_id, company_id, cost_catalog_item_id, *, quantity, unit_rate_snapshot
):
    """Same rationale as `_set_estimate_status_directly` above: `PUT
    /estimates/{id}/lines` (Task 2.11, the only legitimate way to write an
    `EstimateLineItem`) doesn't exist yet at this point in the plan, but
    `GET /estimates/{id}`'s nested `line_items` serialization still needs a
    real, non-empty case exercised rather than only ever asserting against
    an empty list."""
    line_total = quantity * unit_rate_snapshot
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO estimate_line_items "
            "(id, estimate_id, company_id, cost_catalog_item_id, quantity, "
            "unit_rate_snapshot, line_total) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6)",
            estimate_id,
            company_id,
            cost_catalog_item_id,
            quantity,
            unit_rate_snapshot,
            line_total,
        )
    finally:
        await conn.close()


def _catalog_item_payload(**overrides):
    payload = {
        "category": "framing",
        "name": "2x4 Lumber",
        "unit": "each",
        "unit_rate": "45.00",
    }
    payload.update(overrides)
    return payload


async def _create_catalog_item(client, headers, **overrides):
    response = await client.post(
        "/catalogs/items", json=_catalog_item_payload(**overrides), headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_membership_directly(user_id, company_id, role):
    """See `test_cost_catalog.py`'s module docstring for the full
    chicken-and-egg explanation this same helper closes there: no route
    lets an existing user be added to a company they didn't register or get
    invited into, which this file needs to let one admin token act as
    either a parent or child company via `X-Tenant-ID`."""
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
    """Creates a real child branch via the actual API route, then grants the
    parent admin membership in it directly, so the SAME admin token can act
    as either company via `X-Tenant-ID` — identical to `test_cost_catalog.py`'s
    helper of the same name."""
    create = await client.post(
        f"/companies/{parent['company_id']}/children",
        json={"name": name},
        headers=parent["headers"],
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]
    await _add_membership_directly(parent["user_id"], child_id, role)
    return child_id


# =============================================================================
# POST /estimates — create
# =============================================================================


async def test_create_estimate_lead_scoped(client):
    admin = await _register_and_login(client, "Acme Construction", "lead-scoped-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])
    await _advance_lead_to(client, admin["headers"], lead["id"], "estimating")
    markup = await _create_markup_profile(client, admin["headers"])

    response = await client.post(
        "/estimates",
        json={"lead_id": lead["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["lead_id"] == lead["id"]
    assert body["project_id"] is None
    assert body["markup_profile_id"] == markup["id"]
    assert body["status"] == "draft"
    assert body["subtotal"] is None
    assert body["total"] is None
    assert body["is_snapshotted"] is False
    assert body["company_id"] == admin["company_id"]

    audit_rows = await _fetch_audit_rows(admin["company_id"])
    matching = [row for row in audit_rows if row["action"] == "estimate.created"]
    assert len(matching) == 1
    assert str(matching[0]["entity_id"]) == body["id"]


async def test_create_estimate_project_scoped(client):
    admin = await _register_and_login(
        client, "Acme Construction", "project-scoped-admin@acme.test"
    )
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])

    response = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["project_id"] == project["id"]
    assert body["lead_id"] is None
    assert body["status"] == "draft"
    assert body["is_snapshotted"] is False


async def test_create_estimate_as_project_manager(client):
    admin = await _register_and_login(client, "Acme Construction", "pm-est-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "pm-est@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])

    response = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=pm["headers"],
    )
    assert response.status_code == 201, response.text


async def test_create_estimate_rejects_neither_association(client):
    admin = await _register_and_login(client, "Acme Construction", "neither-admin@acme.test")
    markup = await _create_markup_profile(client, admin["headers"])

    response = await client.post(
        "/estimates", json={"markup_profile_id": markup["id"]}, headers=admin["headers"]
    )
    assert response.status_code == 422


async def test_create_estimate_rejects_both_associations(client):
    admin = await _register_and_login(client, "Acme Construction", "both-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    lead = await _create_lead(client, admin["headers"])
    await _advance_lead_to(client, admin["headers"], lead["id"], "estimating")
    markup = await _create_markup_profile(client, admin["headers"])

    response = await client.post(
        "/estimates",
        json={"project_id": project["id"], "lead_id": lead["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_create_estimate_invalid_project_id_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "badproj-admin@acme.test")
    markup = await _create_markup_profile(client, admin["headers"])

    response = await client.post(
        "/estimates",
        json={
            "project_id": "00000000-0000-0000-0000-000000000000",
            "markup_profile_id": markup["id"],
        },
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_create_estimate_invalid_lead_id_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "badlead-admin@acme.test")
    markup = await _create_markup_profile(client, admin["headers"])

    response = await client.post(
        "/estimates",
        json={
            "lead_id": "00000000-0000-0000-0000-000000000000",
            "markup_profile_id": markup["id"],
        },
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_create_estimate_invalid_markup_profile_id_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "badmarkup-admin@acme.test")
    project = await _create_project(client, admin["headers"])

    response = await client.post(
        "/estimates",
        json={
            "project_id": project["id"],
            "markup_profile_id": "00000000-0000-0000-0000-000000000000",
        },
        headers=admin["headers"],
    )
    assert response.status_code == 404


async def test_create_estimate_cross_tenant_markup_profile_id_returns_404(client):
    """Regression test added during Task 2.12's review: the FK constraint on
    `estimates.markup_profile_id` alone only checks row EXISTENCE, not RLS
    visibility, so a well-formed cross-tenant markup_profile_id was
    previously accepted here with 201 and only surfaced as an unhandled
    `NoResultFound` 500 the first time `POST /estimates/{id}/calculate`
    tried to look the profile up. Confirms it now 404s at creation time
    instead, the same "doesn't exist or isn't visible to you" pattern every
    other referenced-id check in this route already uses."""
    a = await _register_and_login(client, "Company A", "crossmarkup-a@acme.test")
    b = await _register_and_login(client, "Company B", "crossmarkup-b@acme.test")
    project = await _create_project(client, a["headers"])
    markup = await _create_markup_profile(client, b["headers"])

    response = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=a["headers"],
    )
    assert response.status_code == 404


async def test_create_estimate_cross_tenant_project_id_returns_404(client):
    a = await _register_and_login(client, "Company A", "cross-proj-a@acme.test")
    b = await _register_and_login(client, "Company B", "cross-proj-b@acme.test")
    project = await _create_project(client, a["headers"])
    markup = await _create_markup_profile(client, b["headers"])

    response = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=b["headers"],
    )
    assert response.status_code == 404


async def test_create_estimate_rejects_lead_not_yet_estimating(client):
    """Lead stays at its default 'new' status — well below the
    `_LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE` floor."""
    admin = await _register_and_login(client, "Acme Construction", "notestimating-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])

    response = await client.post(
        "/estimates",
        json={"lead_id": lead["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_create_estimate_rejects_lost_lead(client):
    """`lost` is chronologically reachable "after" earlier stages but is
    explicitly excluded from `_LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE` — it's
    an off-ramp, not further progress along the estimating pipeline."""
    admin = await _register_and_login(client, "Acme Construction", "lost-admin@acme.test")
    lead = await _create_lead(client, admin["headers"])
    await _advance_lead_to(client, admin["headers"], lead["id"], "lost")
    markup = await _create_markup_profile(client, admin["headers"])

    response = await client.post(
        "/estimates",
        json={"lead_id": lead["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert response.status_code == 422


async def test_non_admin_pm_roles_blocked_on_create(client):
    admin = await _register_and_login(client, "Acme Construction", "blocked-est-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "crew-est@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client-blocked-est@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "acct-blocked-est@acme.test")

    for actor in (field_crew, client_role, accountant):
        response = await client.post(
            "/estimates",
            json={"project_id": project["id"], "markup_profile_id": markup["id"]},
            headers=actor["headers"],
        )
        assert response.status_code == 403


# =============================================================================
# GET /estimates — list
# =============================================================================


async def test_list_estimates(client):
    admin = await _register_and_login(client, "Acme Construction", "list-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )

    response = await client.get("/estimates", headers=admin["headers"])
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_list_estimates_filtered_by_status(client):
    admin = await _register_and_login(client, "Acme Construction", "listfilter-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    draft = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    sent = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    await _set_estimate_status_directly(sent.json()["id"], "sent")

    response = await client.get("/estimates", params={"status": "sent"}, headers=admin["headers"])
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == sent.json()["id"]

    draft_response = await client.get(
        "/estimates", params={"status": "draft"}, headers=admin["headers"]
    )
    assert draft_response.status_code == 200
    draft_items = draft_response.json()["items"]
    assert len(draft_items) == 1
    assert draft_items[0]["id"] == draft.json()["id"]


async def test_list_estimates_rejects_invalid_status_filter(client):
    admin = await _register_and_login(client, "Acme Construction", "invalidfilter-admin@acme.test")

    response = await client.get(
        "/estimates", params={"status": "not-a-real-status"}, headers=admin["headers"]
    )
    assert response.status_code == 422


async def test_list_estimates_as_client_shows_sent_only(client):
    admin = await _register_and_login(client, "Acme Construction", "clientlist-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "client-list-est@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    sent = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    await _set_estimate_status_directly(sent.json()["id"], "sent")

    response = await client.get("/estimates", headers=client_role["headers"])
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == sent.json()["id"]
    assert items[0]["status"] == "sent"


async def test_accountant_can_list_estimates(client):
    admin = await _register_and_login(client, "Acme Construction", "acctlist-est-admin@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "acct-list-est@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )

    response = await client.get("/estimates", headers=accountant["headers"])
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_field_crew_cannot_list_estimates(client):
    admin = await _register_and_login(client, "Acme Construction", "crewlist-est-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "crew-list-est@acme.test")

    response = await client.get("/estimates", headers=field_crew["headers"])
    assert response.status_code == 403


# =============================================================================
# GET /estimates/{id} — detail
# =============================================================================


async def test_get_estimate_includes_line_items(client):
    admin = await _register_and_login(client, "Acme Construction", "get-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    response = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == estimate_id
    assert body["project_id"] == project["id"]
    assert body["line_items"] == []


async def test_get_estimate_includes_nonempty_line_items(client):
    """`PUT /estimates/{id}/lines` (Task 2.11) doesn't exist yet, so a real
    line item is seeded directly (`_insert_line_item_directly`, same
    out-of-band-setup rationale as `_set_estimate_status_directly` above)
    to exercise `EstimateDetailResponse`'s nested-serialization path against
    a genuinely non-empty list, not just the always-empty case a freshly
    created Estimate produces."""
    admin = await _register_and_login(client, "Acme Construction", "nonempty-get-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    catalog_item = await _create_catalog_item(client, admin["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    await _insert_line_item_directly(
        estimate_id,
        admin["company_id"],
        catalog_item["id"],
        quantity=Decimal("10.00"),
        unit_rate_snapshot=Decimal("45.00"),
    )

    response = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    assert response.status_code == 200
    line_items = response.json()["line_items"]
    assert len(line_items) == 1
    assert line_items[0]["cost_catalog_item_id"] == catalog_item["id"]
    assert line_items[0]["quantity"] == "10.00"
    assert line_items[0]["unit_rate_snapshot"] == "45.00"
    assert line_items[0]["line_total"] == "450.00"


async def test_get_estimate_cross_tenant_returns_404(client):
    a = await _register_and_login(client, "Company A", "cross-get-a@acme.test")
    b = await _register_and_login(client, "Company B", "cross-get-b@acme.test")
    project = await _create_project(client, a["headers"])
    markup = await _create_markup_profile(client, a["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=a["headers"],
    )

    response = await client.get(f"/estimates/{created.json()['id']}", headers=b["headers"])
    assert response.status_code == 404


async def test_get_nonexistent_estimate_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "nonexistent-est-admin@acme.test")

    response = await client.get(
        "/estimates/00000000-0000-0000-0000-000000000000", headers=admin["headers"]
    )
    assert response.status_code == 404


async def test_field_crew_cannot_get_estimate(client):
    admin = await _register_and_login(client, "Acme Construction", "crewget-est-admin@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "crew-get-est@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )

    response = await client.get(
        f"/estimates/{created.json()['id']}", headers=field_crew["headers"]
    )
    assert response.status_code == 403


# =============================================================================
# PUT /estimates/{id}/lines — batch replace (Task 2.11)
# =============================================================================


async def test_replace_line_items_fresh_set(client):
    admin = await _register_and_login(client, "Acme Construction", "replace-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    catalog_item = await _create_catalog_item(client, admin["headers"], unit_rate="45.00")
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item["id"], "quantity": "10.00"}]},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == estimate_id
    line_items = body["line_items"]
    assert len(line_items) == 1
    assert line_items[0]["cost_catalog_item_id"] == catalog_item["id"]
    assert line_items[0]["quantity"] == "10.00"
    assert line_items[0]["unit_rate_snapshot"] == "45.00"
    assert line_items[0]["line_total"] == "450.00"

    # GET reflects the same replaced set.
    get_response = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    assert get_response.status_code == 200
    assert len(get_response.json()["line_items"]) == 1


async def test_replace_line_items_as_project_manager(client):
    admin = await _register_and_login(client, "Acme Construction", "replace-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "replace-pm@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    catalog_item = await _create_catalog_item(client, admin["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item["id"], "quantity": "2.00"}]},
        headers=pm["headers"],
    )
    assert response.status_code == 200, response.text


async def test_non_admin_pm_roles_blocked_on_replace_line_items(client):
    admin = await _register_and_login(client, "Acme Construction", "replace-blocked-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    catalog_item = await _create_catalog_item(client, admin["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "replace-crew@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "replace-client@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "replace-acct@acme.test")

    for actor in (field_crew, client_role, accountant):
        response = await client.put(
            f"/estimates/{estimate_id}/lines",
            json={"items": [{"cost_catalog_item_id": catalog_item["id"], "quantity": "1.00"}]},
            headers=actor["headers"],
        )
        assert response.status_code == 403


async def test_replace_line_items_true_replace_clears_prior_lines(client):
    """A true replace, not append: line items present before the call but
    absent from the new request body must be gone afterward, and the final
    set must be EXACTLY the new request body's items — proven by seeding
    an out-of-band line item first (`_insert_line_item_directly`), then
    replacing with a request body naming a DIFFERENT catalog item, and
    asserting the old one is no longer present."""
    admin = await _register_and_login(client, "Acme Construction", "truereplace-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    old_item = await _create_catalog_item(client, admin["headers"], name="Old Item")
    new_item = await _create_catalog_item(client, admin["headers"], name="New Item")
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    await _insert_line_item_directly(
        estimate_id,
        admin["company_id"],
        old_item["id"],
        quantity=Decimal("3.00"),
        unit_rate_snapshot=Decimal("45.00"),
    )
    # Sanity check: the seeded line is actually there before the replace.
    pre_get = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    assert len(pre_get.json()["line_items"]) == 1

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": new_item["id"], "quantity": "5.00"}]},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    line_items = response.json()["line_items"]
    assert len(line_items) == 1
    assert line_items[0]["cost_catalog_item_id"] == new_item["id"]

    get_response = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    final_line_items = get_response.json()["line_items"]
    assert len(final_line_items) == 1
    assert final_line_items[0]["cost_catalog_item_id"] == new_item["id"]


async def test_replace_line_items_snapshotted_estimate_returns_409_and_applies_nothing(client):
    admin = await _register_and_login(client, "Acme Construction", "snapshot-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    existing_item = await _create_catalog_item(client, admin["headers"], name="Existing Item")
    new_item = await _create_catalog_item(client, admin["headers"], name="Rejected Item")
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    await _insert_line_item_directly(
        estimate_id,
        admin["company_id"],
        existing_item["id"],
        quantity=Decimal("2.00"),
        unit_rate_snapshot=Decimal("45.00"),
    )
    await _set_estimate_snapshotted_directly(estimate_id)

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": new_item["id"], "quantity": "1.00"}]},
        headers=admin["headers"],
    )
    assert response.status_code == 409, response.text

    # Nothing was applied: the pre-existing line item is untouched, and the
    # rejected new item never made it in.
    get_response = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    line_items = get_response.json()["line_items"]
    assert len(line_items) == 1
    assert line_items[0]["cost_catalog_item_id"] == existing_item["id"]
    assert line_items[0]["quantity"] == "2.00"


async def test_replace_line_items_invalid_catalog_item_id_returns_422(client):
    admin = await _register_and_login(client, "Acme Construction", "invalidcat-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    valid_item = await _create_catalog_item(client, admin["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={
            "items": [
                {"cost_catalog_item_id": valid_item["id"], "quantity": "1.00"},
                {
                    "cost_catalog_item_id": "00000000-0000-0000-0000-000000000000",
                    "quantity": "1.00",
                },
            ]
        },
        headers=admin["headers"],
    )
    assert response.status_code == 422, response.text

    # Nothing applied: not even the valid line preceding the invalid one.
    get_response = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    assert get_response.json()["line_items"] == []


async def test_replace_line_items_cross_tenant_catalog_item_returns_422(client):
    """A `cost_catalog_item_id` belonging to a different, unrelated tenant
    never appears in `resolve_visible_catalog_items`' output for this
    company, so it is rejected the same way a nonexistent id is — 422, not
    404 (this isn't a "does the estimate exist" check, it's "is this catalog
    item usable in this estimate")."""
    a = await _register_and_login(client, "Company A", "crosscat-a@acme.test")
    b = await _register_and_login(client, "Company B", "crosscat-b@acme.test")
    project = await _create_project(client, a["headers"])
    markup = await _create_markup_profile(client, a["headers"])
    other_tenant_item = await _create_catalog_item(client, b["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=a["headers"],
    )
    estimate_id = created.json()["id"]

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": other_tenant_item["id"], "quantity": "1.00"}]},
        headers=a["headers"],
    )
    assert response.status_code == 422, response.text


async def test_replace_line_items_uses_decimal_arithmetic_not_float(client):
    """`quantity=Decimal("3")` * `unit_rate=Decimal("33.33")` — the exact
    pair this task's own spec text recommends as a currency-scaled
    precision probe (a multiplication analog of the classic `0.1 * 3 ==
    0.30000000000000004` base-2-representation trap: a base-10 value with
    a fractional part, multiplied by a small integer).

    The mathematically correct, hand-computed `Decimal` product is exactly
    `Decimal("99.99")` — `Decimal` multiplication's result scale is the SUM
    of its operands' scales (0 + 2 = 2 decimal places here), computed
    exactly, with no rounding needed. Asserting the API response's
    `line_total` string is EXACTLY `"99.99"` (not `pytest.approx`, not
    truncated/rounded to fewer digits) proves the server computed
    `quantity * unit_rate_snapshot` using `Decimal` throughout.

    Note: on this platform/build, `float(3) * float(Decimal("33.33"))`
    happens to also land on exactly `99.99` (verified empirically, not
    assumed) — this specific pair does not itself demonstrate float
    drift the way `0.1 * 3` does for addition-shaped traps. It is kept
    anyway because it is this task's own suggested concrete example, and
    the assertion below is still a real, exact-value proof that Decimal
    (not truncated/rounded float-shaped) arithmetic produced the result —
    `pytest.approx` would silently accept several wrong-but-close values
    this exact-string comparison does not.
    """
    assert Decimal("3") * Decimal("33.33") == Decimal("99.99")

    admin = await _register_and_login(client, "Acme Construction", "decimal-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    catalog_item = await _create_catalog_item(client, admin["headers"], unit_rate="33.33")
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item["id"], "quantity": "3"}]},
        headers=admin["headers"],
    )
    assert response.status_code == 200, response.text
    line_items = response.json()["line_items"]
    assert len(line_items) == 1
    assert line_items[0]["unit_rate_snapshot"] == "33.33"
    assert line_items[0]["line_total"] == "99.99"


async def test_replace_line_items_mixed_batch_preserves_pre_existing_lines_on_rejection(client):
    """A stronger variant of `test_replace_line_items_invalid_catalog_item_id_returns_422`
    above: that test starts from an EMPTY estimate, so it can only prove the
    rejected batch itself never lands, not that a genuinely PRE-EXISTING
    line item (from an earlier, successful `PUT`) survives untouched. Here,
    a real line item is written via a first, successful `PUT`, then a
    second `PUT` mixing one valid line with one invalid
    `cost_catalog_item_id` must 422 and leave the first `PUT`'s line item
    completely unchanged — proving "validate everything before mutating
    anything" holds across the delete-then-insert boundary, not just within
    a single failed request that never had anything to delete in the first
    place."""
    admin = await _register_and_login(client, "Acme Construction", "mixedbatch-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    surviving_item = await _create_catalog_item(client, admin["headers"], name="Surviving Item")
    valid_item = await _create_catalog_item(client, admin["headers"], name="Valid Item")
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    first_put = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": surviving_item["id"], "quantity": "4.00"}]},
        headers=admin["headers"],
    )
    assert first_put.status_code == 200, first_put.text

    second_put = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={
            "items": [
                {"cost_catalog_item_id": valid_item["id"], "quantity": "1.00"},
                {
                    "cost_catalog_item_id": "00000000-0000-0000-0000-000000000000",
                    "quantity": "1.00",
                },
            ]
        },
        headers=admin["headers"],
    )
    assert second_put.status_code == 422, second_put.text

    get_response = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    line_items = get_response.json()["line_items"]
    assert len(line_items) == 1
    assert line_items[0]["cost_catalog_item_id"] == surviving_item["id"]
    assert line_items[0]["quantity"] == "4.00"


async def test_replace_line_items_uses_child_branch_override_rate(client):
    """The "inheritance-aware rate resolution" this task is named for:
    `resolve_visible_catalog_items` (Task 2.4), not a raw table lookup,
    decides `unit_rate_snapshot`. A child branch overriding a parent's
    catalog item must get ITS OWN override rate when building an estimate,
    never the parent's original — and referencing the parent's original id
    directly (bypassing the override) must be rejected, since that id no
    longer appears in the child's own resolved view."""
    parent = await _register_and_login(client, "Parent Co", "override-parent@acme.test")
    child_id = await _create_child_with_membership(client, parent, "Branch")

    parent_item = await _create_catalog_item(
        client, parent["headers"], name="Shared Item", unit_rate="45.00"
    )
    override = await client.post(
        f"/catalogs/items/{parent_item['id']}/override",
        json=_catalog_item_payload(name="Shared Item", unit_rate="99.00"),
        headers={**parent["headers"], "X-Tenant-ID": child_id},
    )
    assert override.status_code == 201, override.text
    override_item = override.json()

    project = await _create_project(client, {**parent["headers"], "X-Tenant-ID": child_id})
    markup = await _create_markup_profile(client, {**parent["headers"], "X-Tenant-ID": child_id})
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers={**parent["headers"], "X-Tenant-ID": child_id},
    )
    estimate_id = created.json()["id"]

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": override_item["id"], "quantity": "2.00"}]},
        headers={**parent["headers"], "X-Tenant-ID": child_id},
    )
    assert response.status_code == 200, response.text
    line_items = response.json()["line_items"]
    assert line_items[0]["unit_rate_snapshot"] == "99.00"
    assert line_items[0]["line_total"] == "198.00"

    # The parent's original (pre-override) id no longer appears in the
    # child's own resolved view — referencing it directly is rejected the
    # same way a nonexistent id would be.
    rejected = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": parent_item["id"], "quantity": "1.00"}]},
        headers={**parent["headers"], "X-Tenant-ID": child_id},
    )
    assert rejected.status_code == 422, rejected.text


async def test_replace_line_items_cross_tenant_estimate_returns_404(client):
    """A genuinely cross-tenant `estimate_id` in the URL PATH (not a
    cross-tenant `cost_catalog_item_id` inside the body, which
    `test_replace_line_items_cross_tenant_catalog_item_returns_422` above
    already covers as a distinct 422 case) must 404 via
    `_get_estimate_or_404`'s ordinary RLS-backed existence check, the same
    pattern `GET /estimates/{id}` uses."""
    a = await _register_and_login(client, "Company A", "crossest-a@acme.test")
    b = await _register_and_login(client, "Company B", "crossest-b@acme.test")
    project = await _create_project(client, a["headers"])
    markup = await _create_markup_profile(client, a["headers"])
    catalog_item = await _create_catalog_item(client, b["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=a["headers"],
    )
    estimate_id = created.json()["id"]

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item["id"], "quantity": "1.00"}]},
        headers=b["headers"],
    )
    assert response.status_code == 404, response.text


async def test_replace_line_items_snapshotted_check_before_catalog_resolution(client):
    """The 409 (is_snapshotted) check must fire even when the request body
    ALSO contains an invalid cost_catalog_item_id — is_snapshotted is
    checked first, before any catalog resolution happens at all."""
    admin = await _register_and_login(client, "Acme Construction", "snapshotorder-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]
    await _set_estimate_snapshotted_directly(estimate_id)

    response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={
            "items": [
                {
                    "cost_catalog_item_id": "00000000-0000-0000-0000-000000000000",
                    "quantity": "1.00",
                }
            ]
        },
        headers=admin["headers"],
    )
    assert response.status_code == 409, response.text


async def test_replace_line_items_nonexistent_estimate_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "replacemissing-admin@acme.test")
    catalog_item = await _create_catalog_item(client, admin["headers"])

    response = await client.put(
        "/estimates/00000000-0000-0000-0000-000000000000/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item["id"], "quantity": "1.00"}]},
        headers=admin["headers"],
    )
    assert response.status_code == 404


# =============================================================================
# POST /estimates/{id}/send-for-signature, /approve, /reject (Task 2.19)
# =============================================================================


async def _create_calculated_estimate(client, admin, *, unit_rate="45.00", quantity="10.00"):
    """Full real-route setup: project + markup profile + catalog item +
    estimate + a line item + a successful `calculate` run, so `total` is
    non-NULL and `send-for-signature` is legal. Returns the freshly
    calculated estimate body."""
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    catalog_item = await _create_catalog_item(client, admin["headers"], unit_rate=unit_rate)
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text
    estimate_id = created.json()["id"]

    put_response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item["id"], "quantity": quantity}]},
        headers=admin["headers"],
    )
    assert put_response.status_code == 200, put_response.text

    calc_response = await client.post(f"/estimates/{estimate_id}/calculate", headers=admin["headers"])
    assert calc_response.status_code == 200, calc_response.text

    return calc_response.json(), project, catalog_item


async def _advance_to_sent(client, admin, *, unit_rate="45.00", quantity="10.00"):
    """Builds a fully calculated estimate, then sends it for signature via
    the real route, returning the resulting (status='sent') estimate body
    plus the underlying project/catalog_item for callers that need them."""
    estimate, project, catalog_item = await _create_calculated_estimate(
        client, admin, unit_rate=unit_rate, quantity=quantity
    )
    response = await client.post(
        f"/estimates/{estimate['id']}/send-for-signature", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    return response.json(), project, catalog_item


async def _approve_estimate(
    client,
    headers,
    estimate_id,
    *,
    signer_name="Jane Client",
    signer_email="jane-client@example.test",
    content=b"fake-signature-bytes",
):
    return await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": signer_name, "signer_email": signer_email},
        files={"signature_artifact": ("signature.png", content, "image/png")},
        headers=headers,
    )


async def test_send_for_signature_requires_prior_calculation(client):
    admin = await _register_and_login(client, "Acme Construction", "sfs-uncalc-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    estimate_id = created.json()["id"]

    response = await client.post(
        f"/estimates/{estimate_id}/send-for-signature", headers=admin["headers"]
    )
    assert response.status_code == 409, response.text

    # Status is untouched — still 'draft', never became 'sent'.
    get_response = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    assert get_response.json()["status"] == "draft"


async def test_send_for_signature_succeeds_after_calculation(client):
    admin = await _register_and_login(client, "Acme Construction", "sfs-ok-admin@acme.test")
    sent_estimate, _project, _catalog_item = await _advance_to_sent(client, admin)

    assert sent_estimate["status"] == "sent"

    get_response = await client.get(
        f"/estimates/{sent_estimate['id']}", headers=admin["headers"]
    )
    assert get_response.json()["status"] == "sent"


async def test_non_write_roles_cannot_send_for_signature(client):
    """`send-for-signature` is `_WRITE_ROLES` (admin/PM) only — client,
    accountant, and field_crew must all get 403, matching this router's
    existing RBAC coverage pattern for its other `_WRITE_ROLES`-gated
    routes (e.g. `test_replace_line_items_...` above)."""
    admin = await _register_and_login(client, "Acme Construction", "sfs-rbac-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "sfs-rbac-client@acme.test")
    accountant = await _invite_and_login_as(
        client, admin, "accountant", "sfs-rbac-accountant@acme.test"
    )
    field_crew = await _invite_and_login_as(
        client, admin, "field_crew", "sfs-rbac-crew@acme.test"
    )

    for actor in (client_role, accountant, field_crew):
        estimate, _project, _catalog_item = await _create_calculated_estimate(client, admin)
        response = await client.post(
            f"/estimates/{estimate['id']}/send-for-signature", headers=actor["headers"]
        )
        assert response.status_code == 403, response.text


async def test_approve_captures_esignature_and_snapshots(client):
    admin = await _register_and_login(client, "Acme Construction", "approve-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "approve-client@acme.test")
    sent_estimate, _project, _catalog_item = await _advance_to_sent(client, admin)

    response = await _approve_estimate(
        client,
        client_role["headers"],
        sent_estimate["id"],
        signer_name="Jane Client",
        signer_email="jane-client@example.test",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["is_snapshotted"] is True
    assert body["esignature_id"] is not None

    # The e-signature is a REAL, persisted record — not just a set FK.
    esignature_response = await client.get(
        f"/esignatures/{body['esignature_id']}", headers=admin["headers"]
    )
    assert esignature_response.status_code == 200, esignature_response.text
    esignature_body = esignature_response.json()
    assert esignature_body["signer_name"] == "Jane Client"
    assert esignature_body["signer_email"] == "jane-client@example.test"
    assert esignature_body["document_type"] == "estimate"
    assert esignature_body["company_id"] == admin["company_id"]

    audit_rows = await _fetch_audit_rows(admin["company_id"])
    matching = [row for row in audit_rows if row["action"] == "estimate.approved"]
    assert len(matching) == 1
    assert str(matching[0]["entity_id"]) == sent_estimate["id"]


async def test_approve_publishes_estimate_approved_with_expected_payload(client):
    """Same pattern `test_lead_state_machine.py`'s
    `test_transition_into_won_calls_publish_with_the_expected_payload` uses
    for `LEAD_WON`: register a temporary handler on the live
    `app.core.events` dispatcher and confirm it fires with the right
    payload — proving `publish("ESTIMATE_APPROVED", ...)` is a real, wired
    call, not a comment/TODO."""
    admin = await _register_and_login(client, "Acme Construction", "publish-est-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "publish-est-client@acme.test")
    sent_estimate, project, _catalog_item = await _advance_to_sent(client, admin)

    received: list[dict] = []

    async def _capture_handler(**payload):
        received.append(payload)

    events.register("ESTIMATE_APPROVED", _capture_handler)
    response = await _approve_estimate(client, client_role["headers"], sent_estimate["id"])
    assert response.status_code == 200, response.text
    approved_total = response.json()["total"]

    assert len(received) == 1
    payload = received[0]
    assert str(payload["estimate_id"]) == sent_estimate["id"]
    assert str(payload["project_id"]) == project["id"]
    assert str(payload["company_id"]) == admin["company_id"]
    assert str(payload["approved_total"]) == approved_total


async def test_approve_publishes_estimate_approved_with_null_project_id_for_lead_scoped_estimate(
    client,
):
    """`project_id` is documented as nullable in the `ESTIMATE_APPROVED`
    payload — an estimate created against a bare Lead (no Project yet) must
    still publish successfully, with `project_id=None`."""
    admin = await _register_and_login(client, "Acme Construction", "publish-lead-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "publish-lead-client@acme.test")
    lead = await _create_lead(client, admin["headers"])
    await _advance_lead_to(client, admin["headers"], lead["id"], "estimating")
    markup = await _create_markup_profile(client, admin["headers"])
    catalog_item = await _create_catalog_item(client, admin["headers"])
    created = await client.post(
        "/estimates",
        json={"lead_id": lead["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text
    estimate_id = created.json()["id"]
    await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item["id"], "quantity": "1.00"}]},
        headers=admin["headers"],
    )
    await client.post(f"/estimates/{estimate_id}/calculate", headers=admin["headers"])
    sent = await client.post(
        f"/estimates/{estimate_id}/send-for-signature", headers=admin["headers"]
    )
    assert sent.status_code == 200, sent.text

    received: list[dict] = []

    async def _capture_handler(**payload):
        received.append(payload)

    events.register("ESTIMATE_APPROVED", _capture_handler)
    response = await _approve_estimate(client, client_role["headers"], estimate_id)
    assert response.status_code == 200, response.text

    assert len(received) == 1
    assert received[0]["project_id"] is None


async def test_approve_requires_sent_status(client):
    admin = await _register_and_login(client, "Acme Construction", "approve-status-admin@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "approve-status-client@acme.test"
    )
    estimate, _project, _catalog_item = await _create_calculated_estimate(client, admin)
    # Deliberately not sent-for-signature: estimate is still 'draft'.

    response = await _approve_estimate(client, client_role["headers"], estimate["id"])
    assert response.status_code == 409, response.text


async def test_reject_requires_a_reason(client):
    admin = await _register_and_login(client, "Acme Construction", "reject-noreason-admin@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "reject-noreason-client@acme.test"
    )
    sent_estimate, _project, _catalog_item = await _advance_to_sent(client, admin)

    response = await client.post(
        f"/estimates/{sent_estimate['id']}/reject", json={}, headers=client_role["headers"]
    )
    assert response.status_code == 422, response.text

    # Untouched: still 'sent', not 'rejected'.
    get_response = await client.get(
        f"/estimates/{sent_estimate['id']}", headers=admin["headers"]
    )
    assert get_response.json()["status"] == "sent"


async def test_reject_with_reason_succeeds_and_does_not_snapshot(client):
    admin = await _register_and_login(client, "Acme Construction", "reject-ok-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "reject-ok-client@acme.test")
    sent_estimate, _project, _catalog_item = await _advance_to_sent(client, admin)

    response = await client.post(
        f"/estimates/{sent_estimate['id']}/reject",
        json={"reason": "Price is too high for our budget"},
        headers=client_role["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rejected"
    assert body["is_snapshotted"] is False
    assert body["esignature_id"] is None

    audit_rows = await _fetch_audit_rows(admin["company_id"])
    matching = [row for row in audit_rows if row["action"] == "estimate.rejected"]
    assert len(matching) == 1
    assert str(matching[0]["entity_id"]) == sent_estimate["id"]
    metadata = matching[0]["log_metadata"]
    import json as _json

    decoded = _json.loads(metadata) if isinstance(metadata, str) else metadata
    assert decoded == {"reason": "Price is too high for our budget"}


async def test_reject_requires_sent_status(client):
    admin = await _register_and_login(client, "Acme Construction", "reject-status-admin@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "reject-status-client@acme.test"
    )
    estimate, _project, _catalog_item = await _create_calculated_estimate(client, admin)
    # Deliberately not sent-for-signature: estimate is still 'draft'.

    response = await client.post(
        f"/estimates/{estimate['id']}/reject",
        json={"reason": "irrelevant"},
        headers=client_role["headers"],
    )
    assert response.status_code == 409, response.text


async def test_non_client_roles_cannot_approve_or_reject(client):
    admin = await _register_and_login(client, "Acme Construction", "noclient-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "noclient-pm@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "noclient-acct@acme.test")
    field_crew = await _invite_and_login_as(client, admin, "field_crew", "noclient-crew@acme.test")

    for actor in (admin, pm, accountant, field_crew):
        sent_estimate, _project, _catalog_item = await _advance_to_sent(client, admin)

        approve_response = await _approve_estimate(client, actor["headers"], sent_estimate["id"])
        assert approve_response.status_code == 403, approve_response.text

        reject_response = await client.post(
            f"/estimates/{sent_estimate['id']}/reject",
            json={"reason": "should be blocked"},
            headers=actor["headers"],
        )
        assert reject_response.status_code == 403, reject_response.text


async def test_approved_and_snapshotted_estimate_blocks_lines_and_calculate_via_real_flow(client):
    """Task 2.11/2.12 already cover the `is_snapshotted` 409 guard in
    general via `_set_estimate_snapshotted_directly` (a direct SQL
    shortcut). This test goes through the REAL `send-for-signature` ->
    `approve` flow instead, proving the guard actually fires once an
    estimate has been snapshotted the legitimate way, not just when the
    flag is poked directly."""
    admin = await _register_and_login(client, "Acme Construction", "realflow-admin@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "realflow-client@acme.test")
    sent_estimate, _project, catalog_item = await _advance_to_sent(client, admin)

    approve_response = await _approve_estimate(client, client_role["headers"], sent_estimate["id"])
    assert approve_response.status_code == 200, approve_response.text

    put_response = await client.put(
        f"/estimates/{sent_estimate['id']}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item["id"], "quantity": "99.00"}]},
        headers=admin["headers"],
    )
    assert put_response.status_code == 409, put_response.text

    calc_response = await client.post(
        f"/estimates/{sent_estimate['id']}/calculate", headers=admin["headers"]
    )
    assert calc_response.status_code == 409, calc_response.text
