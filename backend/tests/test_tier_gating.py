"""Tier-based module gating (docs/superpowers/specs/2026-07-15-tier-gating-design.md).

This file holds all three layers for the feature, matching
test_read_only_enforcement.py's one-file precedent: core truth-table tests
(this task), per-module behavior tests (Tasks 5.3-5.8), and the
completeness introspection test (Task 5.9).
"""
import uuid

import asyncpg
import pytest

from app.core.tier_gating import MODULE_MIN_TIER, TIER_RANK, tier_allows
from tests.conftest import TEST_DATABASE_URL, set_subscription_tier

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


def test_every_module_maps_to_a_valid_tier():
    for module, tier in MODULE_MIN_TIER.items():
        assert tier in TIER_RANK, f"{module!r} maps to unknown tier {tier!r}"

    # Drift guard (Task 5.1 code-quality review): TIER_RANK re-declares the
    # tier set that subscription.py's VALID_TIERS/DB CHECK already own. If a
    # fourth tier is ever added there without updating TIER_RANK, every
    # gated request from that tenant would 500 on TIER_RANK[tier] — this
    # assertion makes the divergence a loud test failure instead.
    from app.models.subscription import VALID_TIERS

    assert set(TIER_RANK) == set(VALID_TIERS)


def test_tier_rank_orders_the_three_tiers():
    assert TIER_RANK["starter"] < TIER_RANK["pro"] < TIER_RANK["enterprise"]


async def _seed_company_with_tier(tier):
    """Seeds a root company + subscription row directly via the owner
    connection — tier_allows is a plain session-level helper, so its truth
    table doesn't need the HTTP registration flow (and registration can only
    ever produce trialing/pro anyway)."""
    company_id = uuid.uuid4()
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO companies (id, parent_id, name) VALUES ($1, NULL, 'Tier Co')",
            company_id,
        )
        await conn.execute(
            "INSERT INTO subscriptions (id, company_id, stripe_customer_id, "
            "stripe_subscription_id, tier, status, included_seats) "
            "VALUES ($1, $2, $3, $4, $5, 'active', 5)",
            uuid.uuid4(),
            company_id,
            f"cus_fake_{uuid.uuid4().hex[:12]}",
            f"sub_fake_{uuid.uuid4().hex[:12]}",
            tier,
        )
    finally:
        await conn.close()
    return company_id


@pytest.mark.parametrize(
    "tier,module,expected",
    [
        # starter: universal modules aren't in MODULE_MIN_TIER at all, so the
        # grid below covers every gated module at every tier - 3x5 = 15 rows.
        ("starter", "estimation", False),
        ("starter", "compliance", False),
        ("starter", "accounting", False),
        ("starter", "integrations", False),
        ("starter", "child_branches", False),
        ("pro", "estimation", True),
        ("pro", "compliance", True),
        ("pro", "accounting", False),
        ("pro", "integrations", False),
        ("pro", "child_branches", False),
        ("enterprise", "estimation", True),
        ("enterprise", "compliance", True),
        ("enterprise", "accounting", True),
        ("enterprise", "integrations", True),
        ("enterprise", "child_branches", True),
    ],
)
async def test_tier_allows_truth_table(db_session, tier, module, expected):
    company_id = await _seed_company_with_tier(tier)
    assert await tier_allows(db_session, company_id, module) is expected


async def test_tier_allows_fails_open_on_missing_subscription(db_session):
    """Mirrors block_if_read_only's documented stance: no subscription row is
    an unreachable state (every root company gets one atomically at
    registration), treated as allow, not something to build defensive
    handling for."""
    company_id = uuid.uuid4()
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO companies (id, parent_id, name) VALUES ($1, NULL, 'No Sub Co')",
            company_id,
        )
    finally:
        await conn.close()
    assert await tier_allows(db_session, company_id, "accounting") is True


async def test_tier_allows_resolves_the_root_company_for_a_child_branch(db_session):
    """A child branch has no subscriptions row of its own (root-only
    ownership) — tier resolution must walk up via get_root_company_id, the
    same way block_if_read_only does."""
    root_id = await _seed_company_with_tier("enterprise")
    child_id = uuid.uuid4()
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "INSERT INTO companies (id, parent_id, name) VALUES ($1, $2, 'Tier Branch')",
            child_id,
            root_id,
        )
    finally:
        await conn.close()
    assert await tier_allows(db_session, child_id, "accounting") is True


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
    return {
        "company_id": register.json()["company_id"],
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }


async def test_starter_company_cannot_create_a_catalog_item(client):
    admin = await _register_and_login(client, "Tier Co S1", "tier-s1@example.test")
    await set_subscription_tier(admin["company_id"], "starter")

    response = await client.post(
        "/catalogs/items",
        json={"category": "materials", "name": "Lumber", "unit": "board_ft", "unit_rate": "5.00"},
        headers=admin["headers"],
    )
    assert response.status_code == 403
    assert "requires the pro plan" in response.json()["detail"]  # not bare "pro" -
    # the role-403 message contains "project_manager", which a bare
    # substring check would falsely match (Task 5.3 code-quality review)


async def test_starter_company_cannot_create_a_subcontractor_but_can_read_the_dashboard(client):
    admin = await _register_and_login(client, "Tier Co S3", "tier-s3@example.test")
    await set_subscription_tier(admin["company_id"], "starter")

    create = await client.post(
        "/subcontractors",
        json={"name": "Ace Plumbing", "trade": "plumbing"},
        headers=admin["headers"],
    )
    assert create.status_code == 403
    assert "requires the pro plan" in create.json()["detail"]  # not bare "pro" -
    # the role-403 message contains "project_manager", which a bare
    # substring check would falsely match (Task 5.3 code-quality review)

    # The dashboard (and every compliance GET) stays open below tier
    # (spec Decision 3).
    dashboard = await client.get("/compliance/dashboard", headers=admin["headers"])
    assert dashboard.status_code == 200


async def test_starter_company_cannot_create_an_estimate_but_can_still_read(client):
    admin = await _register_and_login(client, "Tier Co S2", "tier-s2@example.test")
    await set_subscription_tier(admin["company_id"], "starter")

    # A valid-SHAPED body (the project/markup profile don't need to exist —
    # the tier dependency 403s before the handler body ever queries), so the
    # assertion can't race FastAPI's own 422 body validation.
    # markup_profile_id is required by EstimateCreateRequest's schema, so a
    # random UUID is supplied to keep the body valid-shaped.
    create = await client.post(
        "/estimates",
        json={"project_id": str(uuid.uuid4()), "markup_profile_id": str(uuid.uuid4())},
        headers=admin["headers"],
    )
    assert create.status_code == 403

    # Reads stay open below tier (spec Decision 3).
    listing = await client.get("/estimates", headers=admin["headers"])
    assert listing.status_code == 200


async def test_pro_company_can_create_a_catalog_item(client):
    """The trial default IS pro — no tier flip needed; this pins the
    at-tier pass so a future gating regression can't silently over-block."""
    admin = await _register_and_login(client, "Tier Co P1", "tier-p1@example.test")

    response = await client.post(
        "/catalogs/items",
        json={"category": "materials", "name": "Lumber", "unit": "board_ft", "unit_rate": "5.00"},
        headers=admin["headers"],
    )
    assert response.status_code == 201, response.text


async def test_gate_fires_for_a_child_branch_acting_session_via_rls_root_resolution(client):
    """Task 5.1 code-quality review carry-forward: the gate's tier lookup
    runs on the tenant-scoped app_user session under the subscriptions
    table's upward-visibility RLS policy — a child branch has no
    subscriptions row of its own, so this only 403s if the root-resolution
    genuinely works through RLS from a child tenant context. fail-open
    means a broken policy would silently allow; this test makes that
    breakage loud."""
    import asyncpg as _asyncpg

    admin = await _register_and_login(client, "Tier Co RLS1", "tier-rls1@example.test")
    # Create the child while the parent is still pro (child creation isn't
    # gated until Task 5.7), grant the admin a membership in it directly,
    # then downgrade the ROOT to starter.
    create = await client.post(
        f"/companies/{admin['company_id']}/children", json={"name": "RLS Branch"}, headers=admin["headers"]
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]

    conn = await _asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        # user_id: registration response carries it; fetch via the users
        # table to keep this helper-independent.
        user_row = await conn.fetchrow(
            "SELECT user_id FROM company_users WHERE company_id = $1", uuid.UUID(admin["company_id"])
        )
        await conn.execute(
            "INSERT INTO company_users (company_id, user_id, role, created_at) VALUES ($1, $2, 'admin', now())",
            uuid.UUID(child_id),
            user_row["user_id"],
        )
    finally:
        await conn.close()

    await set_subscription_tier(admin["company_id"], "starter")

    child_headers = {**admin["headers"], "X-Tenant-ID": child_id}
    response = await client.post(
        "/catalogs/items",
        json={"category": "materials", "name": "Lumber", "unit": "board_ft", "unit_rate": "5.00"},
        headers=child_headers,
    )
    assert response.status_code == 403
    assert "requires the pro plan" in response.json()["detail"]  # not bare "pro" -
    # the role-403 message contains "project_manager", which a bare
    # substring check would falsely match (Task 5.3 code-quality review)


async def test_pro_company_cannot_create_accounting_records(client):
    """The trial default (pro) is exactly the below-tier case for
    accounting — no tier flip needed for the negative direction."""
    admin = await _register_and_login(client, "Tier Co A1", "tier-a1@example.test")
    project = await client.post(
        "/projects", json={"name": "Tier Project", "site_address": "1 Main St"}, headers=admin["headers"]
    )
    assert project.status_code == 201, project.text

    invoice = await client.post(
        f"/projects/{project.json()['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    assert invoice.status_code == 403
    assert "enterprise" in invoice.json()["detail"]

    bill = await client.post(
        "/bills", json={"vendor_name": "Ace Plumbing", "amount": "300.00"}, headers=admin["headers"]
    )
    assert bill.status_code == 403
    assert "enterprise" in bill.json()["detail"]

    expense = await client.post(
        f"/projects/{project.json()['id']}/expenses",
        json={"description": "Materials", "amount": "50.00", "incurred_on": "2026-08-01"},
        headers=admin["headers"],
    )
    assert expense.status_code == 403


async def test_enterprise_company_can_create_an_invoice_and_pro_can_still_read(client):
    admin = await _register_and_login(client, "Tier Co A2", "tier-a2@example.test")
    await set_subscription_tier(admin["company_id"], "enterprise")
    project = await client.post(
        "/projects", json={"name": "Tier Project 2", "site_address": "1 Main St"}, headers=admin["headers"]
    )
    assert project.status_code == 201, project.text
    invoice = await client.post(
        f"/projects/{project.json()['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    assert invoice.status_code == 201, invoice.text

    # Downgrade back to pro: the created invoice must remain READABLE
    # (spec Decision 3 - the whole point of writes-only gating).
    await set_subscription_tier(admin["company_id"], "pro")
    detail = await client.get(f"/invoices/{invoice.json()['id']}", headers=admin["headers"])
    assert detail.status_code == 200
    report = await client.get(
        "/reports/profitability?start_date=2026-01-01&end_date=2026-12-31", headers=admin["headers"]
    )
    assert report.status_code == 200


async def test_pro_company_cannot_start_or_complete_the_oauth_flow(client):
    from app.services.integration_oauth_state import sign_oauth_state

    admin = await _register_and_login(client, "Tier Co I1", "tier-i1@example.test")

    connect = await client.get("/integrations/quickbooks/connect", headers=admin["headers"])
    assert connect.status_code == 403
    assert "enterprise" in connect.json()["detail"]

    # callback: a validly SIGNED state for a below-tier company must also be
    # rejected — this is the spec Section 3 "state minted while Enterprise,
    # redeemed after a downgrade" hole, closed in-route since callback has
    # no CurrentUser for the dependency to hang off.
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    callback = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert callback.status_code == 403
    assert "enterprise" in callback.json()["detail"]


async def test_pro_company_can_still_read_sync_status_for_an_existing_connection(client):
    from app.services.integration_oauth_state import sign_oauth_state

    admin = await _register_and_login(client, "Tier Co I2", "tier-i2@example.test")
    await set_subscription_tier(admin["company_id"], "enterprise")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    connected = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert connected.status_code == 200, connected.text

    # Downgrade: the read route stays open (spec Decision 3).
    await set_subscription_tier(admin["company_id"], "pro")
    sync_status = await client.get("/integrations/quickbooks/sync-status", headers=admin["headers"])
    assert sync_status.status_code == 200
