"""ESTIMATE_APPROVED -> BOM auto-generation wiring (design spec Decision
4). Mirrors tests/test_estimate_approved_handler.py's structure and
register_event_handlers() discipline exactly — same event, a second
independent handler.
"""

import uuid

import asyncpg
import pytest

from app.core.event_handlers import register_event_handlers
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
    await set_subscription_tier(register.json()["company_id"], "pro")
    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
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
        json={"full_name": "Invited Client", "password": "anothersecret123"},
    )
    assert accept.status_code == 200, accept.text
    login = await client.post("/auth/login", json={"email": email, "password": "anothersecret123"})
    assert login.status_code == 200, login.text
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def _create_project(client, headers, name="BOM Project"):
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


async def _create_catalog_item(client, headers, *, name="Lumber", unit="board_ft", unit_rate="5.00"):
    response = await client.post(
        "/catalogs/items",
        json={"category": "materials", "name": name, "unit": unit, "unit_rate": unit_rate},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _create_and_approve_estimate(
    client, admin_headers, client_headers, project_id, markup_profile_id, line_items
):
    create = await client.post(
        "/estimates",
        json={"project_id": project_id, "markup_profile_id": markup_profile_id},
        headers=admin_headers,
    )
    assert create.status_code == 201, create.text
    estimate_id = create.json()["id"]

    lines = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": line_items},
        headers=admin_headers,
    )
    assert lines.status_code == 200, lines.text

    calc = await client.post(f"/estimates/{estimate_id}/calculate", headers=admin_headers)
    assert calc.status_code == 200, calc.text

    send = await client.post(f"/estimates/{estimate_id}/send-for-signature", headers=admin_headers)
    assert send.status_code == 200, send.text

    files = {"signature_artifact": ("sig.png", b"fake-png-bytes", "image/png")}
    approve = await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": "Client Signer", "signer_email": "bom-client@example.test"},
        files=files,
        headers=client_headers,
    )
    assert approve.status_code == 200, approve.text
    return estimate_id


async def _fetch_bom_lines(project_id):
    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        return await conn.fetch(
            "SELECT * FROM bom_lines WHERE project_id = $1 ORDER BY description", project_id
        )
    finally:
        await conn.close()


async def test_approving_an_estimate_creates_bom_lines(client):
    register_event_handlers()

    admin = await _register_and_login(client, "BOM Co", "bom-1@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "bom-client-1@example.test")
    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    await _create_and_approve_estimate(
        client,
        admin["headers"],
        client_role["headers"],
        project["id"],
        markup_profile_id,
        [{"cost_catalog_item_id": catalog_item_id, "quantity": "8.00"}],
    )

    lines = await _fetch_bom_lines(project["id"])
    assert len(lines) == 1
    assert lines[0]["description"] == "Lumber"
    assert lines[0]["unit"] == "board_ft"
    assert lines[0]["quantity"] == 8
    assert lines[0]["source"] == "estimate"
    assert lines[0]["ordered"] is False


async def test_second_approved_estimate_merges_quantity_for_same_catalog_item(client):
    register_event_handlers()

    admin = await _register_and_login(client, "BOM Co 2", "bom-2@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "bom-client-2@example.test")
    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    await _create_and_approve_estimate(
        client,
        admin["headers"],
        client_role["headers"],
        project["id"],
        markup_profile_id,
        [{"cost_catalog_item_id": catalog_item_id, "quantity": "8.00"}],
    )
    await _create_and_approve_estimate(
        client,
        admin["headers"],
        client_role["headers"],
        project["id"],
        markup_profile_id,
        [{"cost_catalog_item_id": catalog_item_id, "quantity": "3.00"}],
    )

    lines = await _fetch_bom_lines(project["id"])
    assert len(lines) == 1, "same catalog item across two estimates should merge into one line"
    assert lines[0]["quantity"] == 11


async def test_two_different_catalog_items_produce_two_lines(client):
    register_event_handlers()

    admin = await _register_and_login(client, "BOM Co 3", "bom-3@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "bom-client-3@example.test")
    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    lumber_id = await _create_catalog_item(client, admin["headers"], name="Lumber")
    drywall_id = await _create_catalog_item(
        client, admin["headers"], name="Drywall", unit="sheet", unit_rate="12.00"
    )

    await _create_and_approve_estimate(
        client,
        admin["headers"],
        client_role["headers"],
        project["id"],
        markup_profile_id,
        [
            {"cost_catalog_item_id": lumber_id, "quantity": "8.00"},
            {"cost_catalog_item_id": drywall_id, "quantity": "4.00"},
        ],
    )

    lines = await _fetch_bom_lines(project["id"])
    assert len(lines) == 2
    assert {line["description"] for line in lines} == {"Lumber", "Drywall"}


async def _create_lead(client, headers):
    response = await client.post(
        "/leads",
        json={
            "contact_name": "Bare Lead Contact",
            "project_name": "Bare Lead Job",
            "email": "bom-leadcontact@example.test",
            "project_type": "residential",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _advance_lead_to_estimating(client, headers, lead_id):
    for step_status in ("contacted", "estimating"):
        response = await client.patch(f"/leads/{lead_id}", json={"status": step_status}, headers=headers)
        assert response.status_code == 200, response.text


async def test_approving_an_estimate_against_a_bare_lead_creates_no_bom_lines(client):
    register_event_handlers()

    admin = await _register_and_login(client, "BOM Co 4", "bom-4@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "bom-client-4@example.test")
    lead = await _create_lead(client, admin["headers"])
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
    send = await client.post(f"/estimates/{estimate_id}/send-for-signature", headers=admin["headers"])
    assert send.status_code == 200, send.text

    files = {"signature_artifact": ("sig.png", b"fake-png-bytes", "image/png")}
    approve = await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": "Client Signer", "signer_email": "bare-bom-client@example.test"},
        files=files,
        headers=client_role["headers"],
    )
    assert approve.status_code == 200, approve.text

    conn = await asyncpg.connect(ADMIN_CONN_DSN)
    try:
        rows = await conn.fetch("SELECT * FROM bom_lines WHERE project_id IS NULL")
    finally:
        await conn.close()
    assert rows == [], "a lead-only Estimate has no project_id — no BomLine can be created"


async def test_handler_no_ops_when_tier_does_not_allow_estimation(client, db_session):
    from app.services.bom_generation_handler import handle_estimate_approved_bom

    register_event_handlers()

    admin = await _register_and_login(client, "BOM Co 6", "bom-6@example.test")
    client_role = await _invite_and_login_as(client, admin, "client", "bom-client-6@example.test")
    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    estimate_id = await _create_and_approve_estimate(
        client,
        admin["headers"],
        client_role["headers"],
        project["id"],
        markup_profile_id,
        [{"cost_catalog_item_id": catalog_item_id, "quantity": "8.00"}],
    )
    lines_after_pro_approval = await _fetch_bom_lines(project["id"])
    assert len(lines_after_pro_approval) == 1

    await set_subscription_tier(admin["company_id"], "starter")

    # Call the handler directly with a second, larger quantity — if tier
    # gating were missing, this would merge into the existing line and
    # bump its quantity to 20.00.
    await handle_estimate_approved_bom(
        session=db_session,
        estimate_id=estimate_id,
        project_id=project["id"],
        company_id=uuid.UUID(admin["company_id"]),
        approved_total=None,
    )
    await db_session.commit()

    lines_after_starter_call = await _fetch_bom_lines(project["id"])
    assert len(lines_after_starter_call) == 1
    assert lines_after_starter_call[0]["quantity"] == 8, "starter tier must block the handler's writes"
