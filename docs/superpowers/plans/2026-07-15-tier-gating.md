# Tier-Based Module Gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce the pricing model's feature-gating table (docs/08 Section 3) at the API layer — mutating routes of Estimation/Compliance require Pro+, of Accounting/Integrations/child-branch creation require Enterprise — per `docs/superpowers/specs/2026-07-15-tier-gating-design.md`.

**Architecture:** One new core module (`app/core/tier_gating.py`) holding the tier table, a `require_module(module)` per-route dependency (same shape as `require_role`/`block_if_read_only`), and a `tier_allows()` helper for the two no-`CurrentUser` consumers (OAuth callback, event handlers). Retrofitted per-route onto exactly the mutating routes of the five gated modules; enforced complete by an introspection test.

**Tech Stack:** FastAPI dependencies, SQLAlchemy async, the existing `subscriptions` table + `get_root_company_id()` SQL function, pytest.

**Task numbering:** 5.1–5.10, continuing this repo's sequential feature numbering (Phase 4 ended at 4.17). This is NOT roadmap "Phase 5" (unscheduled open items) — it is a cross-cutting post-Phase-4 hardening feature.

**Read the spec first:** `docs/superpowers/specs/2026-07-15-tier-gating-design.md`. Its four locked decisions: trials stay Pro-tier; the full table is enforced; writes blocked / reads stay open; event-driven writes gated too.

---

## File structure

- Create: `backend/app/core/tier_gating.py` — tier table + `require_module` + `tier_allows`. Nothing else lives here.
- Create: `backend/tests/test_tier_gating.py` — core truth-table tests, per-module behavior tests, and the completeness introspection test (one file for the feature, matching `test_read_only_enforcement.py`'s precedent).
- Modify: `backend/tests/conftest.py` — add `set_subscription_tier` helper (importable, like `TEST_DATABASE_URL` already is).
- Modify: the 10 gated routers (exact route lists per task below).
- Modify: `backend/app/services/estimate_approved_handler.py`, `backend/app/services/financial_record_sync_handler.py` — event-path gating.
- Modify: existing test files that exercise Enterprise modules or create child companies from a fresh (trialing/pro) registration — enumerated per task.
- Modify: `scripts/e2e_smoke_test.py` — tier bumps for Companies G/H + one live 403 probe.

Docs were already updated in the same commit as the spec (`15a2c3f`) — no doc tasks here.

---

### Task 5.1: The gating core — `app/core/tier_gating.py`

**Files:**
- Create: `backend/app/core/tier_gating.py`
- Test: `backend/tests/test_tier_gating.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_tier_gating.py`:

```python
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
from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")


def test_every_module_maps_to_a_valid_tier():
    for module, tier in MODULE_MIN_TIER.items():
        assert tier in TIER_RANK, f"{module!r} maps to unknown tier {tier!r}"


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_tier_gating.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.tier_gating'`

- [ ] **Step 3: Write `backend/app/core/tier_gating.py`**

```python
"""Tier-based module gating (docs/superpowers/specs/2026-07-15-tier-gating-design.md,
transcribing docs/08-pricing-subscription-model.md Section 3's feature table).

require_module gates a route on the caller's ROOT company's subscription
TIER; it is orthogonal to require_role (who within the company may act) and
block_if_read_only (is the subscription in good standing) — all three
coexist as per-route dependencies on the same routes. It is attached ONLY
to mutating routes (spec Decision 3: writes blocked below tier, reads stay
open), and it deliberately does NO method sniffing: the Integrations
module's mutating routes (connect/callback) are GETs, so any
request.method-based pass-through would exempt exactly the routes that
most need gating. The writes-only semantics live in WHERE the dependency
is attached, enforced complete by test_tier_gating.py's introspection test.

tier_allows is the same one-query tier resolution for the two consumers
with no CurrentUser: the OAuth callback (which authenticates via a signed
state token) and the event handlers (which gate event-driven writes into
gated modules, spec Decision 4).

Missing subscription row fails OPEN in both — mirroring block_if_read_only's
documented "unreachable state (every root company gets a subscription
atomically at registration), not something to build defensive handling
for" stance.
"""
import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user
from app.models import Subscription

TIER_RANK = {"starter": 0, "pro": 1, "enterprise": 2}

MODULE_MIN_TIER = {
    "estimation": "pro",         # Estimation Engine incl. e-signature + change orders
    "compliance": "pro",         # Compliance Tracking
    "accounting": "enterprise",  # client invoicing (AR), bills (AP), expenses
    "integrations": "enterprise",  # QuickBooks/FreshBooks
    "child_branches": "enterprise",  # nested child-company creation
}


async def _root_tier(session: AsyncSession, company_id: uuid.UUID) -> str | None:
    # ONE query, root resolution inlined in the WHERE clause — the identical
    # pattern block_if_read_only and subscriptions.py's
    # _get_subscription_for_current already use, and which already works
    # under the subscriptions table's upward-visibility RLS policy from a
    # tenant-scoped session.
    result = await session.execute(
        select(Subscription.tier).where(
            Subscription.company_id == func.get_root_company_id(company_id)
        )
    )
    return result.scalar_one_or_none()


async def tier_allows(session: AsyncSession, company_id: uuid.UUID, module: str) -> bool:
    tier = await _root_tier(session, company_id)
    if tier is None:
        return True  # fail open — see module docstring
    return TIER_RANK[tier] >= TIER_RANK[MODULE_MIN_TIER[module]]


def require_module(module: str):
    # KeyError HERE (module-import time, when the router file is imported)
    # for a typo'd module name — not at request time.
    min_tier = MODULE_MIN_TIER[module]

    async def dependency(current: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        tier = await _root_tier(current.session, current.company_id)
        if tier is not None and TIER_RANK[tier] < TIER_RANK[min_tier]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"This feature requires the {min_tier} plan",
            )
        return current

    # Stamped for test_tier_gating.py's completeness introspection test —
    # lets it assert not just that SOME gate is present on a route, but that
    # it is the CORRECT module's gate.
    dependency.tier_module = module
    return dependency
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_tier_gating.py -v`
Expected: PASS, 19 passed (2 sync + 15 parametrized + 2 async)

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/tier_gating.py backend/tests/test_tier_gating.py
git commit -m "feat: add tier gating core - tier table, require_module, tier_allows"
```

---

### Task 5.2: `set_subscription_tier` test helper in conftest

**Files:**
- Modify: `backend/tests/conftest.py`

- [ ] **Step 1: Add the helper**

Add at the bottom of `backend/tests/conftest.py` (after the `_clean_tables` fixture):

```python
async def set_subscription_tier(company_id, tier):
    """Task 5.2 (tier-gating spec, Section 5): flips a registered company's
    subscription tier via the RLS-exempt owner connection. Registration can
    only ever produce trialing/pro (docs/08 Section 5), so any test that
    exercises an Enterprise-gated module (accounting, integrations,
    child-branch creation) — or a Starter-blocked scenario — sets the tier
    it needs explicitly with this. Same owner-connection test-setup
    rationale as _clean_tables above and the tenant-isolation files'
    _insert_*_directly helpers. Accepts company_id as str or UUID (asyncpg
    takes either for a uuid column)."""
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        await conn.execute(
            "UPDATE subscriptions SET tier = $1 WHERE company_id = $2",
            tier,
            company_id if not isinstance(company_id, str) else uuid.UUID(company_id),
        )
    finally:
        await conn.close()
```

Check the top of conftest.py: `import uuid` may not be present — add it to the imports if missing (`asyncpg` already is).

- [ ] **Step 2: Verify it imports**

Run: `cd backend && python -c "from tests.conftest import set_subscription_tier; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add backend/tests/conftest.py
git commit -m "test: add set_subscription_tier owner-connection helper"
```

---

### Task 5.3: Estimation retrofit (Pro+) — catalogs, estimates, change orders

**Files:**
- Modify: `backend/app/routers/catalogs.py`
- Modify: `backend/app/routers/estimates.py`
- Modify: `backend/app/routers/change_orders.py`
- Test: `backend/tests/test_tier_gating.py` (append)

- [ ] **Step 1: Write the failing behavior tests**

Append to `backend/tests/test_tier_gating.py`:

```python
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
    assert "pro" in response.json()["detail"]


async def test_starter_company_cannot_create_an_estimate_but_can_still_read(client):
    admin = await _register_and_login(client, "Tier Co S2", "tier-s2@example.test")
    await set_subscription_tier(admin["company_id"], "starter")

    # A valid-SHAPED body (the project doesn't need to exist — the tier
    # dependency 403s before the handler body ever queries), so the
    # assertion can't race FastAPI's own 422 body validation.
    create = await client.post(
        "/estimates", json={"project_id": str(uuid.uuid4())}, headers=admin["headers"]
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
```

Also add the import at the top of the file's import block: `from tests.conftest import TEST_DATABASE_URL, set_subscription_tier` (replacing the existing `TEST_DATABASE_URL`-only import).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_tier_gating.py -v -k "starter or pro_company"`
Expected: the two starter tests FAIL (403 expected, 201/200 returned — no gate exists yet); the pro test passes already.

- [ ] **Step 3: Retrofit the three routers**

In each file, add the import (alphabetically into the existing `app.core.*` import block):

```python
from app.core.tier_gating import require_module
```

Then add this exact parameter to each listed route handler's signature, directly after its existing `_ro: None = Depends(block_if_read_only),` line:

```python
    _tier: CurrentUser = Depends(require_module("estimation")),
```

Routes to gate (identify by decorator method+path — function names may differ; every one already has `block_if_read_only` from the Task 3.26 retrofit):

`catalogs.py` (3): `POST /catalogs/items`, `POST /catalogs/items/{parent_catalog_item_id}/override`, `POST /markup-profiles`.
`estimates.py` (7): `POST ""` (create), `PUT /{estimate_id}/lines`, `POST /{estimate_id}/calculate`, `POST /{estimate_id}/export`, `POST /{estimate_id}/send-for-signature`, `POST /{estimate_id}/approve`, `POST /{estimate_id}/reject`.
`change_orders.py` (4): `POST /projects/{project_id}/change-orders`, `POST /change-orders/{change_order_id}/send-for-signature`, `POST /change-orders/{change_order_id}/approve`, `POST /change-orders/{change_order_id}/reject`.

Do NOT gate any GET in these files, and do NOT touch `esignatures.py` (GET-only).

- [ ] **Step 4: Run tests to verify they pass, plus the existing estimation suites**

Run: `cd backend && python -m pytest tests/test_tier_gating.py tests/test_cost_catalog.py tests/test_cost_catalog_inheritance.py tests/test_markup_profiles.py tests/test_estimates.py tests/test_estimate_calculation.py tests/test_estimate_pdf_export.py tests/test_estimate_snapshotting.py tests/test_change_orders.py tests/test_esignatures.py -v`
Expected: ALL pass — trials are pro, estimation is pro+, so no existing estimation test needs a tier flip. If any existing test fails with a 403 mentioning "plan", something is over-gated — fix the gate, not the test.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/catalogs.py backend/app/routers/estimates.py backend/app/routers/change_orders.py backend/tests/test_tier_gating.py
git commit -m "feat: tier-gate estimation module mutating routes (pro+)"
```

---

### Task 5.4: Compliance retrofit (Pro+)

**Files:**
- Modify: `backend/app/routers/subcontractors.py`
- Modify: `backend/app/routers/compliance.py`
- Modify: `backend/app/routers/subcontractor_assignments.py`
- Test: `backend/tests/test_tier_gating.py` (append)

- [ ] **Step 1: Write the failing behavior tests**

Append to `backend/tests/test_tier_gating.py`:

```python
async def test_starter_company_cannot_create_a_subcontractor_but_can_read_the_dashboard(client):
    admin = await _register_and_login(client, "Tier Co S3", "tier-s3@example.test")
    await set_subscription_tier(admin["company_id"], "starter")

    create = await client.post(
        "/subcontractors",
        json={"name": "Ace Plumbing", "trade": "plumbing"},
        headers=admin["headers"],
    )
    assert create.status_code == 403

    dashboard = await client.get("/compliance/dashboard", headers=admin["headers"])
    assert dashboard.status_code == 200
```

Before finalizing the create payload, check `backend/app/schemas/subcontractor.py` for `SubcontractorCreateRequest`'s actual required fields and adjust minimally if `{"name", "trade"}` isn't valid — the test's intent (403 before validation even matters — dependencies run first) holds either way, but keep the payload valid so the companion at-tier variants elsewhere stay reusable.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_tier_gating.py -v -k subcontractor`
Expected: FAIL (403 expected on create, non-403 returned).

- [ ] **Step 3: Retrofit the three routers**

Same import + same parameter line as Task 5.3, with module `"compliance"`:

```python
    _tier: CurrentUser = Depends(require_module("compliance")),
```

Routes to gate (4):
`subcontractors.py` (2): `POST ""` (create), `POST /{subcontractor_id}/compliance-documents`.
`compliance.py` (1): `POST /notifications/{notification_id}/dismiss`.
`subcontractor_assignments.py` (1): `POST /projects/{project_id}/subcontractor-assignments`.

The dashboard and all list/detail GETs stay ungated.

- [ ] **Step 4: Run tests to verify they pass, plus the existing compliance suites**

Run: `cd backend && python -m pytest tests/test_tier_gating.py tests/test_subcontractors.py tests/test_compliance_documents.py tests/test_compliance_dashboard.py tests/test_compliance_notifications.py tests/test_subcontractor_assignments.py tests/test_tenant_isolation_phase3.py -v`
Expected: ALL pass (trials are pro; compliance is pro+). Adjust the exact test filenames to what exists in `backend/tests/` if these names drift — run `ls backend/tests | grep -i "compliance\|subcontractor"` first.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/subcontractors.py backend/app/routers/compliance.py backend/app/routers/subcontractor_assignments.py backend/tests/test_tier_gating.py
git commit -m "feat: tier-gate compliance module mutating routes (pro+)"
```

---

### Task 5.5: Accounting retrofit (Enterprise) — invoices, bills, expenses

**Files:**
- Modify: `backend/app/routers/invoices.py`
- Modify: `backend/app/routers/bills.py`
- Modify: `backend/app/routers/expenses.py`
- Modify: `backend/tests/test_invoices.py`, `backend/tests/test_bills.py`, `backend/tests/test_expenses.py`, `backend/tests/test_invoicing_ap_tenant_isolation.py` (and any other file the Step 4 run surfaces)
- Test: `backend/tests/test_tier_gating.py` (append)

- [ ] **Step 1: Write the failing behavior tests**

Append to `backend/tests/test_tier_gating.py`:

```python
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
    invoice = await client.post(
        f"/projects/{project.json()['id']}/invoices", json={"amount": "100.00"}, headers=admin["headers"]
    )
    assert invoice.status_code == 201, invoice.text

    # Downgrade back to pro: the created invoice must remain READABLE
    # (spec Decision 3 - the whole point of writes-only gating).
    await set_subscription_tier(admin["company_id"], "pro")
    detail = await client.get(f"/invoices/{invoice.json()['id']}", headers=admin["headers"])
    assert detail.status_code == 200
    report = await client.get("/reports/profitability", headers=admin["headers"])
    assert report.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_tier_gating.py -v -k "accounting or enterprise_company"`
Expected: FAIL — the pro company's creates return 201, not 403.

- [ ] **Step 3: Retrofit the three routers**

Same import + parameter line, module `"accounting"`:

```python
    _tier: CurrentUser = Depends(require_module("accounting")),
```

Routes to gate (8):
`invoices.py` (4): `POST /projects/{project_id}/invoices`, `POST /invoices/{invoice_id}/send`, `POST /invoices/{invoice_id}/payments`, `POST /invoices/{invoice_id}/void`.
`bills.py` (3): `POST ""` (create), `POST /{bill_id}/payments`, `POST /{bill_id}/void`.
`expenses.py` (1): `POST /projects/{project_id}/expenses`.

`reports.py` is GET-only — entirely untouched.

- [ ] **Step 4: Fix the existing accounting test files**

Every test in `test_invoices.py`, `test_bills.py`, `test_expenses.py`, and `test_invoicing_ap_tenant_isolation.py` registers a company (trialing/pro) and then mutates accounting records — each now needs the tier bump. The mechanical change, applied inside each file's own `_register_and_login` helper (ONE change per file, not per test):

```python
from tests.conftest import TEST_DATABASE_URL, set_subscription_tier  # extend the existing conftest import

async def _register_and_login(client, company_name, email):
    ...existing body unchanged...
    # Tier gating (Task 5.5): these suites exercise Enterprise-gated
    # accounting routes; registration can only produce trialing/pro.
    await set_subscription_tier(register.json()["company_id"], "enterprise")
    return {...existing return unchanged...}
```

Where a file registers SECOND companies through the same helper (tenant-isolation files) this is still correct — both sides get enterprise; the isolation being tested is tenant, not tier. Then run each file and chase any remaining 403s the same way (e.g. `test_profitability_report.py` if it creates invoices via HTTP; `test_estimate_approved_handler.py` is handled later in Task 5.8 — its deposit path is event-driven, not route-driven, and is NOT yet gated after this task).

Run: `cd backend && python -m pytest tests/test_tier_gating.py tests/test_invoices.py tests/test_bills.py tests/test_expenses.py tests/test_invoicing_ap_tenant_isolation.py tests/test_profitability_report.py tests/test_estimate_approved_handler.py tests/test_financial_record_sync_handler.py -v`
(Adjust `test_profitability_report.py` to the real filename — check `ls backend/tests | grep -i profit`.)
Expected: ALL pass. `test_estimate_approved_handler.py` and `test_financial_record_sync_handler.py` must still pass UNCHANGED at this point — their writes go through event handlers and the callback, none of which are gated yet.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/invoices.py backend/app/routers/bills.py backend/app/routers/expenses.py backend/tests/test_tier_gating.py backend/tests/test_invoices.py backend/tests/test_bills.py backend/tests/test_expenses.py backend/tests/test_invoicing_ap_tenant_isolation.py
git commit -m "feat: tier-gate accounting module mutating routes (enterprise)"
```

(Include any additional test files Step 4 surfaced.)

---

### Task 5.6: Integrations retrofit (Enterprise) — connect dependency + callback in-route check

**Files:**
- Modify: `backend/app/routers/integrations.py`
- Modify: `backend/tests/test_integrations.py`, `backend/tests/test_integrations_tenant_isolation.py`, `backend/tests/test_financial_record_sync_handler.py`, `backend/tests/test_estimate_approved_handler.py` (its Task-4.13-era sync test uses the callback)
- Test: `backend/tests/test_tier_gating.py` (append)

- [ ] **Step 1: Write the failing behavior tests**

Append to `backend/tests/test_tier_gating.py`:

```python
async def test_pro_company_cannot_start_or_complete_the_oauth_flow(client):
    from app.services.integration_oauth_state import sign_oauth_state

    admin = await _register_and_login(client, "Tier Co I1", "tier-i1@example.test")

    connect = await client.get("/integrations/quickbooks/connect", headers=admin["headers"])
    assert connect.status_code == 403

    # callback: a validly SIGNED state for a below-tier company must also be
    # rejected — this is the spec Section 3 "state minted while Enterprise,
    # redeemed after a downgrade" hole, closed in-route since callback has
    # no CurrentUser for the dependency to hang off.
    state = sign_oauth_state(company_id=admin["company_id"], provider="quickbooks")
    callback = await client.get(f"/integrations/quickbooks/callback?code=fake&state={state}")
    assert callback.status_code == 403


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_tier_gating.py -v -k oauth`
Expected: FAIL — connect returns 200, callback returns 200.

- [ ] **Step 3: Gate `connect` and `callback` in `backend/app/routers/integrations.py`**

Add imports: `from app.core.tier_gating import require_module, tier_allows`.

`connect` gets the normal dependency (it has a `CurrentUser`; being a GET is irrelevant — see tier_gating.py's own docstring on why no method sniffing):

```python
@router.get("/{provider}/connect", response_model=AuthorizationUrlResponse)
async def connect(
    provider: Provider,
    current: CurrentUser = Depends(require_role(*_ROLES)),
    _tier: CurrentUser = Depends(require_module("integrations")),
) -> AuthorizationUrlResponse:
```

`callback` gets the in-route check — inside the existing `async with session.begin():` block, immediately after the existing `await set_current_tenant(session, str(company_id))` line and before `client = ...`:

```python
            # Tier gating (spec Section 3): callback has no CurrentUser for
            # require_module to hang off — the signed state IS its auth — so
            # the check runs in-route. This also closes the one bypass the
            # dependency couldn't: a state minted while Enterprise, redeemed
            # within its 10-minute TTL after a downgrade.
            if not await tier_allows(session, company_id, "integrations"):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "This feature requires the enterprise plan",
                )
```

(`HTTPException`/`status` are already imported in this file. The exception propagates out of `async with session.begin():`, which rolls back — nothing has been written yet at that point anyway.)

- [ ] **Step 4: Fix the existing integration-flow test files**

Same one-change-per-file `_register_and_login` bump as Task 5.5 Step 4 (add `await set_subscription_tier(register.json()["company_id"], "enterprise")` before the return), in: `test_integrations.py`, `test_integrations_tenant_isolation.py`, `test_financial_record_sync_handler.py`, `test_estimate_approved_handler.py`. Note for `test_integrations_tenant_isolation.py`: its `_register_and_login` also returns `user_id` — keep that; and its child-branch flows get their enterprise tier from this same bump (the subscription belongs to the root).

Run: `cd backend && python -m pytest tests/test_tier_gating.py tests/test_integrations.py tests/test_integrations_tenant_isolation.py tests/test_financial_record_sync_handler.py tests/test_estimate_approved_handler.py tests/test_accounting_sync.py -v`
Expected: ALL pass (`test_accounting_sync.py` seeds via raw SQL, no HTTP — should pass unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/integrations.py backend/tests/test_tier_gating.py backend/tests/test_integrations.py backend/tests/test_integrations_tenant_isolation.py backend/tests/test_financial_record_sync_handler.py backend/tests/test_estimate_approved_handler.py
git commit -m "feat: tier-gate integrations connect/callback (enterprise)"
```

---

### Task 5.7: Child-branch creation gate (Enterprise)

**Files:**
- Modify: `backend/app/routers/companies.py`
- Modify: every test file that POSTs `/companies/{id}/children` from a fresh registration (enumerated in Step 4)
- Test: `backend/tests/test_tier_gating.py` (append)

- [ ] **Step 1: Write the failing behavior tests**

Append to `backend/tests/test_tier_gating.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_tier_gating.py -v -k child_branch`
Expected: FAIL — the pro company's create returns 201, not 403.

- [ ] **Step 3: Gate the route**

In `backend/app/routers/companies.py`, add the import and the parameter to exactly `POST /{company_id}/children`:

```python
    _tier: CurrentUser = Depends(require_module("child_branches")),
```

- [ ] **Step 4: Fix every existing test that creates children**

The grep `grep -ln "children" backend/tests/*.py` currently lists 15 files; verify each actually POSTs `/companies/{id}/children` (some may only mention the word). Confirmed creators as of this plan: `test_tenant_isolation.py`, `test_tenant_isolation_phase1.py`, `test_tenant_isolation_phase2.py`, `test_tenant_isolation_phase3.py`, `test_billing_tenant_isolation.py`, `test_invoicing_ap_tenant_isolation.py` (already bumped in 5.5), `test_integrations_tenant_isolation.py` (already bumped in 5.6), `test_integrations.py` (already bumped in 5.6), `test_audit_log.py`, `test_cost_catalog.py`, `test_cost_catalog_inheritance.py`, `test_estimates.py`, `test_lead_won_drafts_project.py`, `test_seat_usage_task.py`, `test_subcontractor_assignments.py`.

Apply the same one-change-per-file `_register_and_login` bump (`enterprise`) — but ONLY to files whose registering company goes on to create children. For files where only SOME tests create children and the file's helper is shared, bumping the shared helper to enterprise is still correct and simplest: tier is not what any of these files test, and enterprise passes every gate. Exception: do NOT touch `test_tier_gating.py` itself, `test_subscriptions.py` (asserts the trial's own pro tier), or `test_read_only_enforcement.py` (tests status, not tier, via ungated leads routes).

Run the full suite for this step — child creation is spread too widely for a curated list:
`cd backend && python -m pytest -q`
Expected: ALL pass. Chase every `403` containing "requires the enterprise plan" back to a file that needed the bump.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/companies.py backend/tests/test_tier_gating.py backend/tests/
git commit -m "feat: tier-gate child-branch creation (enterprise)"
```

---

### Task 5.8: Event-handler gating — deposit invoice + sync enqueue

**Files:**
- Modify: `backend/app/services/estimate_approved_handler.py`
- Modify: `backend/app/services/financial_record_sync_handler.py`
- Test: `backend/tests/test_tier_gating.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_tier_gating.py`:

```python
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
    client_role = await _invite_and_login_as(client, admin | {"user_id": None}, "client", "tier-e1-client@example.test")
    project = await _create_project(client, admin["headers"])
    markup_profile_id = await _create_markup_profile(client, admin["headers"])
    catalog_item_id = await _create_catalog_item(client, admin["headers"])

    estimate_id, _total = await _create_and_approve_estimate(
        client, admin["headers"], client_role["headers"],
        project["id"], markup_profile_id, catalog_item_id, quantity="8.00",
    )

    invoices = await _fetch_invoices_for_estimate(estimate_id)
    assert invoices == [], "a pro-tier approval must not auto-draft an (enterprise-module) invoice"


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
```

Note on `_invite_and_login_as(client, admin | {"user_id": None}, ...)`: check `test_estimate_approved_handler.py`'s helper signature first — it takes `(client, admin, role, email)` and only uses `admin["headers"]`, so passing this file's `admin` dict directly works; drop the `| {"user_id": None}` merge if so (it exists only in case that helper indexes `user_id`; verify by reading, then use the simplest form that's actually correct).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_tier_gating.py -v -k "deposit or leftover"`
Expected: FAIL — the pro approval DOES draft an invoice; the downgraded publish DOES enqueue.

- [ ] **Step 3: Gate the two handlers**

`backend/app/services/estimate_approved_handler.py` — add `from app.core.tier_gating import tier_allows` to imports, and after the existing `if project_id is None: return` early-return:

```python
    # Tier gating (spec Decision 4): the deposit invoice is an ACCOUNTING-
    # module write reached through the event bus, not that module's routes —
    # without this check a pro company would get invoices auto-drafted into
    # a module its plan doesn't include. Same silent no-op shape as the
    # project_id early-return above.
    if not await tier_allows(session, company_id, "accounting"):
        return
```

`backend/app/services/financial_record_sync_handler.py` — add the same import, and as the first statement of `handle_financial_record_created`'s body:

```python
    # Tier gating (spec Decision 4): a downgraded company's leftover
    # integration_connections rows must stop producing sync messages. The
    # Dramatiq actor does NOT re-check tier - enqueue-time is the gate;
    # re-checking per message in the worker buys a seconds-wide race window
    # at the cost of a query per sync.
    if not await tier_allows(session, company_id, "integrations"):
        return
```

- [ ] **Step 4: Run tests to verify they pass, plus the handler suites**

Run: `cd backend && python -m pytest tests/test_tier_gating.py tests/test_estimate_approved_handler.py tests/test_financial_record_sync_handler.py tests/test_accounting_sync.py tests/test_lead_won_drafts_project.py -v`
Expected: ALL pass — the existing handler suites' registrations were bumped to enterprise in Tasks 5.5/5.6, so their deposit/enqueue assertions still hold.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/estimate_approved_handler.py backend/app/services/financial_record_sync_handler.py backend/tests/test_tier_gating.py
git commit -m "feat: tier-gate event-driven writes (deposit invoice, sync enqueue)"
```

---

### Task 5.9: Completeness introspection test

**Files:**
- Test: `backend/tests/test_tier_gating.py` (append)

- [ ] **Step 1: Write the test**

Append to `backend/tests/test_tier_gating.py`:

```python
def test_every_gated_module_mutating_route_has_the_correct_tier_gate():
    """Walks the LIVE app's route table (same technique as
    test_read_only_enforcement.py's completeness test, same coverage
    caveats — see that test's docstring). For every mutating route in a
    gated router, asserts a dependency stamped with the CORRECT module is
    present — catching both a missed route and a copy-pasted wrong module.

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
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        path = route.path
        expected = module_for(path)

        if path == "/integrations/{provider}/connect":
            connect_checked = True
            if "integrations" not in gate_modules(route):
                problems.append(f"GET {path} must carry the integrations gate")
            continue

        if methods.isdisjoint({"POST", "PUT", "PATCH", "DELETE"}):
            continue
        if path in excluded or expected is None:
            continue

        found = gate_modules(route)
        if expected not in found:
            problems.append(f"{sorted(methods)} {path} missing require_module({expected!r}) (found: {found or 'none'})")

    assert connect_checked, "connect route not found — did its path change?"
    assert problems == [], f"Tier-gating gaps: {problems}"
```

- [ ] **Step 2: Run the test — it must pass immediately**

Run: `cd backend && python -m pytest tests/test_tier_gating.py -v -k correct_tier_gate`
Expected: PASS. If it fails, a retrofit task missed a route — fix the ROUTE (add the gate), not the test. Then sanity-check the checker itself by temporarily removing one gate parameter from any route, re-running (must FAIL naming that route), and restoring it.

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_tier_gating.py
git commit -m "test: add tier-gating completeness introspection test"
```

---

### Task 5.10: E2E extension + full regression + closeout + PR

**Files:**
- Modify: `scripts/e2e_smoke_test.py`
- Modify: `docs/superpowers/specs/2026-07-15-tier-gating-design.md` (Implementation Status note)

- [ ] **Step 1: Update the E2E script**

Companies G (Invoicing block) and H (Integrations block) register as trialing/pro and now need enterprise. Per the script's no-backend-imports convention (the `_set_subscription_status` precedent), add one helper next to it:

```python
def _set_subscription_tier(company_id: str, tier: str) -> None:
    """Tier gating (Task 5.10): registration can only produce trialing/pro;
    the Invoicing (Company G) and Integrations (Company H) blocks exercise
    Enterprise-gated modules. Same owner-DSN raw-SQL approach as
    _set_subscription_status above."""
    import asyncio

    async def _run() -> None:
        conn = await asyncpg.connect(OWNER_DATABASE_DSN)
        try:
            await conn.execute(
                "UPDATE subscriptions SET tier = $1 WHERE company_id = $2",
                tier,
                uuid.UUID(company_id),
            )
        finally:
            await conn.close()

    asyncio.run(_run())
```

Match `_set_subscription_status`'s ACTUAL sync/async shape — read it first and mirror it exactly (if it's already called from async context, drop the `asyncio.run` wrapper and make this a plain `async def`).

Then, in the Company G block: immediately after registration/login, call `_set_subscription_tier(company_g_company_id, "enterprise")` (using that block's actual variable name), with a `checks_passed.append("Company G bumped to enterprise tier (tier gating, Task 5.10)")`. Same for Company H. Additionally, in the Company H block BEFORE the bump, add one live 403 probe (real-HTTP proof the gate exists in the running stack):

```python
    blocked_connect = client.get(f"{BASE_URL}/integrations/quickbooks/connect", headers=company_h_headers)
    assert blocked_connect.status_code == 403, (
        f"pro-tier company should be tier-blocked from integrations connect, got {blocked_connect.status_code}: {blocked_connect.text}"
    )
    checks_passed.append("PASS: pro-tier company gets 403 from integrations connect (tier gating)")
```

(Adapt variable names and the client-call style to the block's existing code — read it first.)

Also add a Task 5.10 note to the module docstring, matching the existing per-task notes' style, stating the two tier bumps and the one 403 probe.

- [ ] **Step 2: Verify against the live stack**

The established procedure, with the documented Docker discipline (check `pg_isready` explicitly; watch for the known frontend port issue, which is out of scope):

```bash
cd <worktree root>
docker compose up -d --build
docker exec <postgres-container> pg_isready -U postgres
# apply migrations to the stack's DB if the volume is fresh (check alembic current)
python scripts/e2e_smoke_test.py
```

Expected: all pre-existing checks plus the new tier ones pass; the known frontend health-check failure at the very end is a pre-existing environmental issue (Windows excluded port range) — report, don't fix.

- [ ] **Step 3: Full regression, twice solo, plus the RLS suite**

```bash
cd backend && python -m pytest        # run 1
cd backend && python -m pytest        # run 2
cd backend && python -m pytest -v tests/test_tenant_isolation.py tests/test_rls_policy_regression.py tests/test_tenant_isolation_phase1.py tests/test_tenant_isolation_phase2.py tests/test_tenant_isolation_phase3.py tests/test_billing_tenant_isolation.py tests/test_invoicing_ap_tenant_isolation.py tests/test_integrations_tenant_isolation.py
```

Expected: all pass, both full runs and the RLS suite.

- [ ] **Step 4: Closeout note + commit**

Add an "Implementation Status" note at the top of `docs/superpowers/specs/2026-07-15-tier-gating-design.md` (right after the title, before "**Date:**"), matching the convention the Billing/Invoicing/Integrations specs use: completion, both regression pass counts/timings, the RLS suite count, the E2E additions, and anything deliberately not fixed.

```bash
git add docs/superpowers/specs/2026-07-15-tier-gating-design.md scripts/e2e_smoke_test.py
git commit -m "docs: close out tier-based module gating implementation"
```

- [ ] **Step 5: Push and open a PR against `main`**

```bash
git push -u origin feature/tier-gating
gh pr create --base main --head feature/tier-gating --title "feat: tier-based module gating - enforce the pricing table at the API layer" --body "..."
```

Confirm CI goes green. Merging remains an explicit, separate user decision — not automatic.
