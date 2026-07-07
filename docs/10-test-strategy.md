# Builders Stream — Test Strategy

**Version:** 1.0
**Date:** 2026-07-07
**Related:** [Technical Architecture](03-technical-architecture.md) · [Security & Compliance](07-security-compliance.md) · [Roadmap](09-roadmap-implementation-plan.md)

## 1. Test Pyramid

| Layer | Scope | Tooling |
|---|---|---|
| Unit | Estimation calculation engine, state machines (Project/Lead/Estimate status), Pydantic validation | pytest |
| Integration | API contract per module, database RLS enforcement, event bus wiring | pytest + a real PostgreSQL test database (not mocked) |
| End-to-End | Full user flows through the Next.js frontend against a running backend | Playwright |
| Load/Performance | Multi-tenant query performance at scale | k6 or Locust against a seeded staging dataset |

Business logic (estimation math, state transitions, RLS enforcement) is verified against a real database and real calculations — not mocked — per this project's standing practice of testing against real dependencies rather than mocks that can silently drift from production behavior.

## 2. Tenant Isolation Testing (Release Gate)

This is the single most important test category given the shared-schema multi-tenancy model ([Technical Architecture](03-technical-architecture.md), Section 5) — a failure here is a data breach, not a bug.

**Required test cases, run in CI on every merge to `main`:**

1. Seed two unrelated companies (A and B) each with Leads, Projects, and Estimates.
2. Authenticate as a user in Company A; attempt to `GET`/`PATCH`/`DELETE` every entity type by Company B's IDs directly (not just list endpoints — direct ID access is the common bypass). Assert `404`/`403` on every attempt, never a `200` with B's data.
3. Seed a parent company P with child branches C1 and C2. Authenticate as a P-level user; assert visibility into both C1 and C2's data. Authenticate as a C1 user; assert **no** visibility into C2's data (siblings must stay isolated even though both share a parent).
4. Deliberately omit the `X-Tenant-ID` header / use a malformed one; assert the request is rejected rather than silently defaulting to some other tenant.
5. A regression test that runs a query with RLS temporarily disabled (in test setup only) to confirm the policy itself — not just application code — is what blocks cross-tenant access; this catches the case where a policy is accidentally dropped or misconfigured in a migration.

**This suite failing blocks deployment to staging and production**, per [Security & Compliance](07-security-compliance.md), Section 3.

## 3. Estimation Engine Testing

- Verify the fixed calculation order (quantity × rate → category subtotal → overhead → profit → tax) against hand-computed expected values for representative estimates.
- Verify all monetary math uses `decimal`, not `float` — a test that would catch a regression back to floating-point rounding errors.
- Verify historical immutability: change a `cost_catalog_items.unit_rate` after an Estimate is approved and assert the approved Estimate's totals are unchanged.
- Verify parent/child catalog override precedence.

## 4. State Machine Testing

- For Project, Lead, and Estimate status fields: table-driven tests asserting every *legal* transition succeeds and every *illegal* transition (e.g., Project Draft → Completed) is rejected with `409`.

## 5. E-Signature & Compliance Testing

- Verify an Estimate/Change Order cannot be approved without a captured signer name, email, timestamp, and IP.
- Verify the exact document version presented to the signer matches the snapshotted version stored (no live-document substitution bug).
- Verify a Subcontractor with an expired compliance document cannot be assigned to a Project without an Admin role and a recorded `override_reason`, and that the attempt is written to `audit_log`.

## 6. Third-Party Integration Testing

- **Stripe:** test-mode API keys and Stripe CLI's webhook forwarding for local/staging testing of subscription lifecycle events; verify idempotent webhook handling (Stripe can redeliver events).
- **QuickBooks/FreshBooks:** sandbox accounts for both providers; verify sync retries on transient failure and that a sync failure never blocks the originating Builders Stream action (invoice/expense creation succeeds even if the downstream sync later fails and is retried).

## 7. Performance/Load Testing

- Before the 1,000-subscriber target, run a load test against a seeded dataset simulating that scale (companies, nested branches, leads, projects, estimates) to validate the performance targets in [NFRs](06-nonfunctional-requirements.md), Section 1, and to establish the actual baseline for the RLS-caching trigger in [NFRs](06-nonfunctional-requirements.md), Section 2 — not skipped in favor of assuming it'll be fine.

## 8. CI Gates

A change cannot merge to `main` unless:

1. Unit + integration test suites pass.
2. Tenant isolation suite (Section 2) passes.
3. Linting/type-checking passes (`mypy`/`ruff` for backend, `tsc`/`eslint` for frontend).
4. OpenAPI schema diff is reviewed when backend contracts change, to catch accidental breaking changes to the frontend's generated types (see [Technical Architecture](03-technical-architecture.md), Section 2).

E2E (Playwright) and load tests run on a scheduled/pre-release basis rather than on every commit, given their runtime cost, but are mandatory before promoting a Phase's exit criteria (see [Roadmap](09-roadmap-implementation-plan.md)) to production.
