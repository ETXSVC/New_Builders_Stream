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
    # Bump to enterprise first so the (Task 5.7) child_branches gate lets the
    # child be created, grant the admin a membership in it directly, then
    # downgrade the ROOT to starter — the gate-under-test still fires from
    # the child's acting session against the root's (now starter) tier.
    await set_subscription_tier(admin["company_id"], "enterprise")
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


async def test_existing_child_branches_keep_operating_after_a_downgrade_to_pro(client):
    """Task 5.7 code-quality review: the allow-direction companion to the
    starter RLS test above. Pro-root-with-existing-children is a legitimate
    post-downgrade production state (child CREATION is enterprise-gated,
    but writes-only gating means existing branches must keep working at
    whatever their root's tier still includes) — and after Task 5.7's
    blanket enterprise bumps, no other test in the suite exercises a
    sub-enterprise root with children on a SUCCESS path. Without this, an
    over-blocking regression (e.g. 403ing all child-tenant activity when
    root < enterprise, a plausible misreading of "child branches are
    enterprise-gated") would be invisible: the enterprise suites pass and
    the starter test above still sees its 403."""
    import asyncpg as _asyncpg

    admin = await _register_and_login(client, "Tier Co RLS2", "tier-rls2@example.test")
    await set_subscription_tier(admin["company_id"], "enterprise")
    create = await client.post(
        f"/companies/{admin['company_id']}/children", json={"name": "Surviving Branch"}, headers=admin["headers"]
    )
    assert create.status_code == 201, create.text
    child_id = create.json()["id"]

    conn = await _asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
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

    await set_subscription_tier(admin["company_id"], "pro")

    child_headers = {**admin["headers"], "X-Tenant-ID": child_id}
    response = await client.post(
        "/catalogs/items",
        json={"category": "materials", "name": "Lumber", "unit": "board_ft", "unit_rate": "5.00"},
        headers=child_headers,
    )
    assert response.status_code == 201, response.text


async def test_pro_company_cannot_create_a_child_branch_but_enterprise_can(client):
    admin = await _register_and_login(client, "Tier Co C1", "tier-c1@example.test")

    blocked = await client.post(
        f"/companies/{admin['company_id']}/children", json={"name": "Blocked Branch"}, headers=admin["headers"]
    )
    assert blocked.status_code == 403
    assert "enterprise" in blocked.json()["detail"]

    await set_subscription_tier(admin["company_id"], "enterprise")
    allowed = await client.post(
        f"/companies/{admin['company_id']}/children", json={"name": "Allowed Branch"}, headers=admin["headers"]
    )
    assert allowed.status_code == 201, allowed.text


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


async def test_pro_company_estimate_approval_drafts_no_deposit_invoice(client):
    """Spec Decision 4: event-driven writes into gated modules are gated
    too. A pro company approving an estimate gets the approval (estimation
    is in its tier) but NO auto-drafted invoice (accounting is not) and NO
    invoice.auto_generated audit row. Uses the full real approval flow -
    the same helpers test_estimate_approved_handler.py uses, imported from
    there rather than duplicated."""
    from app.core.event_handlers import register_event_handlers
    from tests.test_estimate_approved_handler import (
        _create_and_approve_estimate,
        _create_catalog_item,
        _create_markup_profile,
        _create_project,
        _fetch_invoices_for_estimate,
        _invite_and_login_as,
    )

    register_event_handlers()
    admin = await _register_and_login(client, "Tier Co E1", "tier-e1@example.test")
    # Deliberately NOT bumped to enterprise: pro is the case under test.
    # (test_estimate_approved_handler's own helper bumps ITS callers to
    # enterprise as of Task 5.6 - which is why this test registers through
    # THIS file's helper instead.)
    client_role = await _invite_and_login_as(client, admin, "client", "tier-e1-client@example.test")
    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    estimate_id, _total = await _create_and_approve_estimate(
        client, admin["headers"], client_role["headers"],
        project["id"], markup_profile_id, catalog_item_id, quantity="8.00",
    )

    invoices = await _fetch_invoices_for_estimate(estimate_id)
    assert invoices == [], "a pro-tier approval must not auto-draft an (enterprise-module) invoice"

    # And no invoice.auto_generated audit row either — asserted in code, not
    # just claimed in the docstring (Task 5.8 code-quality review): the skip
    # must leave the audit log clean, not record a phantom drafting.
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        audit_rows = await conn.fetch(
            "SELECT id FROM audit_log WHERE company_id = $1 AND action = 'invoice.auto_generated'",
            uuid.UUID(admin["company_id"]),
        )
    finally:
        await conn.close()
    assert audit_rows == []


async def test_below_tier_company_with_a_leftover_connection_enqueues_no_sync(client, monkeypatch, db_session):
    from app.core.event_handlers import register_event_handlers
    from app.core.events import publish
    from app.services.integration_oauth_state import sign_oauth_state
    from app.tasks.accounting_sync import sync_financial_record

    register_event_handlers()
    admin = await _register_and_login(client, "Tier Co E2", "tier-e2@example.test")
    await set_subscription_tier(admin["company_id"], "enterprise")
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    connected = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert connected.status_code == 200, connected.text

    # Downgrade AFTER connecting - the leftover connection row survives, but
    # must stop producing sync messages (spec Decision 4).
    await set_subscription_tier(admin["company_id"], "pro")

    calls = []
    monkeypatch.setattr(sync_financial_record, "send", lambda *a, **kw: calls.append((a, kw)))

    await publish(
        "INVOICE_CREATED",
        session=db_session,
        entity_type="invoice",
        entity_id=uuid.uuid4(),
        company_id=uuid.UUID(admin["company_id"]),
    )
    assert calls == []


def test_every_gated_module_mutating_route_has_the_correct_tier_gate():
    """Walks the LIVE app's route table (same technique as
    test_read_only_enforcement.py's completeness test, and it inherits
    that test's caveats — sub-app mounts and the one-level-deep dependency
    walk; see that docstring). For every mutating route in a gated router,
    asserts a dependency stamped with the CORRECT module is present —
    catching both a missed route and a copy-pasted wrong module.

    Coverage caveat OF THIS TEST'S OWN (Task 5.9 code-quality review),
    with the OPPOSITE failure direction from the inherited ones: the
    read-only completeness test enforces a universal rule, so a new route
    is checked by default and drift fails LOUD. This test cannot be
    universal (only some modules are gated), so module_for() below is a
    second, hand-maintained encoding of module ownership — and a path it
    doesn't classify is silently SKIPPED, not flagged. Maintenance
    contract: adding a NEW ROUTER (or renaming routes) under a gated
    module requires extending module_for, and nothing structural can
    catch that case. What IS structurally caught (the seen_modules
    assertion at the bottom, generalizing the connect_checked pattern): a
    new module added to MODULE_MIN_TIER without a module_for clause, and
    any rename/removal that orphans a module's ENTIRE route set — most
    importantly child_branches, whose single route is matched by exact
    string.

    Deliberate exclusions:
    - /integrations/{provider}/callback — gated IN-ROUTE via tier_allows
      (no CurrentUser for a dependency; see the route's own comment), and
      covered by test_pro_company_cannot_start_or_complete_the_oauth_flow.
    - /integrations/{provider}/connect is a GET but MUST carry the gate —
      asserted explicitly below, since the mutating-methods walk skips it.
    """
    from app.main import app

    # router module (by route path prefix ownership) -> tier module
    def module_for(path: str) -> str | None:
        if path.startswith("/catalogs") or path.startswith("/markup-profiles") or path.startswith("/estimates"):
            return "estimation"
        if "/change-orders" in path:
            return "estimation"
        if path.startswith("/subcontractors") or path.startswith("/compliance") or "/subcontractor-assignments" in path:
            return "compliance"
        if "/invoices" in path or path.startswith("/bills") or "/expenses" in path:
            return "accounting"
        if path.startswith("/integrations"):
            return "integrations"
        if path == "/companies/{company_id}/children":
            return "child_branches"
        return None

    excluded = {"/integrations/{provider}/callback"}

    def gate_modules(route) -> set:
        return {
            getattr(dep.call, "tier_module", None)
            for dep in route.dependant.dependencies
        } - {None}

    problems = []
    connect_checked = False
    seen_modules = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        path = route.path
        expected = module_for(path)

        if path == "/integrations/{provider}/connect":
            connect_checked = True
            seen_modules.add("integrations")
            if "integrations" not in gate_modules(route):
                problems.append(f"GET {path} must carry the integrations gate")
            continue

        if methods.isdisjoint({"POST", "PUT", "PATCH", "DELETE"}):
            continue
        if path in excluded or expected is None:
            continue
        seen_modules.add(expected)

        found = gate_modules(route)
        if expected not in found:
            problems.append(f"{sorted(methods)} {path} missing require_module({expected!r}) (found: {found or 'none'})")

    assert connect_checked, "connect route not found — did its path change?"
    assert seen_modules >= set(MODULE_MIN_TIER), (
        f"gated modules with ZERO classified mutating routes — module_for() has "
        f"drifted from the route table (new module without a mapping clause, or a "
        f"rename orphaned a module's whole prefix?): {set(MODULE_MIN_TIER) - seen_modules}"
    )
    assert problems == [], f"Tier-gating gaps: {problems}"


def test_tier_gating_classified_route_count_per_module_is_pinned():
    """Closes the specific blind spot the test above's own docstring
    documents ("PARTIAL drift is invisible... only WHOLE-module orphaning
    is structurally caught"): a single existing route silently falling out
    of module_for()'s hand-maintained path-prefix matching (e.g. an
    estimates route renamed to a path outside `/estimates`) would still
    leave every OTHER route in that module classified — `seen_modules >=
    set(MODULE_MIN_TIER)` above stays satisfied, and `problems` stays
    empty (a route module_for() doesn't recognize is silently SKIPPED, not
    flagged as missing its gate), so that test passes even though a real
    route quietly lost its tier-gating coverage entirely.

    Pinning the exact per-module COUNT of classified mutating routes
    closes that gap: if a route's path changes in a way that stops
    matching its module's prefix rule, that module's count drops by one
    and this test fails loudly — a repeat of the same
    recompute-and-diff-the-classification-inputs technique the test above
    already uses for `seen_modules`, just applied at route-count
    granularity instead of module-presence granularity. A genuine,
    intentional route addition/removal is expected to require updating
    this literal alongside it, the same maintenance cost
    `test_every_gated_module_mutating_route_has_the_correct_tier_gate`'s
    own docstring already asks of `module_for()` itself.

    `integrations` is deliberately absent from this count: its only
    mutating-methods-loop-visible route is `/integrations/{provider}/
    connect` (a GET, special-cased and separately asserted via
    `connect_checked` in the test above), so it never contributes to this
    loop's Counter at all — asserting `0` for it here would be checking a
    tautology, not a real invariant.
    """
    from app.main import app

    def module_for(path: str) -> str | None:
        if path.startswith("/catalogs") or path.startswith("/markup-profiles") or path.startswith("/estimates"):
            return "estimation"
        if "/change-orders" in path:
            return "estimation"
        if path.startswith("/subcontractors") or path.startswith("/compliance") or "/subcontractor-assignments" in path:
            return "compliance"
        if "/invoices" in path or path.startswith("/bills") or "/expenses" in path:
            return "accounting"
        if path.startswith("/integrations"):
            return "integrations"
        if path == "/companies/{company_id}/children":
            return "child_branches"
        return None

    excluded = {"/integrations/{provider}/callback", "/integrations/{provider}/connect"}

    counts: dict[str, int] = {}
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods or methods.isdisjoint({"POST", "PUT", "PATCH", "DELETE"}):
            continue
        path = route.path
        if path in excluded:
            continue
        expected = module_for(path)
        if expected is None:
            continue
        counts[expected] = counts.get(expected, 0) + 1

    assert counts == {
        "child_branches": 1,
        "estimation": 21,
        "compliance": 4,
        "accounting": 8,
    }, (
        f"classified mutating-route count per tier-gated module changed: {counts!r}. "
        "If this is a genuine, intentional route addition/removal, update this "
        "literal. If it's NOT — a route's path changed and silently fell out of "
        "module_for()'s classification — that route just lost its tier-gating "
        "test coverage entirely; fix module_for() instead."
    )
