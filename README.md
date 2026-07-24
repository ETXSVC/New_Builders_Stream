# Builders Stream

A multi-tenant SaaS platform for small/medium building construction and renovation companies, unifying CRM, project management, cost estimation, accounting/billing, compliance tracking, and company administration into a single system.

**Status:** Feature-complete against the consolidated requirements (Phases 0–4 of the roadmap), full stack — FastAPI backend plus a Next.js product frontend. The remaining gap between "feature-complete" and "production" is external-service credentials: Stripe, QuickBooks/FreshBooks, and SMTP all run against config-selected fake clients until real keys are supplied. See [Build Status](#build-status).

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

Start with the [PRD](docs/01-prd.md) for the "why," then [Technical Architecture](docs/03-technical-architecture.md) for the "how." For working in the code, [`CLAUDE.md`](CLAUDE.md) is the maintained architecture/commands reference. Per-feature design specs and implementation plans live under [`docs/superpowers/`](docs/superpowers/).

## Build Status

Implemented against the [roadmap](docs/09-roadmap-implementation-plan.md), on `main`:

| Phase | Status | Notes |
|---|---|---|
| 0 — Foundation | ✅ Done | Multi-tenant schema, RLS, auth (JWT + Argon2id, refresh-token rotation, MFA/TOTP), audit log, CI |
| 1 — CRM & Project Management | ✅ Done | Leads, Projects, Phases/Tasks (full CRUD), Documents, Daily Logs, invitations (with email delivery) |
| 2 — Estimation Engine + E-Signature | ✅ Done | Cost catalog, markup profiles, estimate calculation, PDF export, e-signature, Change Orders |
| 3 — Compliance Tracking | ✅ Done | Subcontractor registry, compliance documents, expiry dashboard/notifications, assignment override flow, daily expiry scheduler |
| 3 — Accounting/Billing | ✅ Done | Invoices/payments, bills, expenses, auto-drafted deposit + final invoices (event-driven), Stripe subscription lifecycle behind a fake client |
| 4 — External Integrations | ✅ Done | QuickBooks/FreshBooks OAuth connect + idempotent sync behind fake clients |
| Frontend (all of the above) | ✅ Done | Next.js App Router product UI: CRM, projects, estimation + client e-signature, billing, compliance, integrations, invitation accept |
| 5 — Offline/PWA, AI takeoff, multi-currency | ⬜ Not scheduled | Per roadmap |

Backend test suite (`main`): 880+ passing tests, including dedicated tenant-isolation/RLS regression suites and a tier-gating completeness gate. Three CI workflows gate every merge: backend ([backend-ci.yml](.github/workflows/backend-ci.yml) — pytest against real Postgres 16 + Redis 7, ruff lint, OpenAPI schema-diff), frontend ([frontend-ci.yml](.github/workflows/frontend-ci.yml) — eslint + typechecked build), and end-to-end ([e2e-ci.yml](.github/workflows/e2e-ci.yml) — the full stack with a Playwright suite driving real browser flows).

**Remaining gaps** (deliberate, tracked):
- **Real external-service clients:** Stripe billing, QuickBooks/FreshBooks sync, and SMTP email all run behind Protocol interfaces with config-selected fake implementations. The wiring, webhooks, tier gating, idempotency, and tests are in place; production use needs real credentials and SDK-backed clients dropped in behind the existing interfaces.
- **Auth provider:** plain PyJWT + Argon2id rather than the doc's OIDC/Keycloak example — a documented, deliberate substitution ([CLAUDE.md](CLAUDE.md)).
- **PDF rendering:** xhtml2pdf, substituted for the doc's WeasyPrint (no simple Windows dev-environment install path) — same Jinja2 → HTML → PDF architecture, reduced CSS support, documented in `backend/pyproject.toml`.
- **Backend package layout:** organized by technical layer (`models/`, `routers/`, `services/`) rather than the doc's domain-bounded packages; the no-cross-module-table-queries rule is enforced by review, not tooling.
- **mypy:** gates `backend/app` in CI alongside ruff; the test suite itself stays outside the type gate (exercised by pytest instead).

## Architecture (Summary)

- **Frontend:** Next.js 16 (App Router), TypeScript — backend-for-frontend pattern; all backend calls go through Route Handlers
- **Backend:** FastAPI (Python 3.12), modular monolith with an in-process event bus
- **Database:** PostgreSQL with Row-Level Security as the tenant-isolation enforcement boundary, including a nested (parent/child branch) company hierarchy
- **Background jobs:** Dramatiq + Redis (PDF generation, accounting sync, compliance-expiry checks, invitation email, seat-usage reporting) with an APScheduler-driven daily scheduler
- **Payments:** Stripe (Builders Stream's own subscription billing; webhook-driven lifecycle)
- **Hosting:** Self-hosted (Docker Compose on the developer's own Proxmox infrastructure)

Full rationale and diagrams are in [03-technical-architecture.md](docs/03-technical-architecture.md); the maintained day-to-day reference is [`CLAUDE.md`](CLAUDE.md).

## Running It

```bash
cp .env.example .env   # then set INTEGRATION_TOKEN_ENCRYPTION_KEY (see comments)
docker compose up      # Postgres, Redis, backend :8000, worker, scheduler, frontend :3001
docker compose exec backend alembic upgrade head
```

Register a company at http://localhost:3001/register — registration creates a pro-tier trial. Backend tests: `cd backend && pip install -e ".[dev]" && pytest` (needs Postgres + Redis per `.env`). E2E: `cd frontend && npm run test:e2e` against a running stack.

## Open Questions

Tracked in [01-prd.md](docs/01-prd.md#8-open-questions):

- Whether field crews need offline/mobile (PWA) support
- Final dollar pricing for the Starter/Pro/Enterprise tiers
- Target adoption/revenue metrics

## Contributing / Next Steps

Queued follow-up work, in rough priority order:

1. **Real provider clients** — SDK-backed Stripe, QuickBooks, and FreshBooks implementations behind the existing Protocol interfaces (needs real developer accounts/credentials; SMTP already works with real settings).
2. **Phase 5 exploration** — offline/PWA for field crews, AI blueprint takeoff, multi-currency (unscheduled, per roadmap).
