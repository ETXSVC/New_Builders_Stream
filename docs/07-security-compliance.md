# Builders Stream — Security & Compliance Plan

**Version:** 1.0
**Date:** 2026-07-07
**Related:** [Technical Architecture](03-technical-architecture.md) · [Functional Requirements](02-functional-requirements.md), Section 7

## 1. Authentication

- OIDC/JWT-based session management. Passwords hashed with a modern, salted algorithm (e.g., Argon2id) — never reversible encryption.
- JWTs are short-lived (e.g., 15 minutes) with a refresh-token rotation flow; refresh tokens are revocable server-side (e.g., on logout, password change, or suspected compromise). Implemented per [`docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md`](superpowers/specs/2026-07-16-auth-token-lifecycle-design.md): 15-minute access tokens, opaque SHA-256-hashed refresh tokens with 14-day absolute expiry, rotation with family-level reuse detection (`/auth/refresh`), and revocation via `/auth/logout` (family) and `/auth/change-password` (all the user's tokens). MFA/TOTP below remains this section's one open item — its own follow-up spec.
- Multi-factor authentication (TOTP) is a requirement for the Admin role at minimum, strongly recommended for all roles — should be scoped into an early phase given the platform handles client financial/contract data.

## 2. Authorization (RBAC Matrix)

| Module | Admin | Project Manager | Field Crew | Accountant | Client |
|---|---|---|---|---|---|
| Users & Company | Full CRUD | Read own company | — | — | — |
| CRM | Full CRUD | Full CRUD | — | — | — |
| Project Management | Full CRUD | Full CRUD | Read assigned + create Daily Logs | Read (financial fields only) | Read sanitized dashboard |
| Estimation | Full CRUD | Full CRUD | — | Read | Approve/reject own estimate (e-sign) |
| Accounting/Billing (AR) | Full CRUD | — | — | Full CRUD | Read own invoices |
| Accounting/Billing (AP) | Full CRUD | — | — | Full CRUD | — |
| Expenses | Full CRUD | — | — | Full CRUD | — |
| Compliance | Full CRUD | Read + assign (with override logging) | — | Read | — |
| Integrations | Full CRUD | — | — | Full CRUD | — |

AP (Bills, Bill Payments) is never Client-visible — unlike AR Invoices, which the Client is the actual recipient of, Bills represent the company's own internal obligations to its vendors/subcontractors. Expenses follow the identical never-Client-visible rule as AP, for the same reason — they record the company's own project costs, not anything billed to the Client. Integrations (QuickBooks/FreshBooks connections and sync status) follow the same rule for the same reason — third-party accounting sync is an internal bookkeeping concern, not something ever exposed to a Client. The one exception to `require_role` gating in this module is the OAuth `callback` route itself, which has no authenticated `CurrentUser` at all (an external redirect cannot carry a bearer token) — its security boundary is a signed, short-lived `state` parameter instead, the same structural pattern `/invitations/{id}/accept` already uses.

Enforced at two layers, per [Technical Architecture](03-technical-architecture.md), Section 5: a FastAPI dependency checks role before the request reaches business logic (fast-fail, clear error), and PostgreSQL RLS enforces tenant boundary regardless of application-layer bugs (defense in depth).

Role gating composes with **tier gating** ([Pricing Model](08-pricing-subscription-model.md), Section 3; design: [`docs/superpowers/specs/2026-07-15-tier-gating-design.md`](superpowers/specs/2026-07-15-tier-gating-design.md)): role decides *who within a company* may act on a module; tier decides *which subscription plan* the company must be on for that module's mutating routes to be available at all. Both reject with `403`, alongside the subscription-status read-only gate — three orthogonal per-route dependencies on the same routes.

## 3. Tenant Isolation

- Row-Level Security is the authoritative isolation mechanism (see [Technical Architecture](03-technical-architecture.md), Section 5) — application-layer `WHERE company_id = ...` filtering is a performance/clarity convenience, never the sole safeguard.
- Automated isolation tests are a **release gate**, not optional: no deploy to staging/prod proceeds if the RLS test suite (see [Test Strategy](10-test-strategy.md), Section 2) fails.

## 4. Encryption

- **In transit:** TLS 1.2+ enforced at the reverse proxy for all external traffic; internal Docker-network traffic between backend/DB/Redis is not exposed externally.
- **At rest:** PostgreSQL data volume encrypted at the disk/filesystem level (e.g., LUKS on the Proxmox host); third-party OAuth tokens (QuickBooks/FreshBooks — see [Database Schema](04-database-schema.md), Section 7) are additionally encrypted at the application layer before storage, never stored in plaintext.
- **Backups:** encrypted at rest in their off-host storage location.

## 5. Audit Logging

- Every state-changing action on financially or legally significant entities (Project status changes, Estimate approval, Change Order approval, Subcontractor compliance overrides, user role changes, Invoice send/payment/void, Bill payment/void, connecting a QuickBooks/FreshBooks integration) writes an `audit_log` row (see [Database Schema](04-database-schema.md), Section 8) recording who, what, when, and relevant metadata.
- Audit log entries are append-only; no application code path updates or deletes them.

## 6. E-Signature Workflow

Builders Stream implements a first-party e-signature capture for Estimates and Change Orders (see [Functional Requirements](02-functional-requirements.md), US-4.5 and US-3.6), rather than assuming a third-party e-sign service — the architecture doesn't preclude integrating one later (Section 9 of the [API Specification](05-api-specification.md) reserves a webhook route for that).

To satisfy the intent-to-sign standard under the U.S. ESIGN Act (and equivalent state UETA statutes):

- Capture: signer's typed/drawn signature, full name, email, timestamp, and originating IP address (`esignatures` table, [Database Schema](04-database-schema.md), Section 6).
- Present the signer with the exact document version being signed (the snapshotted Estimate or Change Order), not a live/mutable one.
- Retain the signed artifact and its metadata indefinitely as a legal record (see Section 7 below) — it must never be deleted, even if the underlying Project or company is later deactivated.
- **This is not a substitute for legal review.** Before this workflow is relied upon for binding contracts, it should be reviewed by counsel familiar with construction contract law in the jurisdictions Builders Stream's subscribers operate in — this document specifies the technical capture requirements, not a legal opinion on enforceability.

## 7. Data Retention

| Record Type | Retention |
|---|---|
| E-signature records (`esignatures`) | Indefinite / immutable |
| Audit log | Minimum 7 years (typical construction-contract statute-of-limitations window; confirm against subscribers' jurisdictions) |
| Compliance documents (insurance/license) | Retained for the life of the subcontractor relationship + 7 years |
| General operational data (tasks, daily logs, communications) | Retained for the life of the tenant account; purged within 90 days of a company's confirmed account deletion request, except where audit/e-signature retention above overrides |
| Deactivated company data | Not deleted on deactivation (`is_active = false`) — only on explicit, confirmed deletion request |

## 8. Compliance Tracking Feature (Insurance/License Expiry)

- Implements [Functional Requirements](02-functional-requirements.md) US-7.1–US-7.3: notification cadence (30/14/7 days before expiry), compliance dashboard, and mandatory Admin-logged override to assign a subcontractor with expired documentation.
- This is a workflow/notification feature within Builders Stream, not a certification authority — the platform does not verify the authenticity of uploaded insurance certificates or licenses; that remains the subscribing company's responsibility. State this limitation clearly in-product.

## 9. Incident Response (Outline)

- Defined escalation path for a suspected data breach or tenant-isolation failure: immediate containment (revoke affected credentials/tokens), root-cause investigation, affected-tenant notification per applicable state breach-notification law, and a post-incident review.
- Given the solo-developer/self-hosted context, this is deliberately lightweight in v1 but must exist in writing (a runbook) before the platform holds real subscriber financial/contract data — not deferred indefinitely.

## 10. Third-Party Data Processors

Any subprocessor handling subscriber data (Stripe, email delivery provider, error tracking, QuickBooks/FreshBooks) must be listed in Builders Stream's Terms of Service / Privacy Policy (a legal deliverable outside the scope of this technical document, but its accuracy depends on this Security & Compliance Plan staying current).
