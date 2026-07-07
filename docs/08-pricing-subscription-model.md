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

Enforcement happens server-side: the `TenantMiddleware` (see [Technical Architecture](03-technical-architecture.md), Section 5) resolves the active company's `subscriptions.tier` and the relevant module's FastAPI dependency rejects requests with `403` if the tier doesn't include that module — never enforced only in the frontend UI.

## 4. Stripe Implementation Mapping

- Each tier is a Stripe **Product** with a **Price** (monthly and annual variants).
- Seat overage is modeled as a Stripe **metered/usage-based** line item on the subscription, incremented when a company's active user count exceeds the tier's included seats.
- `subscriptions` table (see [Database Schema](04-database-schema.md), Section 7) stores `stripe_customer_id`, `stripe_subscription_id`, `tier`, `status`, and `current_period_end`, kept in sync via the `/webhooks/stripe` endpoint ([API Specification](05-api-specification.md), Section 9).
- Plan changes (upgrade/downgrade) and cancellations are handled through Stripe's Customer Portal rather than custom UI, per the original architectural decision to avoid building billing logic from scratch.

## 5. Trial & Conversion

- New companies start on a time-boxed trial (e.g., 14 days) with **Pro**-tier feature access, to let a prospective subscriber experience the Estimation Engine — the feature most likely to demonstrate value over their current spreadsheet-based workflow.
- On trial expiry without a payment method, the account downgrades to a read-only state (data preserved, write access blocked) rather than being deleted — consistent with the data retention policy in [Security & Compliance](07-security-compliance.md), Section 7.

## 6. Upgrade / Downgrade Rules

- Upgrades take effect immediately (proration handled by Stripe).
- Downgrades that would remove access to a module currently in use (e.g., Enterprise → Pro while QuickBooks integration is active) surface a confirmation warning listing exactly what will become inaccessible, and take effect at the end of the current billing period rather than immediately.
