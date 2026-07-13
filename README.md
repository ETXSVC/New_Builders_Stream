# Builders Stream

A multi-tenant SaaS platform for small/medium building construction and renovation companies, unifying CRM, project management, cost estimation, accounting/billing, and company administration into a single system.

**Status:** MVP complete (Phases 0–2), backend-only. Phase 3 (Compliance Tracking) is in active development. See [Build Status](#build-status) below for what's actually implemented versus what's still spec/docs only.

## Documentation

The full document set lives in [`docs/`](docs/) and a combined, presentation-ready copy is at [`Builders Stream.docx`](Builders%20Stream.docx) (title page, table of contents, and diagrams).

| Doc | Contents |
|---|---|
| [01-prd.md](docs/01-prd.md) | Product vision, personas, goals, MVP boundary, open questions |
| [02-functional-requirements.md](docs/02-functional-requirements.md) | User stories + acceptance criteria for every module |
| [03-technical-architecture.md](docs/03-technical-architecture.md) | Stack, modular monolith design, tenant/RLS model, deployment topology |
| [04-database-schema.md](docs/04-database-schema.md) | Full ERD and SQL schema |
| [05-api-specification.md](docs/05-api-specification.md) | Conceptual REST endpoint contracts per module |
| [06-nonfunctional-requirements.md](docs/06-nonfunctional-requirements.md) | Performance, scalability, availability, backup, infra sizing |
| [07-security-compliance.md](docs/07-security-compliance.md) | RBAC matrix, encryption, audit logging, e-signature, retention |
| [08-pricing-subscription-model.md](docs/08-pricing-subscription-model.md) | Tiered pricing structure and Stripe billing model |
| [09-roadmap-implementation-plan.md](docs/09-roadmap-implementation-plan.md) | Phased build plan (Phase 0–5), MVP launch bar |
| [10-test-strategy.md](docs/10-test-strategy.md) | Test pyramid, tenant-isolation release gate, test cases |

Start with the [PRD](docs/01-prd.md) for the "why," then [Technical Architecture](docs/03-technical-architecture.md) for the "how."

## Build Status

Implemented against the [roadmap](docs/09-roadmap-implementation-plan.md), on `main` unless noted:

| Phase | Status | Notes |
|---|---|---|
| 0 — Foundation | ✅ Done | Multi-tenant schema, RLS, auth, audit log, CI |
| 1 — CRM & Project Management | ✅ Done | Leads, Projects, Phases/Tasks, Documents, Daily Logs |
| 2 — Estimation Engine + E-Signature | ✅ Done — **MVP launch bar met** | Cost catalog, markup profiles, estimate calculation, PDF export, e-signature, Change Orders |
| 3 — Compliance Tracking | 🚧 In progress | On branch `feature/compliance-tracking`; Subcontractor/ComplianceDocument models, migration, and initial CRUD routes done, remainder (assignment override flow, expiry-notification scheduler, dashboard) in flight |
| 3 — Accounting/Billing | ⬜ Not started | Own future spec, per roadmap decomposition |
| 4 — External Integrations | ⬜ Not started | |

Backend test suite (`main`): 448 passing tests across 33 test files, including a dedicated tenant-isolation regression suite that gates every merge (per [10-test-strategy.md](docs/10-test-strategy.md) Section 2). CI runs on GitHub Actions ([.github/workflows/backend-ci.yml](.github/workflows/backend-ci.yml)).

**Known gaps versus the documented design** (tracked as follow-up work, not silently dropped):
- **Frontend:** every phase so far has been scoped backend-only — the product frontend (client dashboards, e-signature capture UI, project views) described in the PRD/Technical Architecture has not been built. `frontend/` currently holds a minimal scaffold plus some marketing-site pages; it is not the application UI.
- **MFA (TOTP):** [07-security-compliance.md](docs/07-security-compliance.md) Section 1 calls this a requirement for the Admin role; not yet implemented.
- **Refresh-token rotation:** the doc specifies short-lived JWTs plus a revocable refresh-token flow; the current implementation issues a single flat-expiry JWT with no refresh/revocation mechanism.
- **Auth provider:** plain PyJWT + Argon2id, not the doc's OIDC/Keycloak example.
- **PDF rendering:** xhtml2pdf, substituted for the doc's WeasyPrint (which has no simple Windows dev-environment install path) — same architecture (Jinja2 → HTML → PDF), reduced CSS support, documented in `backend/pyproject.toml`.

## Planned Architecture (Summary)

- **Frontend:** Next.js 16 (App Router), TypeScript
- **Backend:** FastAPI (Python 3.12), decoupled from the frontend, organized as a modular monolith
- **Database:** PostgreSQL with Row-Level Security for multi-tenant isolation, including a nested (parent/child branch) company hierarchy
- **Background jobs:** Celery/Dramatiq + Redis (PDF generation, QuickBooks/FreshBooks sync, notifications)
- **Payments:** Stripe (Builders Stream's own subscription billing)
- **Hosting:** Self-hosted (Docker Compose on the developer's own Proxmox infrastructure)

Full rationale and diagrams are in [03-technical-architecture.md](docs/03-technical-architecture.md).

## MVP Scope

Users & Company Management → CRM → Project Management → Estimation Engine (with e-signature approval) — **complete as of Phase 2.** Accounting/Billing, QuickBooks/FreshBooks integration, and Compliance Tracking are post-MVP; Compliance Tracking is now in progress (see [Build Status](#build-status)). See [09-roadmap-implementation-plan.md](docs/09-roadmap-implementation-plan.md) for the full phase breakdown.

## Open Questions

Tracked in [01-prd.md](docs/01-prd.md#8-open-questions):

- Whether field crews need offline/mobile (PWA) support
- Final dollar pricing for the Starter/Pro/Enterprise tiers
- Target adoption/revenue metrics

## Contributing / Next Steps

Active work: finishing Phase 3 Compliance Tracking (subcontractor/vendor compliance documents, expiry notifications, PM-assignment override flow) on `feature/compliance-tracking`, per [the design spec](docs/superpowers/specs/2026-07-13-compliance-tracking-design.md) and its implementation plan.

Queued follow-up work (see [Build Status](#build-status) for why): MFA (TOTP) for the Admin role, refresh-token rotation with server-side revocation, and scoping/building the real product frontend.
