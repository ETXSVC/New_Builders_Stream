# Builders Stream — Pricing & Subscription Model

**Version:** 1.0
**Date:** 2026-07-07
**Related:** [PRD](01-prd.md) · [Functional Requirements](02-functional-requirements.md), Section 5

## 1. Model: Tiered Plans by Feature + Seat Count

Three tiers gate access by **module availability** and scale cost by **active user seats**. This rewards companies that grow their team while keeping the entry tier affordable for a small renovation shop.

## 2. Tier Definitions

| Tier | Target Company Size | Modules Included | Seat Model |
|---|---|---|---|
| **Starter** | Solo contractor / very small crew | Users & Company, CRM, Project Management | Included seats: small fixed number; overage priced per seat |
| **Pro** | Small/medium company (the primary target for the 1,000-subscriber goal) | Everything in Starter + Estimation Engine + Compliance Tracking | Included seats: moderate; overage priced per seat |
| **Enterprise** | Multi-branch companies using the nested company hierarchy | Everything in Pro + Accounting/Billing + QuickBooks/FreshBooks Integration + nested child-branch support | Custom seat pricing; volume discount |

**Note on exact dollar amounts:** this document intentionally does not invent per-tier prices. Pricing should be set from real market research (competitor pricing, willingness-to-pay interviews with target contractors) before launch, and is tracked as [PRD](01-prd.md) Open Question #2. The structure above (what's gated by tier) is the design decision this document commits to; the numbers are a pending business decision, not a technical one.

## 3. Feature Gating Table

| Module | Starter | Pro | Enterprise |
|---|:---:|:---:|:---:|
| Users & Company Management | ✓ | ✓ | ✓ |
| CRM | ✓ | ✓ | ✓ |
| Project Management | ✓ | ✓ | ✓ |
| Estimation Engine (incl. e-signature) | — | ✓ | ✓ |
| Compliance Tracking | — | ✓ | ✓ |
| Accounting & Billing | — | — | ✓ |
| QuickBooks / FreshBooks Integration | — | — | ✓ |
| Nested child-branch companies | — | Single branch only | ✓ |

**The table above is the default, not the last word.** A per-tenant override
(`company_module_overrides`, [Database Schema](04-database-schema.md),
Section 9) takes precedence over the tier in either direction: support can
grant a module the plan withholds — a pilot, a goodwill concession, an
enterprise trial — or withhold one the plan grants, without moving the
customer's plan or touching Stripe. Overrides are set through the platform
console ([API Spec](05-api-specification.md), Section 10), are held by the
**root** company so they apply to every branch in that tree, and are recorded
in that tenant's audit log. A tenant with no override row behaves exactly as
this table says.

Enforcement happens server-side: a per-route FastAPI dependency (`require_module`, see [`docs/superpowers/specs/2026-07-15-tier-gating-design.md`](superpowers/specs/2026-07-15-tier-gating-design.md)) resolves the active company's ROOT-company `subscriptions.tier`, applies any override, and rejects requests with `403` if the result doesn't include that module — never enforced only in the frontend UI. (Earlier versions of this document attributed tier resolution to the `TenantMiddleware`; the middleware has no database session — enforcement lives in the same per-route dependency layer as role checks and read-only enforcement.) Gating applies to a module's **mutating** routes; read routes stay open at every tier so existing data remains visible after a downgrade — see Section 6.

## 4. Stripe Implementation Mapping

- Each tier is a Stripe **Product** with a **Price** (monthly and annual variants).
- Seat overage is modeled as a Stripe **metered/usage-based** line item on the subscription, incremented when a company's active user count exceeds the tier's included seats.
- `subscriptions` table (see [Database Schema](04-database-schema.md), Section 7) stores `stripe_customer_id`, `stripe_subscription_id`, `tier`, `status`, and `current_period_end`, kept in sync via the `/webhooks/stripe` endpoint ([API Specification](05-api-specification.md), Section 9).
- That sync is **last-write-wins on `status`**, which is why the table also carries `manual_status_override`. When an operator sets a status by hand from the platform console, the flag is set and the webhook stops applying Stripe's `status` to that row — otherwise the next routine `customer.subscription.updated` event would silently revert the decision, with no error and nothing in the logs. `current_period_end` keeps syncing regardless: that is Stripe's own fact to own, and suppressing it would leave the row claiming a period that has passed. Clearing the override hands `status` back to Stripe.
- Plan changes (upgrade/downgrade) and cancellations are handled through Stripe's Customer Portal rather than custom UI, per the original architectural decision to avoid building billing logic from scratch.

## 5. Trial & Conversion

- New companies start on a time-boxed trial (e.g., 14 days) with **Pro**-tier feature access, to let a prospective subscriber experience the Estimation Engine — the feature most likely to demonstrate value over their current spreadsheet-based workflow.
- On trial expiry without a payment method, the account downgrades to a read-only state (data preserved, write access blocked) rather than being deleted — consistent with the data retention policy in [Security & Compliance](07-security-compliance.md), Section 7.

## 6. Upgrade / Downgrade Rules

- Upgrades take effect immediately (proration handled by Stripe).
- Downgrades that would remove access to a module currently in use (e.g., Enterprise → Pro while QuickBooks integration is active) surface a confirmation warning listing exactly what will be affected, and take effect at the end of the current billing period rather than immediately.
- After a downgrade, the removed module's **mutating** actions are blocked (`403`), but its existing data remains readable — a company that drops Enterprise can still view (not modify) its old invoices, bills, and expenses, consistent with Section 5's trial-expiry precedent (data preserved, write access blocked) and the data retention policy in [Security & Compliance](07-security-compliance.md), Section 7. Event-driven writes into a removed module stop too: e.g., approving an Estimate below Enterprise no longer auto-drafts a deposit invoice, and leftover integration connections stop enqueuing syncs.
