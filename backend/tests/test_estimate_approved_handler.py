"""Task 3.39: ESTIMATE_APPROVED -> draft Invoice wiring (design spec Section
2). Mirrors tests/test_lead_won_drafts_project.py's own structure and
register_event_handlers() discipline.

Two schema/behavior facts drove the exact numbers and helpers used below,
found by reading the real routes/schemas rather than assuming:

1. `POST /estimates/{id}/approve` is gated `require_role("client")`
   (app/routers/estimates.py) — a real, authenticated in-app "client" role
   user, not an anonymous e-signature flow. `get_current_user`
   (app/core/deps.py) 401s with no bearer token at all, so the approve call
   needs a logged-in client-role user's headers, same as
   tests/test_estimates.py's own `_invite_and_login_as` + `_approve_estimate`
   precedent.
2. `PUT /estimates/{id}/lines`' body is `{"items": [...]}`
   (`EstimateLineItemsReplaceRequest`, app/schemas/estimate_line_item.py),
   not a bare list. `POST /catalogs/items`' `category` field is required
   (`CostCatalogItemCreateRequest`, app/schemas/cost_catalog_item.py).

The first test's quantity (8.00 @ $5.00/board_ft = $40.00 subtotal) is
deliberately chosen, not copied from an example: with this company's markup
profile (10% overhead, 15% profit -> a combined 1.265x multiplier,
app/services/estimate_calculation.py), $40.00 * 1.265 = $50.60 total. A
subtotal landing on a total whose cents digit is exactly 5 (e.g. $50.00 ->
$63.25 total) would make the 10% deposit ($6.325) an exact rounding tie at
the third decimal place when stored into invoices.amount's NUMERIC(12,2)
column — an avoidable source of test flakiness across Postgres versions,
not a real assertion about the handler's own correctness. $50.60's clean
10% deposit ($5.06) has no such tie.
"""
import json
import uuid
from decimal import Decimal

import asyncpg

from app.core.event_handlers import register_event_handlers
from tests.conftest import TEST_DATABASE_URL, grant_client_access, register_and_login

ADMIN_CONN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


async def _register_and_login(client, company_name, email):
    """Thin wrapper: this module's tests need the enterprise tier.
    See tests/conftest.py's register_and_login."""
    return await register_and_login(client, company_name, email, tier="enterprise")



async def _invite_and_login_as(client, admin, role, email):
    """Same precedent as tests/test_estimates.py's own helper of the same
    name: `POST /estimates/{id}/approve` is `require_role("client")`-gated,
    so exercising it for real requires a logged-in client-role user, not
    just the admin who set the Estimate up."""
    invite = await client.post(
        "/invitations",
        json={"email": email, "role": role},
        headers=admin["headers"],
    )
    assert invite.status_code == 201, invite.text
    accept = await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Invited Client", "password": "anothersecret123"},
    )
    assert accept.status_code == 200, accept.text
    login = await client.post("/auth/login", json={"email": email, "password": "anothersecret123"})
    assert login.status_code == 200, login.text
    # `email` is needed because migration 0019 rejects a signer_email that
    # isn't the signing account's own.
    # `user_id` so a test can assert WHICH account an audit row names, not
    # merely that one exists.
    members = await client.get("/companies/members", headers=admin["headers"])
    user_id = next(m["user_id"] for m in members.json()["items"] if m["email"] == email)
    return {
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
        "email": email,
        "user_id": user_id,
    }


async def _create_project(client, headers, name="Deposit Project"):
    response = await client.post(
        "/projects", json={"name": name, "site_address": "1 Main St"}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_markup_profile(client, headers):
    response = await client.post(
        "/markup-profiles",
        json={"name": "Standard", "overhead_pct": "10.00", "profit_pct": "15.00"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_catalog_item(client, headers):
    response = await client.post(
        "/catalogs/items",
        json={"category": "materials", "name": "Lumber", "unit": "board_ft", "unit_rate": "5.00"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_and_approve_estimate(
    client, admin, client_role, project_id, markup_profile_id, catalog_item_id, *, quantity
):
    """Takes the whole `admin`/`client_role` login dicts, not just their
    headers: migration 0019 needs both the project-membership grant (a
    client sees nothing on a project they aren't on) and the client's own
    email (the signature block must match the signing account)."""
    admin_headers = admin["headers"]
    client_headers = client_role["headers"]
    await grant_client_access(client, admin, project_id=project_id, email=client_role["email"])

    create = await client.post(
        "/estimates",
        json={"project_id": project_id, "markup_profile_id": markup_profile_id},
        headers=admin_headers,
    )
    assert create.status_code == 201, create.text
    estimate_id = create.json()["id"]

    lines = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item_id, "quantity": quantity}]},
        headers=admin_headers,
    )
    assert lines.status_code == 200, lines.text

    calc = await client.post(f"/estimates/{estimate_id}/calculate", headers=admin_headers)
    assert calc.status_code == 200, calc.text

    send = await client.post(
        f"/estimates/{estimate_id}/send-for-signature",
        headers=admin_headers,
    )
    assert send.status_code == 200, send.text

    files = {"signature_artifact": ("sig.png", b"fake-png-bytes", "image/png")}
    approve = await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": "Client Signer", "signer_email": client_role["email"]},
        files=files,
        headers=client_headers,
    )
    assert approve.status_code == 200, approve.text
    return estimate_id, calc.json()["total"]


async def _fetch_invoices_for_estimate(estimate_id):
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        return await conn.fetch("SELECT * FROM invoices WHERE estimate_id = $1", estimate_id)
    finally:
        await conn.close()


async def _fetch_audit_log_for_invoice(invoice_id):
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        return await conn.fetch(
            "SELECT action, entity_type, entity_id, actor_id, log_metadata "
            "FROM audit_log WHERE entity_type = 'invoice' AND entity_id = $1",
            invoice_id,
        )
    finally:
        await conn.close()


def _decode_metadata(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


async def test_approving_an_estimate_with_a_project_drafts_a_deposit_invoice(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Deposit Co", "deposit-1@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "deposit-client-1@example.test")
    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    estimate_id, total = await _create_and_approve_estimate(
        client,
        admin,
        client_role,
        project["id"],
        markup_profile_id,
        catalog_item_id,
        quantity="8.00",
    )

    invoices = await _fetch_invoices_for_estimate(estimate_id)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice["status"] == "draft"
    assert invoice["project_id"] == uuid.UUID(project["id"])
    assert invoice["company_id"] == uuid.UUID(admin["company_id"])
    assert invoice["estimate_id"] == uuid.UUID(estimate_id)
    assert invoice["due_date"] is None
    assert Decimal(invoice["amount"]) == Decimal(str(total)) * Decimal("0.10")

    audit_rows = await _fetch_audit_log_for_invoice(invoice["id"])
    assert len(audit_rows) == 1
    audit_row = audit_rows[0]
    assert audit_row["action"] == "invoice.auto_generated"
    # Was `is None` — pinning a gap rather than a requirement. The payload
    # now carries the approving client, so this action is attributed the
    # same way whether the invoice came from here or from
    # PROJECT_COMPLETED's handler (M4).
    assert audit_row["actor_id"] == uuid.UUID(client_role["user_id"])
    assert _decode_metadata(audit_row["log_metadata"]) == {"estimate_id": estimate_id}


async def test_approving_an_estimate_enqueues_a_sync_for_the_deposit_invoice(client, monkeypatch):
    """Regression test for a gap found by external design review after Task
    4.13: handle_estimate_approved is the SECOND place Invoices are created
    (create_invoice in app/routers/invoices.py is the first), and it
    originally never published INVOICE_CREATED — so auto-drafted deposit
    invoices silently bypassed accounting sync, which is precisely the
    flagship US-6.2 flow (client signs an Estimate, the deposit invoice
    lands in the accountant's platform)."""
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()

    admin = await _register_and_login(client, "Deposit Co 3", "deposit-3@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "deposit-client-3@example.test")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    connect = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert connect.status_code == 303, connect.text

    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    estimate_id, _total = await _create_and_approve_estimate(
        client,
        admin,
        client_role,
        project["id"],
        markup_profile_id,
        catalog_item_id,
        quantity="8.00",
    )

    invoices = await _fetch_invoices_for_estimate(estimate_id)
    assert len(invoices) == 1

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["entity_type"] == "invoice"
    assert kwargs["entity_id"] == str(invoices[0]["id"])


async def _create_lead(client, headers):
    response = await client.post(
        "/leads",
        json={
            "contact_name": "Bare Lead Contact",
            "project_name": "Bare Lead Job",
            "email": "leadcontact@example.test",
            "project_type": "residential",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _advance_lead_to_estimating(client, headers, lead_id):
    """`POST /estimates` only accepts a Lead in `estimating`/`qualified`/
    `won` status (app/routers/estimates.py's own 422 message, found by
    running this test against a freshly created 'new' Lead first).
    Deliberately stops at `estimating`, not `won` — advancing all the way to
    `won` would fire `LEAD_WON` and draft a Project of its own, which would
    make this "bare Lead, no Project" test no longer bare. Same
    shortest-legal-path pattern as tests/test_estimates.py's own
    `_advance_lead_to`."""
    for step_status in ("contacted", "estimating"):
        response = await client.patch(f"/leads/{lead_id}", json={"status": step_status}, headers=headers)
        assert response.status_code == 200, response.text


async def test_approving_an_estimate_against_a_bare_lead_does_not_create_an_invoice(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Deposit Co 2", "deposit-2@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "deposit-client-2@example.test")
    lead = await _create_lead(client, admin["headers"])
    # A lead-backed estimate has no project, so `lead_clients` is the only
    # membership that can reach it (migration 0019).
    await grant_client_access(
        client, admin, lead_id=lead["id"], email="deposit-client-2@example.test"
    )
    await _advance_lead_to_estimating(client, admin["headers"], lead["id"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    create = await client.post(
        "/estimates",
        json={"lead_id": lead["id"], "markup_profile_id": markup_profile_id},
        headers=admin["headers"],
    )
    assert create.status_code == 201, create.text
    estimate_id = create.json()["id"]

    lines = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": catalog_item_id, "quantity": "5.00"}]},
        headers=admin["headers"],
    )
    assert lines.status_code == 200, lines.text
    calc = await client.post(f"/estimates/{estimate_id}/calculate", headers=admin["headers"])
    assert calc.status_code == 200, calc.text
    send = await client.post(
        f"/estimates/{estimate_id}/send-for-signature",
        headers=admin["headers"],
    )
    assert send.status_code == 200, send.text

    files = {"signature_artifact": ("sig.png", b"fake-png-bytes", "image/png")}
    approve = await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": "Client Signer", "signer_email": client_role["email"]},
        files=files,
        headers=client_role["headers"],
    )
    assert approve.status_code == 200, approve.text

    invoices = await _fetch_invoices_for_estimate(estimate_id)
    assert invoices == [], "an Estimate with no project_id must not auto-generate an Invoice"


async def test_auto_generated_invoice_records_the_client_who_approved(client):
    """M4: `invoice.auto_generated` is written by two different handlers —
    this one (deposit invoice, on ESTIMATE_APPROVED) and
    project_completed_handler (final invoice, on PROJECT_COMPLETED). The
    latter always recorded a real actor; this one hardcoded None, because
    the event payload didn't carry one. So a single audit action answered
    "who did this" two different ways depending on which event fired.

    The actor was never unknown: the approve route's own `estimate.approved`
    audit row right above the publish() call already used
    `current.user.id`.
    """
    register_event_handlers()

    admin = await _register_and_login(client, "Actor Co", "actor-admin@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "actor-client@example.test")
    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    estimate_id, _total = await _create_and_approve_estimate(
        client,
        admin,
        client_role,
        project["id"],
        markup_profile_id,
        catalog_item_id,
        quantity="4.00",
    )

    invoices = await _fetch_invoices_for_estimate(uuid.UUID(estimate_id))
    assert len(invoices) == 1, invoices

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        actor = await conn.fetchval(
            "SELECT actor_id FROM audit_log "
            "WHERE action = 'invoice.auto_generated' AND entity_id = $1",
            invoices[0]["id"],
        )
    finally:
        await conn.close()

    assert actor is not None, (
        "the deposit invoice's audit row has no actor, while the final-invoice "
        "handler records one for the same action"
    )
    assert str(actor) == client_role["user_id"]
