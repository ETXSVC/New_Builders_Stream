"""Regression tests for the company_id-stamping bug class, applied to the
three sites a full-project review found still violating it after six
earlier rounds of fixes elsewhere:

  1. EstimateLineItem  (PUT /estimates/{id}/lines)
  2. Esignature        (POST /estimates/{id}/approve)
  3. CommunicationLog  (POST /leads/{id}/communications)

The bug: a PARENT-branch session may legitimately act on a DESCENDANT
branch's estimate/lead (RLS's get_all_descendant_ids grant makes the row
visible without switching X-Tenant-ID). Stamping the acting session's
company on the nested row instead of the parent record's own company makes
that row invisible under RLS to a session scoped directly to the branch
that owns the parent — the child sees its estimate with zero line items,
or its signed estimate pointing at an esignature row it cannot read
(breaking the ESIGN evidence chain precisely where it matters).

Each test asserts the persisted company_id equals the PARENT RECORD's, read
back through the owner connection the way every other test in this suite
inspects cross-tenant state.
"""
import uuid

import asyncpg

from tests.conftest import TEST_DATABASE_URL, grant_client_access
from tests.test_estimates import (
    _add_membership_directly,
    _create_child_with_membership,
    _register_and_login,
)

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


async def _fetch_scalar(query, *args):
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _child_headers(parent, child_id):
    """Same admin token, acting as the child branch via X-Tenant-ID."""
    return {**parent["headers"], "X-Tenant-ID": child_id}


async def test_estimate_line_items_are_stamped_with_the_estimates_company(client):
    parent = await _register_and_login(client, "Stamp Parent 1", "stamp-1@example.test")
    child_id = await _create_child_with_membership(client, parent, "Stamp Child 1")
    child_headers = await _child_headers(parent, child_id)

    # Build the estimate AS THE CHILD, so the estimate's own company_id is
    # the child's.
    project = await client.post(
        "/projects", json={"name": "Child House", "site_address": "1 Oak"}, headers=child_headers
    )
    assert project.status_code == 201, project.text
    markup = await client.post(
        "/markup-profiles",
        json={"name": "Std", "overhead_pct": "10.00", "profit_pct": "15.00"},
        headers=child_headers,
    )
    item = await client.post(
        "/catalogs/items",
        json={"category": "materials", "name": "Lumber", "unit": "board_ft", "unit_rate": "5.00"},
        headers=child_headers,
    )
    estimate = await client.post(
        "/estimates",
        json={"project_id": project.json()["id"], "markup_profile_id": markup.json()["id"]},
        headers=child_headers,
    )
    assert estimate.status_code == 201, estimate.text
    estimate_id = estimate.json()["id"]

    # ...but write the line items AS THE PARENT (no X-Tenant-ID switch).
    lines = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": item.json()["id"], "quantity": "3.00"}]},
        headers=parent["headers"],
    )
    assert lines.status_code == 200, lines.text

    stamped = await _fetch_scalar(
        "SELECT company_id FROM estimate_line_items WHERE estimate_id = $1",
        uuid.UUID(estimate_id),
    )
    assert str(stamped) == child_id, (
        "line items must carry the ESTIMATE's company, or the branch that owns "
        "the estimate sees it with zero lines under RLS"
    )


async def test_esignature_is_stamped_with_the_estimates_company(client):
    parent = await _register_and_login(client, "Stamp Parent 2", "stamp-2@example.test")
    child_id = await _create_child_with_membership(client, parent, "Stamp Child 2")
    child_headers = await _child_headers(parent, child_id)

    project = await client.post(
        "/projects", json={"name": "Sign House", "site_address": "2 Oak"}, headers=child_headers
    )
    markup = await client.post(
        "/markup-profiles",
        json={"name": "Std", "overhead_pct": "10.00", "profit_pct": "15.00"},
        headers=child_headers,
    )
    item = await client.post(
        "/catalogs/items",
        json={"category": "materials", "name": "Lumber", "unit": "board_ft", "unit_rate": "5.00"},
        headers=child_headers,
    )
    estimate = await client.post(
        "/estimates",
        json={"project_id": project.json()["id"], "markup_profile_id": markup.json()["id"]},
        headers=child_headers,
    )
    estimate_id = estimate.json()["id"]
    await client.put(
        f"/estimates/{estimate_id}/lines",
        json={"items": [{"cost_catalog_item_id": item.json()["id"], "quantity": "3.00"}]},
        headers=child_headers,
    )
    await client.post(f"/estimates/{estimate_id}/calculate", headers=child_headers)
    await client.post(f"/estimates/{estimate_id}/send-for-signature", headers=child_headers)

    # A client-role user in the PARENT company approves (parent tenant
    # context), against the CHILD's estimate.
    invite = await client.post(
        "/invitations",
        json={"email": "stamp-2-client@example.test", "role": "client"},
        headers=parent["headers"],
    )
    await client.post(
        f"/invitations/{invite.json()['id']}/accept",
        json={"full_name": "Signer", "password": "anothersecret123"},
    )
    login = await client.post(
        "/auth/login",
        json={"email": "stamp-2-client@example.test", "password": "anothersecret123"},
    )
    client_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # Migration 0019: granted by the PARENT admin against the CHILD's
    # project. The membership row carries the child's company_id (the parent
    # record's — the same stamping rule this file exists to pin down), and
    # RLS's descendant grant is what makes it readable from the parent
    # context the signer is acting in.
    await grant_client_access(
        client, parent, project_id=project.json()["id"], email="stamp-2-client@example.test"
    )

    approve = await client.post(
        f"/estimates/{estimate_id}/approve",
        data={"signer_name": "Signer", "signer_email": "stamp-2-client@example.test"},
        files={"signature_artifact": ("sig.png", b"fake-png-bytes", "image/png")},
        headers=client_headers,
    )
    assert approve.status_code == 200, approve.text

    stamped = await _fetch_scalar(
        "SELECT e.company_id FROM esignatures e "
        "JOIN estimates est ON est.esignature_id = e.id WHERE est.id = $1",
        uuid.UUID(estimate_id),
    )
    assert str(stamped) == child_id, (
        "the esignature must carry the ESTIMATE's company — otherwise the branch "
        "that owns the signed document cannot read its own ESIGN evidence"
    )


async def test_communication_log_is_stamped_with_the_leads_company(client):
    parent = await _register_and_login(client, "Stamp Parent 3", "stamp-3@example.test")
    child_id = await _create_child_with_membership(client, parent, "Stamp Child 3")
    child_headers = await _child_headers(parent, child_id)

    lead = await client.post(
        "/leads",
        json={
            "contact_name": "Child Contact",
            "project_name": "Child Job",
            "email": "child-contact@example.test",
            "project_type": "residential",
        },
        headers=child_headers,
    )
    assert lead.status_code == 201, lead.text
    lead_id = lead.json()["id"]

    logged = await client.post(
        f"/leads/{lead_id}/communications",
        json={"channel": "call", "body": "Discussed scope"},
        headers=parent["headers"],
    )
    assert logged.status_code == 201, logged.text

    stamped = await _fetch_scalar(
        "SELECT company_id FROM communication_logs WHERE lead_id = $1", uuid.UUID(lead_id)
    )
    assert str(stamped) == child_id, (
        "the communication log must carry the LEAD's company, or the branch that "
        "owns the lead cannot see its own communication history"
    )


async def test_branding_logo_upload_is_size_capped(client, monkeypatch):
    """branding.py was the one multipart route the upload-cap sweep missed —
    it read the whole body into memory before the (post-read) 2 MiB check."""
    from app.config import settings

    monkeypatch.setattr(settings, "max_document_upload_bytes", 1024)
    admin = await _register_and_login(client, "Branding Cap Co", "branding-cap@example.test")

    response = await client.post(
        "/companies/branding/logo",
        files={"file": ("logo.png", b"x" * 2048, "image/png")},
        headers=admin["headers"],
    )
    assert response.status_code == 413, response.text


# _add_membership_directly is imported for its side-effect-free reuse by
# _create_child_with_membership; referenced here so linters see the import
# as intentional.
assert callable(_add_membership_directly)
