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
