# Builders Stream — Roadmap & Implementation Plan

**Version:** 1.0 — phase statuses annotated 2026-07-27
**Date:** 2026-07-07
**Related:** [PRD](01-prd.md) · [Functional Requirements](02-functional-requirements.md)

Pacing assumption: solo developer, no fixed external deadline. Phases are ordered by **dependency**, not by calendar — each phase should be substantially complete (including its tests, per [Test Strategy](10-test-strategy.md)) before the next begins, since later modules assume earlier ones exist and work correctly.

![Implementation roadmap phases 0 through 5](images/05-roadmap.png)

## Phase 0 — Foundation (Pre-MVP)

**Status: shipped.**

**Goal:** nothing else can be built safely without this.

- Dockerized dev environment (PostgreSQL, Redis, backend, frontend containers) per [Technical Architecture](03-technical-architecture.md), Section 8.
- `companies` / `users` / `company_users` schema + nested-hierarchy `parent_id` + `get_all_descendant_ids()` function.
- RLS policies enabled and proven on the Users & Company tables.
- `TenantMiddleware`: JWT validation → `contextvars` → `SET LOCAL app.current_tenant`.
- Auth: registration, login, invitations.
- Audit log table and a working write path.
- **Exit criteria:** automated RLS isolation tests (two companies, verify zero cross-visibility) pass in CI.

## Phase 1 — CRM & Project Management (MVP core)

**Status: shipped.**

- CRM: Lead CRUD, pipeline status transitions, communication logs.
- Project Management: Project CRUD/lifecycle state machine, Phases, Tasks, Documents, Daily Logs.
- `LEAD_WON` → draft Project event wiring (see [Technical Architecture](03-technical-architecture.md), Section 4).
- Client-facing read-only project dashboard (sanitized view, RBAC-scoped).
- **Exit criteria:** a Lead can be created, moved to Won, and land as a Draft Project with client details carried over, end-to-end, with tests.

## Phase 2 — Estimation Engine + E-Signature (MVP completion)

**Status: shipped.**

- Cost Catalog, Markup Profiles (with parent/child override inheritance).
- Estimate creation, line items, server-side calculation pipeline (fixed-point decimal, fixed order of operations).
- PDF export as an async job.
- E-signature capture flow for Estimate approval (`esignatures` table, [Security & Compliance](07-security-compliance.md), Section 6).
- Change Order creation + e-signature approval on active Projects (reuses the e-signature capability built for Estimates).
- Historical immutability / snapshotting on approval.
- `ESTIMATE_APPROVED` event published (consumed starting in Phase 3).
- **Exit criteria: this is the MVP launch bar.** Users/Company + CRM + Project Management + Estimation Engine (including e-signature) are feature-complete, tested, and deployed to production. This matches the MVP scope defined in [PRD](01-prd.md), Section 6.

## Phase 3 — Compliance Tracking + Accounting/Billing (Post-MVP)

**Status: shipped.**

Grouped together because both are needed before Enterprise-tier subscribers can be onboarded (see [Pricing Model](08-pricing-subscription-model.md), Section 3), and Billing's invoice generation is the natural consumer of the `ESTIMATE_APPROVED` event from Phase 2.

- Subcontractor/Vendor records, compliance document upload + expiry notifications, compliance dashboard, assignment override + audit logging.
- Builders Stream's own Stripe subscription billing (tiers, seats, Customer Portal, webhook sync).
- Client-facing Project invoicing (AR), vendor Bills (AP), and Expense tracking.
- Profitability reporting, including AR/AP aging and estimated tax liability.
- **Exit criteria:** a company can subscribe/pay via Stripe, an approved Estimate flows into a draft client invoice automatically, and Accountants can track both money owed to and owed by the company through to payment.

## Phase 4 — External Integrations (Post-MVP)

**Status: shipped. The remaining gap is credentials, not code.** `RealQuickBooksClient` and `RealFreshBooksClient` are real SDK-free HTTP clients (migration 0030, PR #117), and `get_accounting_client` selects them **per provider**, gated on that provider's own client id being configured — a tenant can have real QuickBooks while FreshBooks is still the fake, because the two are separate OAuth applications with separate approval processes. With neither configured, the fake is returned and nothing moves real money. See `backend/app/services/accounting_client.py`.

- QuickBooks OAuth connect + async sync of invoices/expenses.
- FreshBooks OAuth connect + async sync (same pattern).
- Sync status visibility and retry-on-failure handling.
- Payments sync per payment, not per settled invoice (`INVOICE_PAYMENT_RECORDED`, migration 0032) — an invoice paid in three instalments would otherwise read as unpaid until the last one landed.
- **Exit criteria:** Enterprise-tier companies can connect and see a successful, monitored sync. **Not yet demonstrable** — it needs approved OAuth applications on both providers, which is a signup-and-configuration task rather than an engineering one.

## Phase 5 — Open Items (Scope TBD, Not Yet Scheduled)

**Status: not started.**

These are explicitly deferred pending decisions noted in [PRD](01-prd.md), Section 8, and are **not** part of any committed phase above:

- ~~Offline-capable mobile/PWA support for field crews.~~ **Partly shipped
  2026-08-03, and the PRD's framing of this question was the wrong one.**
  Scoping it found that the driver is an *estimator on site* capturing an
  estimate, not a field crew — a different role, a much larger write
  surface, and the case that actually needs a cached app shell. That case
  is built: `/estimates/capture`, cold-starting with no network, with the
  design and the decisions behind it in
  `docs/superpowers/specs/2026-08-02-offline-capture-screen-design.md` and
  its parent `2026-08-02-offline-pwa-design.md`. The **field-crew write
  queue remains unscheduled**: two writes, both independent and
  append-only, no cached documents and no security implications — it shares
  almost nothing with the estimator case beyond the words "offline
  support," and should be decided on its own merits.
- AI-assisted blueprint takeoff for the Estimation Engine.
- Multi-currency / multi-language support.

## Milestone Summary

| Phase | Delivers | Depends On |
|---|---|---|
| 0 | Multi-tenant foundation, auth, RLS | — |
| 1 | CRM + Project Management | Phase 0 |
| 2 | Estimation + e-signature — **MVP launch** | Phase 1 |
| 3 | Compliance + Billing | Phase 2 |
| 4 | QuickBooks/FreshBooks | Phase 3 |
| 5 | Open items (unscheduled) | Varies |
