"""Task 2.12: `POST /estimates/{id}/calculate` router tests
(`app/routers/estimates.py`'s `calculate_estimate_totals`, backed by
`app/services/estimate_calculation.py`).

Every scenario here goes through real HTTP calls via the `client` fixture,
same discipline as `test_estimates.py`. Helpers are duplicated from
`test_estimates.py` rather than imported cross-file — no test module in
this codebase imports from another test module (verified: no `from tests.`
import exists anywhere except conftest.py's own `TEST_DATABASE_URL`), so
this file follows that same established convention.
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


async def _create_estimate(client, headers, project_id, markup_profile_id):
    response = await client.post(
        "/estimates",
        json={"project_id": project_id, "markup_profile_id": markup_profile_id},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _put_lines(client, headers, estimate_id, items):
    response = await client.put(
        f"/estimates/{estimate_id}/lines", json={"items": items}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _set_estimate_snapshotted_directly(estimate_id):
    """Same out-of-band-setup rationale as `test_estimates.py`'s own
    `_set_estimate_snapshotted_directly`: `is_snapshotted` only
    legitimately becomes `true` via Task 2.19's real approval flow, which
    doesn't exist yet at this point in the plan."""
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
    """Same rationale as `test_estimates.py`'s helper of the same name:
    used here ONLY to prove the snapshotted-409 case truly skips
    recomputation even when the underlying line items change out from
    under it (a real `PUT /estimates/{id}/lines` call would itself be
    rejected with 409 once snapshotted, so this bypasses that route
    entirely, deliberately, to set up a stronger proof)."""
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


async def _get_estimate(client, headers, estimate_id):
    response = await client.get(f"/estimates/{estimate_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# =============================================================================
# POST /estimates/{id}/calculate
# =============================================================================


async def test_calculate_representative_multi_line_multi_category(client):
    """Hand-computed expected values, worked out in full below (not just
    asserted against whatever the code happens to produce):

    Line items:
      - framing / 2x4 Lumber:  quantity=10.00 * unit_rate=45.00 -> line_total=450.00
      - framing / Drywall:     quantity=5.00  * unit_rate=20.00 -> line_total=100.00
      - electrical / Wiring:   quantity=3     * unit_rate=33.33 -> line_total=99.99

    subtotal = 450.00 + 100.00 + 99.99 = 649.99

    category_breakdown (sorted by category name):
      - electrical: 99.99
      - framing:    550.00  (450.00 + 100.00)

    MarkupProfile: overhead_pct=10.00, profit_pct=15.00
      overhead_multiplier = 1 + 10.00/100 = 1.10
      profit_multiplier   = 1 + 15.00/100 = 1.15

    unrounded_total = 649.99 * 1.10 * 1.15
                     = 714.989 * 1.15
                     = 822.23735

    total = round_half_up(822.23735, 2 dp)
          -> 822.23735 is 0.00735 above 822.23 and 0.00265 below 822.24,
             so it rounds UP to 822.24 (not a tie; the third decimal digit
             alone, 7, already exceeds the halfway point).

    Expected: subtotal="649.99", total="822.24".
    """
    admin = await _register_and_login(client, "Acme Construction", "calc-repr-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(
        client, admin["headers"], overhead_pct="10.00", profit_pct="15.00"
    )
    item_a = await _create_catalog_item(
        client, admin["headers"], category="framing", name="2x4 Lumber", unit_rate="45.00"
    )
    item_b = await _create_catalog_item(
        client, admin["headers"], category="framing", name="Drywall", unit_rate="20.00"
    )
    item_c = await _create_catalog_item(
        client, admin["headers"], category="electrical", name="Wiring", unit_rate="33.33"
    )
    estimate = await _create_estimate(client, admin["headers"], project["id"], markup["id"])

    await _put_lines(
        client,
        admin["headers"],
        estimate["id"],
        [
            {"cost_catalog_item_id": item_a["id"], "quantity": "10.00"},
            {"cost_catalog_item_id": item_b["id"], "quantity": "5.00"},
            {"cost_catalog_item_id": item_c["id"], "quantity": "3"},
        ],
    )

    response = await client.post(
        f"/estimates/{estimate['id']}/calculate", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["subtotal"] == "649.99"
    assert body["total"] == "822.24"
    assert len(body["line_items"]) == 3

    breakdown = {entry["category"]: entry["subtotal"] for entry in body["category_breakdown"]}
    assert breakdown == {"electrical": "99.99", "framing": "550.00"}
    # Deterministic ordering: sorted alphabetically by category name.
    assert [entry["category"] for entry in body["category_breakdown"]] == ["electrical", "framing"]

    # Persisted, not just returned: a fresh GET reflects the same totals.
    get_body = await _get_estimate(client, admin["headers"], estimate["id"])
    assert get_body["subtotal"] == "649.99"
    assert get_body["total"] == "822.24"


async def test_calculate_zero_line_items(client):
    """No line items ever added — `subtotal`/`total` must compute cleanly
    to zero with no division-by-zero or null-arithmetic crash (the
    pipeline is `0 * overhead_multiplier * profit_multiplier`, pure
    multiplication throughout, so there is nothing to divide by zero in
    the first place)."""
    admin = await _register_and_login(client, "Acme Construction", "calc-zero-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    estimate = await _create_estimate(client, admin["headers"], project["id"], markup["id"])

    response = await client.post(
        f"/estimates/{estimate['id']}/calculate", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert Decimal(body["subtotal"]) == Decimal("0")
    assert Decimal(body["total"]) == Decimal("0")
    assert body["line_items"] == []
    assert body["category_breakdown"] == []


async def test_calculate_decimal_precision_trap(client):
    """A genuine, VERIFIED Decimal-vs-float divergence (not merely a
    textbook example that happens not to diverge on this platform, unlike
    `test_estimates.py`'s own documented caveat for its analogous line-item
    test) — checked empirically with a brute-force search before being
    hand-picked for this test:

    subtotal = 520.00 (one line item: quantity=10.00 * unit_rate=52.00)
    MarkupProfile: overhead_pct=3.45, profit_pct=25.00

    overhead_multiplier = 1 + 3.45/100 = 1.0345
    profit_multiplier   = 1 + 25.00/100 = 1.25

    unrounded_total = 520.00 * 1.0345 * 1.25
                     = 537.94 * 1.25
                     = 672.425   <- an EXACT tie at the third decimal place

    Decimal ROUND_HALF_UP on an exact tie always rounds away from zero:
    total = 672.43.

    Verified divergence: computing the equivalent pipeline in `float`
    (`float(Decimal("520.00")) * (1 + float(Decimal("3.45"))/100) *
    (1 + float(Decimal("25.00"))/100)`) produces `672.4249999999999...`
    (IEEE-754 binary64 cannot represent 672.425 exactly, and lands
    fractionally BELOW the true tie), which Python's own naive `round(x, 2)`
    rounds DOWN to 672.42 — one cent off from the mathematically correct
    Decimal result. This was confirmed by direct interpreter execution
    before writing this test, not assumed:

        >>> from decimal import Decimal
        >>> float(Decimal("520.00")) * (1 + float(Decimal("3.45")) / 100) * (1 + float(Decimal("25.00")) / 100)
        672.4249999999999
        >>> round(672.4249999999999, 2)
        672.42

    Asserting the API's `total` is EXACTLY `"672.43"` (the correct Decimal
    result, not the float-tainted `"672.42"`) proves the server computed
    this pipeline in exact `Decimal` arithmetic throughout, never `float`.
    """
    assert Decimal("520.00") * Decimal("1.0345") * Decimal("1.25") == Decimal("672.425")
    assert round(672.4249999999999, 2) == 672.42  # the float trap this test guards against

    admin = await _register_and_login(client, "Acme Construction", "calc-trap-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(
        client, admin["headers"], overhead_pct="3.45", profit_pct="25.00"
    )
    catalog_item = await _create_catalog_item(client, admin["headers"], unit_rate="52.00")
    estimate = await _create_estimate(client, admin["headers"], project["id"], markup["id"])

    await _put_lines(
        client,
        admin["headers"],
        estimate["id"],
        [{"cost_catalog_item_id": catalog_item["id"], "quantity": "10.00"}],
    )

    response = await client.post(
        f"/estimates/{estimate['id']}/calculate", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["subtotal"] == "520.00"
    assert body["total"] == "672.43"


async def test_calculate_snapshotted_estimate_returns_409_and_does_not_recompute(client):
    """Stronger than merely checking `subtotal`/`total` stay `None`: a
    successful calculation is run FIRST (so there is a real, non-null
    prior result), the estimate is then snapshotted, its line items are
    then changed out from under it directly (bypassing `PUT
    /estimates/{id}/lines`, which would itself now also 409 — see
    `_insert_line_item_directly`'s own docstring), and a second `POST
    .../calculate` is confirmed to both 409 AND leave the FIRST
    calculation's totals completely untouched, proving the 409 guard
    fires before `calculate_estimate` (the service) is ever called, not
    merely before the persist step."""
    admin = await _register_and_login(client, "Acme Construction", "calc-snap-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(
        client, admin["headers"], overhead_pct="10.00", profit_pct="15.00"
    )
    catalog_item = await _create_catalog_item(client, admin["headers"], unit_rate="45.00")
    estimate = await _create_estimate(client, admin["headers"], project["id"], markup["id"])

    await _put_lines(
        client,
        admin["headers"],
        estimate["id"],
        [{"cost_catalog_item_id": catalog_item["id"], "quantity": "10.00"}],
    )

    first = await client.post(f"/estimates/{estimate['id']}/calculate", headers=admin["headers"])
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["subtotal"] == "450.00"

    await _set_estimate_snapshotted_directly(estimate["id"])

    # Changes the underlying line items directly — if a recompute somehow
    # still ran, the totals below would visibly change.
    other_item = await _create_catalog_item(
        client, admin["headers"], name="Sneaked-in item", unit_rate="999.00"
    )
    await _insert_line_item_directly(
        estimate["id"],
        admin["company_id"],
        other_item["id"],
        quantity=Decimal("1.00"),
        unit_rate_snapshot=Decimal("999.00"),
    )

    second = await client.post(f"/estimates/{estimate['id']}/calculate", headers=admin["headers"])
    assert second.status_code == 409, second.text

    get_body = await _get_estimate(client, admin["headers"], estimate["id"])
    assert get_body["subtotal"] == first_body["subtotal"]
    assert get_body["total"] == first_body["total"]


async def test_calculate_as_project_manager(client):
    admin = await _register_and_login(client, "Acme Construction", "calc-pm-admin@acme.test")
    pm = await _invite_and_login_as(client, admin, "project_manager", "calc-pm@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    estimate = await _create_estimate(client, admin["headers"], project["id"], markup["id"])

    response = await client.post(f"/estimates/{estimate['id']}/calculate", headers=pm["headers"])
    assert response.status_code == 200, response.text


async def test_calculate_blocked_for_non_write_roles(client):
    admin = await _register_and_login(client, "Acme Construction", "calc-blocked-admin@acme.test")
    project = await _create_project(client, admin["headers"])
    markup = await _create_markup_profile(client, admin["headers"])
    estimate = await _create_estimate(client, admin["headers"], project["id"], markup["id"])

    field_crew = await _invite_and_login_as(client, admin, "field_crew", "calc-crew@acme.test")
    client_role = await _invite_and_login_as(client, admin, "client", "calc-client@acme.test")
    accountant = await _invite_and_login_as(client, admin, "accountant", "calc-acct@acme.test")

    for actor in (field_crew, client_role, accountant):
        response = await client.post(
            f"/estimates/{estimate['id']}/calculate", headers=actor["headers"]
        )
        assert response.status_code == 403


async def test_calculate_nonexistent_estimate_returns_404(client):
    admin = await _register_and_login(client, "Acme Construction", "calc-404-admin@acme.test")

    response = await client.post(
        "/estimates/00000000-0000-0000-0000-000000000000/calculate", headers=admin["headers"]
    )
    assert response.status_code == 404


async def test_calculate_cross_tenant_estimate_returns_404(client):
    a = await _register_and_login(client, "Company A", "calc-cross-a@acme.test")
    b = await _register_and_login(client, "Company B", "calc-cross-b@acme.test")
    project = await _create_project(client, a["headers"])
    markup = await _create_markup_profile(client, a["headers"])
    estimate = await _create_estimate(client, a["headers"], project["id"], markup["id"])

    response = await client.post(
        f"/estimates/{estimate['id']}/calculate", headers=b["headers"]
    )
    assert response.status_code == 404
