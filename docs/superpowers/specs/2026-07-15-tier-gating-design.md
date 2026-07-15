# Tier-Based Module Gating — Design Spec

**Date:** 2026-07-15
**Depends on:** Billing (the `subscriptions` table, `get_root_company_id()`, `block_if_read_only` — merged to `main` via PR #9), plus every module being gated: Estimation/E-Signature (Phase 2), Compliance Tracking, Invoicing/AR-AP, Integrations core (PR #11) — all merged to `main`.
**Scope source:** [`docs/08-pricing-subscription-model.md`](../../08-pricing-subscription-model.md) Sections 3, 5, 6; [`docs/07-security-compliance.md`](../../07-security-compliance.md) Section 2; [`docs/09-roadmap-implementation-plan.md`](../../09-roadmap-implementation-plan.md) Phase 4 exit criterion ("**Enterprise-tier** companies can connect").

## Why now

The pricing model's Section 3 feature-gating table has been a documented design commitment since v1.0, and Phase 4's own exit criterion presumes it — but nothing enforces it. Every route gates by **role** (`require_role`) and by subscription **status** (`block_if_read_only`), never by **tier**: a brand-new trialing company (tier `pro`) can create invoices and connect QuickBooks today, both Enterprise-only per the table. This spec closes that gap at the API layer, per the pricing doc's own instruction that enforcement is server-side and never frontend-only.

## Decisions (made explicitly during design)

1. **Trials stay Pro-tier.** Registration keeps creating `trialing`/`pro` subscriptions, exactly per pricing doc Section 5 — which means trials lose the Enterprise modules (Accounting/Billing, Integrations) they can incorrectly use today. Existing tests that exercise Enterprise features from a fresh registration gain a set-tier test helper.
2. **The full table is enforced** — all five gated areas, not just the currently-exposed Enterprise gaps: Estimation (Pro+), Compliance (Pro+), Accounting & Billing (Enterprise), Integrations (Enterprise), nested child-branch creation (Enterprise).
3. **Writes blocked, reads stay open.** A below-tier module's mutating routes 403; its read routes keep working, so a downgraded company can still view existing invoices/estimates/etc. This deliberately supersedes pricing doc Section 6's "become inaccessible" wording (updated in the same commit as this spec): data preservation and visibility on downgrade is the kinder, support-ticket-avoiding behavior, and it is consistent with Section 5's own trial-expiry precedent (read-only, data preserved).
4. **Event-driven writes into gated modules are gated too.** `handle_estimate_approved` skips deposit-invoice drafting below Enterprise; `handle_financial_record_created` skips sync enqueuing below Enterprise. No back door into a gated module through the event bus.

## 1. The gating core: `app/core/tier_gating.py`

One new, small module — the single source of truth for the pricing table:

```python
TIER_RANK = {"starter": 0, "pro": 1, "enterprise": 2}

MODULE_MIN_TIER = {
    "estimation":     "pro",         # Estimation Engine incl. e-signature + change orders
    "compliance":     "pro",         # Compliance Tracking
    "accounting":     "enterprise",  # client invoicing (AR), bills (AP), expenses
    "integrations":   "enterprise",  # QuickBooks/FreshBooks
    "child_branches": "enterprise",  # nested child-company creation
}
```

**`require_module(module: str)`** — a dependency factory shaped exactly like `require_role`/`block_if_read_only`:

- Depends on `get_current_user`, sharing FastAPI's per-request dependency cache — no second JWT decode, no second session.
- Resolves the caller's ROOT company's subscription tier in ONE query, `func.get_root_company_id(...)` inlined in the WHERE clause — the identical pattern `block_if_read_only` and `subscriptions.py`'s `_get_subscription_for_current` already use (and which already works under the `subscriptions` table's upward-visibility RLS policy from a tenant-scoped session).
- Raises `403` with an upgrade-worded message (`"This feature requires the {min_tier} plan"`) when `TIER_RANK[tier] < TIER_RANK[MODULE_MIN_TIER[module]]`.
- **No subscription row → fails open**, mirroring `block_if_read_only`'s documented "unreachable state, not something to build defensive handling for" stance (every root company gets a subscription atomically at registration).
- **No method sniffing.** The dependency gates whatever route it is attached to; the writes-only semantics come from attaching it only to mutating routes. This is what makes the Integrations module gateable at all — its mutating routes (`connect`, `callback`) are GETs, so any `request.method`-based pass-through would exempt exactly the routes that most need gating.
- The factory stamps the module name onto the returned dependency (`dependency.tier_module = module`) so the completeness test (Section 5) can introspect not just presence but *correctness* of each route's gate.

**`tier_allows(session, company_id, module) -> bool`** — the same one-query tier resolution as a plain async helper, for the two consumers that have no `CurrentUser`: event handlers (Section 3) and the OAuth callback (Section 3). Fails open on a missing subscription row, same stance as above.

`require_module` and `block_if_read_only` are orthogonal and coexist on the same routes: status gates *whether the account is in good standing* (lapsed → no writes anywhere); tier gates *what the plan includes* (below plan → no writes in this module). A trialing company passes status and carries tier `pro` — exactly Pro-module access, per pricing doc Section 5.

## 2. Route retrofit map

The gate attaches per-route to mutating routes only — the same retrofit shape as the `block_if_read_only` rollout (Tasks 3.25–3.27). By module:

| Module | Router file(s) | Gated routes | Deliberately ungated (reads) |
|---|---|---|---|
| `estimation` | `catalogs.py`, `estimates.py`, `change_orders.py` | every POST/PUT: catalog items, markup profiles, estimate create / replace-lines / calculate / send-for-signature / approve / PDF export, change-order create / send-for-signature / approve | estimate/catalog/change-order GETs |
| `compliance` | `subcontractors.py`, `compliance.py`, `subcontractor_assignments.py` | every POST: subcontractors, compliance documents, notifications, assignments | compliance dashboard GET, all list/detail GETs |
| `accounting` | `invoices.py`, `bills.py`, `expenses.py` | every POST: invoice create/send/payments/void, bill create/payments/void, expense create | all invoice/bill/expense GETs, `GET /reports/profitability` (`reports.py` is GET-only and entirely ungated) |
| `integrations` | `integrations.py` | `GET /integrations/{provider}/connect` (mutating-flow GET; has a `CurrentUser`, normal dependency), `GET /integrations/{provider}/callback` (special-cased in-route — Section 3) | `GET /integrations/{provider}/sync-status` |
| `child_branches` | `companies.py` | exactly `POST /companies/{id}/children` | everything else in Users & Company — invitations, company reads, auth — is ungated in every tier |

On the pricing table's "Nested child-branch companies — Pro: *Single branch only*" cell: this spec reads it as "a Pro (or Starter) company operates as its own single branch and cannot create child companies" — i.e., child-branch **creation** requires Enterprise, which is why `child_branches` maps to `enterprise` for Starter and Pro alike. Existing child branches created while Enterprise remain readable/operable after a downgrade (reads-stay-open, Decision 3); only creating *new* ones is blocked.

Notes locked in deliberately:

- **Client-role approve routes are gated.** A downgrade mid-signature-flow blocks the client's approval — consistent with writes-blocked-below-tier, and the confirmation-warning flow on downgrade (pricing doc Section 6) is exactly where the contractor learns this.
- **`/projects/{id}/invoices` and `/projects/{id}/expenses` are `accounting` routes** despite their URL prefix — module membership follows the router/domain, not the path. (This is why a middleware path-prefix design was rejected.)
- `webhooks.py`, `auth.py`, `subscriptions.py`, `leads.py`, `projects.py`, `tasks.py` and the rest of Users & Company / CRM / Project Management are ungated at every tier, per the table's three universal rows.

## 3. The two non-dependency paths

**OAuth `callback`** (`GET /integrations/{provider}/callback`) has no `CurrentUser` — the signed `state` is its entire authentication — so `require_module` cannot attach. Instead, inside its existing `session_scope()` block, after `verify_oauth_state` decodes `company_id` and `set_current_tenant` runs, and before `_upsert_connection`: call `await tier_allows(session, company_id, "integrations")` and raise `HTTPException(403, ...)` if false. This closes the one realistic bypass — a `state` minted while Enterprise, redeemed within its 10-minute TTL after a downgrade — making it structurally impossible rather than merely unlikely. `callback` joins the completeness test's exclusion list with this justification (it is gated, just not via the introspectable dependency).

**Event handlers**, per Decision 4:

- `handle_estimate_approved`: after the existing `project_id is None` early return, check `tier_allows(session, company_id, "accounting")`; return silently if below tier — the same silent no-op shape as the existing early return. A Pro company approving an estimate gets the approval (estimation is in its tier) but no auto-drafted deposit invoice (accounting is not).
- `handle_financial_record_created`: check `tier_allows(session, company_id, "integrations")` before the connections query; return silently if below tier, so a downgraded company's leftover connection rows stop producing sync messages.

The Dramatiq actor itself does **not** re-check tier: by the time a message is enqueued the tier was verified at enqueue time, and re-checking per sync in the worker buys a race window measured in seconds at the cost of a query on every message.

## 4. Registration, downgrades, and existing data

- **Registration is untouched**: `trialing`/`pro`, per pricing doc Section 5.
- **Tier changes arrive via the existing Stripe webhook** (`POST /webhooks/stripe` updates the subscription row). No new upgrade/downgrade machinery in this spec; the confirmation-warning UX on downgrade (pricing doc Section 6) is Stripe-Portal/frontend territory and remains out of scope.
- **Existing data below tier is preserved and readable** (Decision 3). Nothing is deleted or hidden on downgrade; mutating it again requires re-upgrading.

## 5. Testing strategy

- **Completeness introspection test** (in the new `test_tier_gating.py`, alongside the behavior tests — one file for the feature, matching how `test_read_only_enforcement.py` holds both its behavior and completeness tests): a `ROUTER_MODULE` map (router → module for the five gated modules); walk `app.routes`; for every mutating route belonging to a gated router, assert a dependency with `tier_module == <the correct module>` is present — catching both a missed route and a copy-pasted wrong module. `connect` is asserted present-by-dependency despite being a GET (explicitly listed); `callback` is on the documented exclusion list (gated in-route). Same fail-safe introspection caveats as `test_read_only_enforcement.py`'s completeness test.
- **Behavior tests** (same file): below-tier 403 per module (a `starter` company blocked from estimation; a `pro` company blocked from accounting, from integrations connect, and from child-branch creation); at-tier success for each; reads-stay-open below tier (a `pro` company still GETs invoices/reports); callback-below-tier 403 (validly-signed state, tier since downgraded); the two event no-ops (a `pro` company's estimate approval drafts **no** invoice and writes **no** `invoice.auto_generated` audit row; a below-tier company with a seeded connection enqueues **no** sync message).
- **`set_subscription_tier(company_id, tier)` shared test helper** — an owner-role asyncpg UPDATE, same precedent as the read-only suite's status helper and the E2E script's `_set_subscription_status`. Every existing test that registers a company and then exercises an Enterprise module adds one call: the AR/AP suites, the integrations suites, the sync-handler suites, `test_estimate_approved_handler`'s deposit tests, the tenant-isolation files touching those tables, and the E2E script's Invoicing (Company G) and Integrations (Company H) blocks (which replicate the helper inline per that script's no-backend-imports convention). The full-suite double regression pass is the proof this retrofit is complete.
- **Unit tests for the core**: `TIER_RANK`/`MODULE_MIN_TIER` sanity (every module maps to a valid tier), `tier_allows` truth table across the 3×5 tier/module grid, fail-open on missing subscription.

## 6. Documentation updates (same commit as this spec)

- `docs/08-pricing-subscription-model.md`: Section 3's enforcement paragraph corrected — it says `TenantMiddleware` resolves the tier; the middleware has no DB session, and enforcement actually lives in a per-route FastAPI dependency (`require_module`), consistent with how `block_if_read_only` already works. Section 6's "become inaccessible" reworded to the decided semantics: mutating routes blocked, existing data remains readable.
- `docs/07-security-compliance.md`: the RBAC section gains a sentence that tier gating composes with role gating — role decides *who* within a company may act; tier decides *which plan* the company must be on.
- `docs/05-api-specification.md`: gated modules note the 403 tier response alongside the existing role/read-only 403s.

## Open questions deferred, not silently dropped

- **Downgrade confirmation warning UI** (pricing doc Section 6) — frontend/Stripe-Portal flow; the backend contribution (the tier actually changing via webhook, writes then blocking) is fully covered here.
- **Seat overage enforcement** — orthogonal to module gating; the seat-usage reporting job already exists, but blocking actions on seat overage is its own (undesigned) policy question.
- **Tier-aware frontend UX** (hiding gated modules in navigation) — frontend scope; this spec guarantees the server rejects regardless of what the UI shows, per the pricing doc's "never enforced only in the frontend UI."
