# Billing (Builders Stream Subscription) — Design Spec

**Date:** 2026-07-13
**Depends on:** Phase 0 (foundation), Phase 1 (CRM & Project Management), Phase 2 (Estimation Engine + E-Signature), Compliance Tracking — all merged to `main`.
**Scope source:** [`docs/01-prd.md`](../../01-prd.md) Section 6/8, [`docs/02-functional-requirements.md`](../../02-functional-requirements.md) Section 5 (US-5.1), [`docs/04-database-schema.md`](../../04-database-schema.md) Section 7, [`docs/05-api-specification.md`](../../05-api-specification.md) Sections 6 & 9, [`docs/07-security-compliance.md`](../../07-security-compliance.md) Sections 2, 7, 10, [`docs/08-pricing-subscription-model.md`](../../08-pricing-subscription-model.md), [`docs/09-roadmap-implementation-plan.md`](../../09-roadmap-implementation-plan.md) Phase 3.

## Decomposition note

`docs/09-roadmap-implementation-plan.md`'s "Phase 3" bundles Compliance Tracking, Builders Stream's own Stripe subscription billing, and client-facing invoicing/expenses/profitability reporting for a *business* reason (all needed before Enterprise-tier onboarding). Per the same explicit user decision that produced the Compliance Tracking spec, these are being planned and built as **three separate specs/plans, one at a time**. Compliance Tracking is complete and merged. This spec covers **Billing only** — Builders Stream's own subscription billing (US-5.1: tier, seats, Stripe Customer Portal). Client-facing Project invoicing, Expense tracking, and profitability reporting (US-5.2–US-5.4) are their own future spec, kept separate per `docs/02-functional-requirements.md`'s own business rule: "Builders Stream subscription billing ... and client-facing Project invoicing ... are separate financial flows and must not be conflated in the data model."

## Scope

**Backend-only**, matching every prior phase's precedent. Explicitly **excluded** from this spec (each a deliberate, discussed scope cut, not an oversight):

- **Tier-based module gating** (e.g., blocking Starter-tier companies from Estimation/Compliance routes) is NOT built here. No route in the codebase enforces tier today; retrofitting that is its own future, separately-scoped task once Billing itself is proven.
- **Real Stripe credentials/SDK wiring** is NOT built here. This spec is built entirely behind a `StripeClient` interface with an in-memory fake as the only implementation exercised in tests and wired by default. Swapping in the real `stripe` SDK against live test-mode credentials is a follow-up task, not a code change to this spec's design.
- **Client-facing invoicing, expenses, profitability reporting** (US-5.2–US-5.4) — separate future spec, per the decomposition note above.

**Included**: the `subscriptions` table, trial creation on registration, `GET /subscriptions/me`, `POST /subscriptions/portal-session`, `POST /webhooks/stripe` (with signature verification and status sync), seat-overage metering via a daily scheduled job, and read-only enforcement across existing write routes when a subscription lapses.

## 1. Data Model

```sql
CREATE TABLE subscriptions ( -- Builders Stream's own SaaS billing, distinct from client invoicing
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id),
    stripe_customer_id VARCHAR(255) NOT NULL,
    stripe_subscription_id VARCHAR(255) NOT NULL,
    tier VARCHAR(20) NOT NULL CHECK (tier IN ('starter','pro','enterprise')),
    status VARCHAR(20) NOT NULL,
    included_seats INTEGER NOT NULL,
    current_period_end TIMESTAMPTZ
);
CREATE UNIQUE INDEX idx_subscriptions_company_id ON subscriptions(company_id);
CREATE UNIQUE INDEX idx_subscriptions_stripe_subscription_id ON subscriptions(stripe_subscription_id);
```

Base shape matches the pre-sketched table in `docs/04-database-schema.md` Section 7, built as-is, with one addition: **`included_seats`**. The pricing doc explicitly defers exact per-tier numbers to a pending business decision, not a technical one — rather than hardcoding seat counts in application code, they're stored per-subscription (set from a per-tier default at creation, changeable later without a migration).

**Root-only ownership**: a `subscriptions` row may only belong to a company with `parent_id IS NULL`. Not expressible as a plain `CHECK` constraint (would need a trigger to inspect another table); enforced instead at the application layer, at the single point subscriptions get created (registration — see Section 2). `idx_subscriptions_company_id` is `UNIQUE` because each root company has at most one subscription. Child branches have no subscription of their own; their effective tier/status is resolved by walking up to their root ancestor via a new `get_root_company_id(company_id)` SQL function — the mirror-image of the existing `get_all_descendant_ids()`, built the same way (`SECURITY DEFINER`, `REVOKE EXECUTE ... FROM PUBLIC` + `GRANT ... TO app_user` only, per that function's own established rationale in migration `0001`) since it's used both inside the RLS policy below and directly by application code. Reused by every consumer that needs "which subscription governs this tenant": registration is the only writer; the RLS policy itself, the read-only dependency, and the seat-usage job are all readers.

**RLS is deliberately NOT the standard `tenant_isolation` policy shape.** Every other company-scoped table in this codebase grants *downward* visibility only (a session scoped to a parent can see a descendant's rows, via `get_all_descendant_ids(current_tenant)`). `subscriptions.company_id` is always a root, so a session scoped to a *child* branch — which is exactly who needs to resolve "my effective subscription" — needs the opposite, *upward*, direction: visibility into its own root's row, which no existing policy grants. `subscriptions` gets its own policy shape instead:

```sql
CREATE POLICY tenant_isolation ON subscriptions
    USING (company_id = get_root_company_id(current_setting('app.current_tenant')::uuid));
```

A session can only ever see the one row belonging to its own root ancestor (or itself, if it *is* the root — `get_root_company_id` returns its own id in that case, matching `get_all_descendant_ids()`'s own base-case convention). This is narrower than the standard policy in one sense (a parent can't browse a child's *own* view of the subscription, but there isn't one — every branch shares the same single row) and is the first table in this codebase whose access model isn't the standard downward-tree shape, which is exactly why it isn't reusing that policy rather than bending it to fit.

## 2. Stripe Abstraction

`app/services/stripe_client.py` defines a `StripeClient` protocol with exactly the operations this feature needs:

```python
class StripeClient(Protocol):
    async def create_customer(self, *, email: str, name: str) -> str: ...  # returns stripe_customer_id
    async def create_trialing_subscription(
        self, *, customer_id: str, tier: str, trial_days: int
    ) -> StripeSubscription: ...  # returns {stripe_subscription_id, status, current_period_end}
    async def create_portal_session(self, *, customer_id: str) -> str: ...  # returns portal URL
    async def report_seat_usage(self, *, stripe_subscription_id: str, quantity: int) -> None: ...
    def verify_webhook_signature(self, *, payload: bytes, signature_header: str) -> dict: ...  # returns parsed event or raises
```

Production and test code both depend only on this protocol — the same "swap the real thing for a fake behind an interface" pattern `document_storage.py` already establishes (filesystem today, could be S3 later) applied to an external paid API instead of a filesystem. The only implementation built as part of this spec/plan is `FakeStripeClient` — deterministic, in-memory, no network calls — wired as the default dependency everywhere, including local dev. A `RealStripeClient` wrapping the `stripe` SDK is explicitly out of scope (see Scope section); when it's built later, it's a new class implementing the same protocol plus a settings-driven dependency swap, not a change to any of the code this spec produces.

## 3. Registration & Trial Flow

On `POST /auth/register` (top-level company creation only — `POST /companies/{id}/children` is untouched), the handler synchronously calls `StripeClient.create_customer()` then `create_trialing_subscription(tier="pro", trial_days=14)`, and writes the resulting `subscriptions` row (`tier="pro"`, `status="trialing"`, `included_seats` = the Pro-tier default, `current_period_end` = trial end) in the same transaction as the new company/user/membership rows. If the Stripe call fails, registration fails too (502) — no retry/fallback path; a trial-less root company isn't a state this feature tolerates, consistent with this codebase's "don't add error handling for scenarios that shouldn't happen" convention.

**Per-tier `included_seats` defaults**, a module-level constant dict (`app/services/billing.py` or similar — home decided during planning): `{"starter": 3, "pro": 10, "enterprise": 25}`. These specific numbers are explicitly placeholders (see Open Questions) — chosen so the implementation and its tests have concrete values to work with, not because they're validated pricing. Changing them later is a one-line constant edit, not a migration, since the actual per-subscription value lives in the `included_seats` column.

## 4. Endpoints & Webhook Sync

| Route | Method | RBAC | Behavior |
|---|---|---|---|
| `/subscriptions/me` | GET | Admin, Accountant | Resolves caller's root company, returns `tier`/`status`/`current_period_end`/`included_seats`. |
| `/subscriptions/portal-session` | POST | Admin only | Calls `create_portal_session`, returns the portal URL. Account-level action (payment method, cancellation), matches US-5.1's "As an Admin" framing exactly. |
| `/webhooks/stripe` | POST | None (public receiver) | Verifies `Stripe-Signature` against the **raw request body bytes** before processing anything. |

Webhook handles exactly three event types — the minimum needed to keep `subscriptions` an accurate mirror of Stripe's own state:

- `customer.subscription.updated` → sync `status`/`tier`/`current_period_end`
- `customer.subscription.deleted` → `status = "canceled"`
- `invoice.payment_failed` → `status = "past_due"`

Any other event type is acknowledged with `200` (Stripe retries on non-2xx) and otherwise ignored — out of scope, not silently swallowed. No dedup/event-log table: all three handlers are idempotent last-write-wins updates keyed on `stripe_subscription_id`, so Stripe's at-least-once redelivery is harmless without extra bookkeeping. An `audit_log` entry (via the existing `write_audit_log`) is written whenever a webhook actually changes `status` (old vs. new compared before commit) — not on routine `current_period_end`-only refreshes.

## 5. Seat-Overage Metering

New `app/tasks/seat_usage.py`, mirroring `app/tasks/compliance_expiry.py`'s exact shape (undecorated `_report_seat_usage(session_factory)` for direct testability, wrapped by a decorated `report_seat_usage = dramatiq.actor(...)`), wired into the existing `scheduler.py` as a second daily `add_job(...)` call alongside the compliance-expiry job — no new scheduler infrastructure.

Uses the same owner-role engine pattern (`settings.migrations_database_url`) as the compliance job — legitimately cross-tenant, since it scans every company's subscription. For each `subscriptions` row with `status IN ('trialing','active','past_due')` (skips `'canceled'`), counts `SELECT COUNT(DISTINCT user_id) FROM company_users WHERE company_id IN (SELECT id FROM get_all_descendant_ids(subscription.company_id))` — unique users across the whole branch tree, so a person with membership in two branches counts once. There is no separate "active" flag on `company_users` in this codebase; a membership row's existence is the signal. If the count exceeds `included_seats`, calls `StripeClient.report_seat_usage(subscription_id, count - included_seats)`.

## 6. Read-Only Enforcement

New `block_if_read_only` dependency (`app/core/deps.py`, alongside `require_role`): takes `request: Request` and `current: CurrentUser = Depends(get_current_user)` (FastAPI caches this per-request, so no duplicate DB work versus the route's own `require_role` dependency). GET/HEAD/OPTIONS pass through. Any other method resolves the caller's root company and checks its `subscriptions.status`: anything other than `'trialing'`/`'active'` → `403`. This collapses Stripe's more granular dunning states into one simple rule rather than mirroring Stripe's exact status machine. A root with no subscription row at all (should be unreachable — every root gets one atomically at registration) fails open rather than blocking.

Every existing write route (roughly 15 router files spanning every phase built so far: Leads, Communication Logs, Projects, Phases/Tasks, Documents, Daily Logs, Cost Catalog, Markup Profiles, Estimates, Estimate lines, Esignatures, Change Orders, Subcontractors, Compliance Documents, Subcontractor Assignments) gets `_ro: None = Depends(block_if_read_only)` added alongside its existing `require_role(...)`. This is the one deliberate exception to this spec's "no broad retrofit" boundary — sized as its own dedicated task(s) in the implementation plan, not folded into the endpoint tasks above.

## 7. Testing Strategy

- `FakeStripeClient` is the only `StripeClient` implementation exercised anywhere in the test suite — no real network calls.
- Webhook signature verification is fully testable without a live Stripe account (HMAC over the raw body + a shared secret — same technique the `stripe` SDK's own test utilities use): both a valid-signature happy path and a tampered-signature rejection.
- Standard RBAC tests per endpoint; trial-creation-on-registration (extends existing registration tests); root-only subscription ownership (a child-branch session resolves to its root's subscription); seat-usage job tested directly via its undecorated function, using the fresh-single-use-owner-engine-per-call pattern established in Compliance Tracking's own `_seed_notifications` helper (Task 3.10) rather than a shared pooled engine, for the same deadlock-avoidance reason documented there.
- **Read-only enforcement completeness check**: given the ~15-router blast radius in Section 6, coverage relies on a single introspection test over the live FastAPI app's route table (`app.routes`), asserting every non-GET route — except `/auth/*`, `/webhooks/*`, and `/subscriptions/portal-session` itself — has `block_if_read_only` in its dependency list. This catches a route missing the dependency now and for any future router added after this feature ships, rather than relying on a hand-maintained list of routes to check.

## Open Questions Deferred, Not Silently Dropped

- Exact per-tier `included_seats` defaults and Stripe Product/Price configuration are a pending business decision (per `docs/08-pricing-subscription-model.md` Section 2's own note) — placeholder defaults will be used, easily changed via the `included_seats` column without a migration.
- Tier-based module gating retrofit, real Stripe SDK wiring, and Invoicing/Expenses/Profitability are each explicitly out of scope here (see Scope section) and remain open follow-up work.
