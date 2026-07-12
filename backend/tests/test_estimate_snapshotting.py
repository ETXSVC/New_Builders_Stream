"""Task 2.19: historical-immutability regression test — the single most
important test in this task, per the plan's own Test Strategy Section 3
callout.

Proves that once an Estimate is approved and snapshotted
(`is_snapshotted=True`), its `subtotal`/`total`/line items' `line_total`/
`unit_rate_snapshot` genuinely DECOUPLE from the live `cost_catalog_items`
table — not merely that the `is_snapshotted` flag got set (Task 2.11/2.12's
own tests already cover that the flag blocks further writes; this file
proves the DATA itself stops tracking the catalog once the flag is true).

Every scenario here goes through real HTTP calls via the `client` fixture
for the estimate lifecycle itself (create -> lines -> calculate ->
send-for-signature -> approve), matching `test_estimates.py`'s established
discipline. Only the "change the catalog item's rate out from under an
already-approved estimate" step uses a direct DB write — there is no API
route that mutates `cost_catalog_items.unit_rate` in place (Task 2.3's
`PATCH /catalogs/items/{id}` may or may not exist depending on how it was
scoped; either way, a raw SQL UPDATE against the owner connection is the
simplest, most direct way to prove the point regardless of what mutation
routes exist, and keeps this test focused on the snapshot behavior, not on
which catalog-mutation route is available).
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


async def _update_catalog_item_unit_rate_directly(catalog_item_id, new_unit_rate: Decimal) -> None:
    """No API route mutates an existing `cost_catalog_items.unit_rate` row
    in place used by this test — a raw SQL UPDATE against the Postgres
    owner connection (bypassing RLS, same test-cleanup-only justification
    `tests/conftest.py`'s `_clean_tables` fixture and `test_estimates.py`'s
    `_set_estimate_status_directly` already use) is the simplest, most
    direct way to simulate "the live catalog changed after this Estimate
    was approved" without depending on which catalog-mutation route(s)
    happen to exist."""
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "UPDATE cost_catalog_items SET unit_rate = $1 WHERE id = $2",
            new_unit_rate,
            catalog_item_id,
        )
    finally:
        await conn.close()


async def test_approved_estimate_totals_and_line_items_are_immune_to_later_catalog_price_changes(
    client,
):
    """The core regression test (Test Strategy Section 3's own callout).

    1. Build a real, multi-line Estimate against two distinct catalog
       items, calculate it, send it for signature, and approve it — all
       through real HTTP routes, exactly the way a genuine approval would
       happen.
    2. Capture `subtotal`/`total`/every line item's `line_total`/
       `unit_rate_snapshot` at that moment.
    3. Directly UPDATE `cost_catalog_items.unit_rate` for ONE of the two
       items actually used in this Estimate's line items — simulating a
       price change to the live catalog that happens well after approval.
    4. Re-fetch the approved Estimate via `GET /estimates/{id}` and assert
       every one of those captured values is BYTE-FOR-BYTE unchanged.

    If snapshotting only set the `is_snapshotted` flag without genuinely
    decoupling the stored `unit_rate_snapshot`/`line_total`/`subtotal`/
    `total` values from the live catalog (e.g. if `GET /estimates/{id}`
    ever re-joined against `cost_catalog_items.unit_rate` instead of
    reading the persisted `EstimateLineItem.unit_rate_snapshot` column),
    this test would catch it: the re-fetch below would show a changed
    `unit_rate_snapshot`/`line_total`/`total` for the line item whose
    underlying catalog price was changed in step 3.
    """
    admin = await _register_and_login(client, "Acme Construction", "snaphist-admin@acme.test")
    client_role = await _invite_and_login_as(
        client, admin, "client", "snaphist-client@acme.test"
    )
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(
        client, admin["headers"], overhead_pct="10.00", profit_pct="15.00"
    )
    item_a = await _create_catalog_item(
        client, admin["headers"], category="framing", name="2x4 Lumber", unit_rate="45.00"
    )
    item_b = await _create_catalog_item(
        client, admin["headers"], category="electrical", name="Wiring", unit_rate="20.00"
    )

    created = await client.post(
        "/estimates",
        json={"project_id": project["id"], "markup_profile_id": markup["id"]},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text
    estimate_id = created.json()["id"]

    put_response = await client.put(
        f"/estimates/{estimate_id}/lines",
        json={
            "items": [
                {"cost_catalog_item_id": item_a["id"], "quantity": "10.00"},
                {"cost_catalog_item_id": item_b["id"], "quantity": "5.00"},
            ]
        },
        headers=admin["headers"],
    )
    assert put_response.status_code == 200, put_response.text

    calc_response = await client.post(
        f"/estimates/{estimate_id}/calculate", headers=admin["headers"]
    )
    assert calc_response.status_code == 200, calc_response.text

    sent_response = await client.post(
        f"/estimates/{estimate_id}/send-for-signature", headers=admin["headers"]
    )
    assert sent_response.status_code == 200, sent_response.text

    approve_response = await _approve_estimate(client, client_role["headers"], estimate_id)
    assert approve_response.status_code == 200, approve_response.text
    approved_body = approve_response.json()
    assert approved_body["is_snapshotted"] is True

    # Capture the just-approved state directly from GET /estimates/{id},
    # not from the approve response — GET is the route production code
    # actually uses to display an approved estimate, and is the one this
    # test is ultimately protecting.
    pre_change = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    assert pre_change.status_code == 200, pre_change.text
    pre_change_body = pre_change.json()

    captured_subtotal = pre_change_body["subtotal"]
    captured_total = pre_change_body["total"]
    captured_line_items = {
        li["cost_catalog_item_id"]: {
            "line_total": li["line_total"],
            "unit_rate_snapshot": li["unit_rate_snapshot"],
            "quantity": li["quantity"],
        }
        for li in pre_change_body["line_items"]
    }
    assert len(captured_line_items) == 2
    # Sanity: the captured snapshot values reflect the ORIGINAL catalog
    # rates, before anything has changed.
    assert captured_line_items[item_a["id"]]["unit_rate_snapshot"] == "45.00"
    assert captured_line_items[item_a["id"]]["line_total"] == "450.00"
    assert captured_line_items[item_b["id"]]["unit_rate_snapshot"] == "20.00"
    assert captured_line_items[item_b["id"]]["line_total"] == "100.00"

    # The live catalog price for item_a changes drastically AFTER approval —
    # this must have zero effect on the already-approved Estimate.
    await _update_catalog_item_unit_rate_directly(item_a["id"], Decimal("999.99"))

    # Sanity check the catalog item itself really did change, via the
    # ordinary read route — proving the UPDATE actually took effect and
    # this isn't a false-negative test that never touched the DB.
    catalog_check = await client.get("/catalogs/items", headers=admin["headers"])
    assert catalog_check.status_code == 200, catalog_check.text
    changed_item = next(
        row for row in catalog_check.json()["items"] if row["id"] == item_a["id"]
    )
    assert changed_item["unit_rate"] == "999.99"

    # Re-fetch the approved Estimate and assert every captured value is
    # byte-for-byte unchanged — the entire point of this test.
    post_change = await client.get(f"/estimates/{estimate_id}", headers=admin["headers"])
    assert post_change.status_code == 200, post_change.text
    post_change_body = post_change.json()

    assert post_change_body["subtotal"] == captured_subtotal
    assert post_change_body["total"] == captured_total

    post_change_line_items = {
        li["cost_catalog_item_id"]: {
            "line_total": li["line_total"],
            "unit_rate_snapshot": li["unit_rate_snapshot"],
            "quantity": li["quantity"],
        }
        for li in post_change_body["line_items"]
    }
    assert post_change_line_items == captured_line_items
    # Explicitly re-assert the changed item's snapshot is STILL the
    # original 45.00, not the new live 999.99 — the single most direct
    # assertion this test exists to make.
    assert post_change_line_items[item_a["id"]]["unit_rate_snapshot"] == "45.00"
    assert post_change_line_items[item_a["id"]]["line_total"] == "450.00"

    # And POST /estimates/{id}/calculate (the route that WOULD recompute
    # from live catalog data) is itself blocked outright on a snapshotted
    # estimate — reinforcing that there is no path, direct or indirect,
    # by which the new catalog price could ever reach this Estimate.
    recalc_attempt = await client.post(
        f"/estimates/{estimate_id}/calculate", headers=admin["headers"]
    )
    assert recalc_attempt.status_code == 409, recalc_attempt.text
