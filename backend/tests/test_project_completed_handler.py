"""PROJECT_COMPLETED -> final-invoice wiring. Mirrors
tests/test_estimate_approved_handler.py's structure and
register_event_handlers() discipline, importing that file's helpers rather
than duplicating them (same precedent test_tier_gating.py set).

The numbers below reuse that file's deliberately chosen $50.60 total
(quantity 8.00 @ $5.00 through the 10%/15% markup profile — see its
docstring for why that avoids a NUMERIC(12,2) rounding tie): approving the
estimate auto-drafts a 10% deposit ($5.06), so the final invoice the
completion handler drafts is $50.60 - $5.06 = $45.54.
"""
import uuid
from decimal import Decimal

import asyncpg

from app.core.event_handlers import register_event_handlers
from tests.conftest import TEST_DATABASE_URL
from tests.test_estimate_approved_handler import (
    _create_and_approve_estimate,
    _create_catalog_item,
    _create_markup_profile,
    _create_project,
    _decode_metadata,
    _fetch_invoices_for_estimate,
    _invite_and_login_as,
    _register_and_login,
)

ADMIN_CONN_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


async def _fetch_invoices_for_project(project_id):
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        return await conn.fetch(
            "SELECT * FROM invoices WHERE project_id = $1 ORDER BY created_at",
            uuid.UUID(project_id),
        )
    finally:
        await conn.close()


async def _fetch_audit_rows(invoice_id):
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        return await conn.fetch(
            "SELECT action, actor_id, log_metadata FROM audit_log "
            "WHERE entity_type = 'invoice' AND entity_id = $1",
            invoice_id,
        )
    finally:
        await conn.close()


async def _complete_project(client, headers, project_id, *, from_status="draft"):
    """Walks the shortest legal transition chain from `from_status` to
    `completed` (PROJECT_TRANSITIONS: draft -> pre_construction -> active ->
    completed) via the real route, so the publish site under test actually
    fires."""
    chain = {"draft": ("pre_construction", "active", "completed"), "active": ("completed",)}[from_status]
    for step in chain:
        response = await client.patch(
            f"/projects/{project_id}/status", json={"status": step}, headers=headers
        )
        assert response.status_code == 200, response.text


async def _activate_project(client, headers, project_id):
    for step in ("pre_construction", "active"):
        response = await client.patch(
            f"/projects/{project_id}/status", json={"status": step}, headers=headers
        )
        assert response.status_code == 200, response.text


async def test_completing_a_project_drafts_a_final_invoice_for_the_remainder(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Final Inv Co 1", "final-1@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "final-1-client@example.test")
    project = await _create_project(client, admin["headers"], name="Final Invoice Project")
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    estimate_id, total = await _create_and_approve_estimate(
        client,
        admin["headers"],
        client_role["headers"],
        project["id"],
        markup_profile_id,
        catalog_item_id,
        quantity="8.00",
    )
    assert Decimal(str(total)) == Decimal("50.60")

    await _complete_project(client, admin["headers"], project["id"])

    invoices = await _fetch_invoices_for_project(project["id"])
    # Deposit (drafted on approval) + final (drafted on completion).
    assert len(invoices) == 2
    final = invoices[-1]
    assert final["status"] == "draft"
    assert final["estimate_id"] is None
    assert final["due_date"] is None
    assert final["company_id"] == uuid.UUID(admin["company_id"])
    assert Decimal(final["amount"]) == Decimal("45.54")  # 50.60 - 5.06 deposit

    audit_rows = await _fetch_audit_rows(final["id"])
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["action"] == "invoice.auto_generated"
    # Unlike ESTIMATE_APPROVED's handler (whose payload carries no actor),
    # PROJECT_COMPLETED's publish forwards current.user.id.
    assert row["actor_id"] == uuid.UUID(admin["user_id"])
    assert _decode_metadata(row["log_metadata"]) == {
        "trigger": "project_completed",
        "project_id": project["id"],
    }


async def test_approved_change_orders_add_to_the_final_invoice(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Final Inv Co 2", "final-2@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "final-2-client@example.test")
    project = await _create_project(client, admin["headers"], name="CO Final Project")
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    await _create_and_approve_estimate(
        client,
        admin["headers"],
        client_role["headers"],
        project["id"],
        markup_profile_id,
        catalog_item_id,
        quantity="8.00",
    )

    # Change Orders can only be created against an active Project.
    await _activate_project(client, admin["headers"], project["id"])
    create_co = await client.post(
        f"/projects/{project['id']}/change-orders",
        json={"description": "Extra framing", "cost_delta": "10.00", "schedule_impact_days": 0},
        headers=admin["headers"],
    )
    assert create_co.status_code == 201, create_co.text
    co_id = create_co.json()["id"]
    send_co = await client.post(
        f"/change-orders/{co_id}/send-for-signature", headers=admin["headers"]
    )
    assert send_co.status_code == 200, send_co.text
    approve_co = await client.post(
        f"/change-orders/{co_id}/approve",
        data={"signer_name": "Client Signer", "signer_email": "final-2-client@example.test"},
        files={"signature_artifact": ("sig.png", b"fake-png-bytes", "image/png")},
        headers=client_role["headers"],
    )
    assert approve_co.status_code == 200, approve_co.text

    await _complete_project(client, admin["headers"], project["id"], from_status="active")

    invoices = await _fetch_invoices_for_project(project["id"])
    assert len(invoices) == 2
    # 50.60 + 10.00 change order - 5.06 deposit = 55.54
    assert Decimal(invoices[-1]["amount"]) == Decimal("55.54")


async def test_completing_a_project_with_no_approved_estimate_drafts_nothing(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Final Inv Co 3", "final-3@example.test")
    project = await _create_project(client, admin["headers"], name="No Estimate Project")

    await _complete_project(client, admin["headers"], project["id"])

    invoices = await _fetch_invoices_for_project(project["id"])
    assert invoices == [], "a project with no approved estimate must not auto-draft a final invoice"


async def test_completing_a_fully_invoiced_project_drafts_nothing(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Final Inv Co 4", "final-4@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "final-4-client@example.test")
    project = await _create_project(client, admin["headers"], name="Fully Invoiced Project")
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    await _create_and_approve_estimate(
        client,
        admin["headers"],
        client_role["headers"],
        project["id"],
        markup_profile_id,
        catalog_item_id,
        quantity="8.00",
    )

    # Manually invoice the exact remainder, so contracted == invoiced.
    manual = await client.post(
        f"/projects/{project['id']}/invoices",
        json={"amount": "45.54"},
        headers=admin["headers"],
    )
    assert manual.status_code == 201, manual.text

    await _complete_project(client, admin["headers"], project["id"])

    invoices = await _fetch_invoices_for_project(project["id"])
    assert len(invoices) == 2, "no third invoice: the remainder was already fully invoiced"


async def test_voided_invoices_do_not_count_as_already_invoiced(client):
    register_event_handlers()

    admin = await _register_and_login(client, "Final Inv Co 5", "final-5@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "final-5-client@example.test")
    project = await _create_project(client, admin["headers"], name="Voided Deposit Project")
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    estimate_id, _total = await _create_and_approve_estimate(
        client,
        admin["headers"],
        client_role["headers"],
        project["id"],
        markup_profile_id,
        catalog_item_id,
        quantity="8.00",
    )

    deposit = (await _fetch_invoices_for_estimate(estimate_id))[0]
    void = await client.post(f"/invoices/{deposit['id']}/void", headers=admin["headers"])
    assert void.status_code == 200, void.text

    await _complete_project(client, admin["headers"], project["id"])

    invoices = await _fetch_invoices_for_project(project["id"])
    assert len(invoices) == 2
    # The voided deposit doesn't count toward "already invoiced", so the
    # final invoice covers the full contracted total.
    assert Decimal(invoices[-1]["amount"]) == Decimal("50.60")
