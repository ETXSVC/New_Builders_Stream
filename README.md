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
| [11-production-deployment.md](docs/11-production-deployment.md) | Production runbook: server `.env`, first deploy, smoke tests, backups, split topology |
| [12-project-review.md](docs/12-project-review.md) | Full-codebase review (2026-07-24): findings, strengths, prioritized follow-ups |
| [13-database-erd.md](docs/13-database-erd.md) | Per-domain ERD generated from the live schema, plus the RLS boundary it can't draw |
| [14-frontend-architecture.md](docs/14-frontend-architecture.md) | Next.js App Router structure, BFF pattern, shared list/loader conventions |

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
| Frontend (all of the above) | ✅ Done | Next.js App Router product UI: CRM, projects, estimation + client e-signature, billing, compliance, integrations, team directory, invitation accept |
| Platform console (cross-tenant admin) | ✅ Done | `/platform/*`: list every tenant, create one (company + owner + trial), rename, change tier/status, three-state per-module entitlement overrides, and take a tenant out of service (soft — the role holds no DELETE anywhere). Separate token scope, separate DB role, TOTP-gated. Operator UI at `/platform` (own login, own httpOnly session cookie, no product nav); operator accounts are still minted by `scripts/grant_platform_admin.py`, because no route can grant that privilege |
| Team directory | ✅ Done | `/team`: each company's own record of its people — names, address, phone numbers, trade and photo — kept per membership rather than on the global `users` row, so the same person working for two builders has two records neither can read the other's. Company-managed profession list; photo bytes served by a role-checked route rather than off the static volume, which is why the UI fetches them with the session's bearer token instead of pointing an `<img>` at a URL. Product UI at `/team` (list + one person's record), reads `admin`/`project_manager`, writes `admin` |
| 5 — Offline/PWA, AI takeoff, multi-currency | ⬜ Not scheduled | Per roadmap |

Backend test suite (`main`): 1,104 passing tests, plus a 34-test Playwright e2e suite, including dedicated tenant-isolation/RLS regression suites (both a `company_id`-index and an RLS-*policy* coverage gate, each catalog-driven so a future tenant table can't slip past either) and a tier-gating completeness gate. Three CI workflows (six jobs) gate every merge: backend ([backend-ci.yml](.github/workflows/backend-ci.yml) — a `deploy-config` job validating every compose file and the backup scripts, a `docker-build` job that builds the backend image *and runs it* to prove it's serviceable, and a `test` job running pytest against real Postgres 16 + Redis 7, ruff, mypy, and an OpenAPI schema-diff), frontend ([frontend-ci.yml](.github/workflows/frontend-ci.yml) — eslint + typechecked build, plus a `docker-build` job that boots the standalone production image against a stand-in backend), and end-to-end ([e2e-ci.yml](.github/workflows/e2e-ci.yml) — the full stack with a Playwright suite driving real browser flows).

**Remaining gaps** (deliberate, tracked):
- **Real external-service clients:** Stripe billing, QuickBooks/FreshBooks sync, and SMTP email all run behind Protocol interfaces with config-selected fake implementations. The wiring, webhooks, tier gating, idempotency, and tests are in place; production use needs real credentials and SDK-backed clients dropped in behind the existing interfaces.
- **Auth provider:** plain PyJWT + Argon2id rather than the doc's OIDC/Keycloak example — a documented, deliberate substitution ([CLAUDE.md](CLAUDE.md)).
- **PDF rendering:** xhtml2pdf, substituted for the doc's WeasyPrint (no simple Windows dev-environment install path) — same Jinja2 → HTML → PDF architecture, reduced CSS support, documented in `backend/pyproject.toml`.
- **Backend package layout:** organized by technical layer (`models/`, `routers/`, `services/`) rather than the doc's domain-bounded packages. The no-cross-module-imports rule is enforced by an AST sweep (`tests/test_module_boundaries.py`), not by review.
- **Alert delivery:** the monitoring stack (Prometheus + Grafana + Alertmanager + node-exporter + cAdvisor, in `docker-compose.prod.yml`) evaluates and groups the alerts [docs/06 §5](docs/06-nonfunctional-requirements.md) requires, but ships with no notifier wired up — Alertmanager cannot read environment variables, so the SMTP details have to be filled in on the box ([runbook §10](docs/11-production-deployment.md)). Backup failure has an independent cron-mail path regardless.
- **mypy:** gates `backend/app` in CI alongside ruff; the test suite itself stays outside the type gate (exercised by pytest instead).
- **Platform console account provisioning:** the console now has a UI at `/platform` and can create *tenants*, but creating an **operator** still needs `backend/scripts/grant_platform_admin.py` — database access and a shell. That is deliberate rather than missing: `platform_admins` revokes writes from both runtime roles, so no route can grant the privilege, and the console cannot enrol its own second factor from the UI either (that is two password-gated API calls). The signed-in surface *is* now covered end to end — `backend/scripts/provision_e2e_operator.py` mints an operator (via that same grant script) and `frontend/e2e/platform-console-authenticated.spec.ts` drives the real UI with real TOTP codes.
- **Hard-deleting a tenant is not in the console, deliberately:** `DELETE /platform/companies/{id}` is a soft delete (`companies.deleted_at`), and the console's database role holds DELETE on nothing. Removing a tenant's rows for real crosses ~40 tables whose `company_id` foreign keys are almost all NO ACTION, and lives in `backend/scripts/prune_dev_tenants.py` behind a shell and the table owner.

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
docker compose up      # Postgres, Redis, migrate, backend :8000, worker, scheduler, frontend :3001
```

A one-shot `migrate` service applies `alembic upgrade head` before the
backend and worker start, so `up` leaves you with a migrated database
rather than an empty one.

Register a company at http://localhost:3001/register — registration creates a pro-tier trial. Backend tests: `cd backend && pip install -e ".[dev]" && pytest` (needs Postgres + Redis per `.env`). E2E: `cd frontend && npm run test:e2e` against a running stack.

### The platform console

Cross-tenant administration at http://localhost:3001/platform — a different
trust tier from the product, with its own login, its own token scope, its own
database role, and no product navigation. From it an operator can:

| Action | Notes |
|---|---|
| List / search every tenant | Cursor-paginated; filter to root companies, or include ones out of service |
| Create a tenant | Company + owner user + 14-day trial in one transaction. The owner's password is generated server-side and shown **once** — it is stored only as a hash and cannot be retrieved afterwards |
| Rename | Works on branches too, unlike entitlements |
| Change tier / status / seats | Setting a status takes it out of Stripe's hands until handed back, so a routine webhook cannot revert an operator's decision |
| Override modules per tenant | Three-state: grant what the tier withholds, withhold what it grants, or defer to the tier |
| Take out of service / restore | **Soft.** Sets `companies.deleted_at`; blocks sign-in for the company and every branch beneath it within one request, not one token lifetime. Reversible, and destroys nothing |

Every change is written to the **target tenant's** own audit log, not to a
separate operator stream — the customer can see that their entitlements
changed, by whom, and from what to what.

Two things the console deliberately cannot do. It cannot **hard-delete** a
tenant: its database role holds `DELETE` on nothing, and removing a tenant's
rows for real crosses ~40 tables of `NO ACTION` foreign keys, which lives in
`backend/scripts/prune_dev_tenants.py` behind a shell and the table owner. And
it cannot **create an operator**, because `platform_admins` revokes writes
from both runtime database roles, so no route can grant that privilege:

```bash
cd backend
python scripts/grant_platform_admin.py grant you@example.com   # the user must already exist
```

Then enrol a second factor (`POST /platform/auth/mfa/enroll` → `/activate`, both password-gated); login refuses any account without one. The console also needs `PLATFORM_DATABASE_URL` set — `.env.example` has it, and leaving it unset disables the console entirely (every `/platform` route 503s) rather than quietly running it on a wider connection.

### Local development notes

Two things about the dev stack that look like bugs and are not:

- **Browsing by hostname rather than `localhost`.** `next dev` serves its own
  internal endpoints (fonts, the HMR socket) only to origins it trusts, which
  out of the box means localhost — so `http://your-box:3001` gives a 403 on
  every font and a WebSocket that reconnects forever while the app works
  fine. Set `DEV_ALLOWED_ORIGINS` in `.env` (comma-separated hostnames;
  `.env.example` documents it). Dev-only: `next build` never reads it.

- **Clearing the dev server's cache.** `.next` lives in a container volume,
  not the bind mount, so `rm -rf frontend/.next` on the host does nothing.
  Use:

  ```bash
  docker compose rm -sfv frontend && docker compose up -d frontend
  ```

  The `-v` is the point. `docker compose down frontend` does **not** remove
  an anonymous volume, so a stale `.next` survives what looks like a clean
  restart.

- **Serving the frontend as it actually ships.** Turbopack intermittently
  fails to register `app/(platform)/api/platform/companies/[companyId]/modules/[module]`
  on a Windows host — it 404s in the dev container while `next build`
  produces it correctly, and the same route sometimes appears after an
  unrelated change and disappears again. To run the built artifact instead:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.frontend-prod.yml up -d frontend
  ```

  All nine platform routes register reliably under that overlay. Use
  **http://localhost:3001** with it, not a hostname: `NODE_ENV=production`
  makes the session cookies `Secure`, and browsers refuse to store those
  over plain HTTP except on localhost — otherwise login appears to succeed
  and bounces straight back. No hot reload in this mode; add `--build`
  after a frontend change, or drop back with a plain
  `docker compose up -d frontend`.

  What this is *not*, each ruled out by experiment rather than reasoning:
  the `[module]` segment name (a probe route with the same name under a
  different parent registered fine), route depth alone (a short
  eight-segment path registered), the parent directory being called
  `modules`, and Windows' 260-character path limit (the longest artifact
  Turbopack writes for this route is ~157 characters). Production is
  unaffected, so this is a dev-server annoyance rather than a defect.

### Production

`docker-compose.prod.yml` is the hardened single-box stack (Caddy TLS termination, internal-only DB/Redis, auto-migrations, restart policies, nightly backups); `deploy/split/` holds three standalone stacks (backend API, middleware worker tier, frontend) for deploying each tier on its own machine with independent lifecycles. The full guide — server `.env` requirements, first-deploy smoke-test checklist, split-topology wiring — is [docs/11-production-deployment.md](docs/11-production-deployment.md).

## Open Questions

Tracked in [01-prd.md](docs/01-prd.md#8-open-questions):

- Whether field crews need offline/mobile (PWA) support
- Final dollar pricing for the Starter/Pro/Enterprise tiers
- Target adoption/revenue metrics

## Contributing / Next Steps

Queued follow-up work, in rough priority order:

1. **Real provider clients** — SDK-backed Stripe, QuickBooks, and FreshBooks implementations behind the existing Protocol interfaces (needs real developer accounts/credentials; SMTP already works with real settings).
2. **Phase 5 exploration** — offline/PWA for field crews, AI blueprint takeoff, multi-currency (unscheduled, per roadmap).
