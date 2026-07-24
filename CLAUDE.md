# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Builders Stream — a multi-tenant SaaS platform for small/medium construction
companies (CRM, project management, cost estimation/e-signature,
accounting/billing, compliance tracking, QuickBooks/FreshBooks integration).
Backend is FastAPI (Python 3.12); frontend is Next.js 16 (App Router,
TypeScript). The full design doc set is in `docs/` — start with
`docs/01-prd.md` (why) and `docs/03-technical-architecture.md` (how).
`docs/superpowers/plans/` and `docs/superpowers/specs/` hold the
per-feature implementation plans and design specs actually built against;
when a router/model/migration's docstring references one of these, read it
for the "why" before changing that area.

`README.md`'s "Build Status" table was refreshed 2026-07-24 to match the
code, but READMEs drift — when in doubt, trust the code and its migrations
over any prose status table.

## Commands

All backend commands run from `backend/`.

```bash
# Install (editable, with dev/test deps)
pip install -e ".[dev]"

# Run the full test suite (765+ tests; needs Postgres + Redis reachable per .env)
pytest

# Run one file / one test
pytest tests/test_leads.py
pytest tests/test_leads.py::test_create_lead_requires_admin_role -v

# Migrations (Alembic)
alembic upgrade head
alembic revision -m "description"          # hand-write the migration body

# Full stack locally (Postgres, Redis, backend, worker, scheduler, frontend)
docker compose up
```

Test setup notes (see `backend/tests/conftest.py` for the full rationale):
- The session-scoped `_setup_test_database` fixture drops/recreates a
  `builders_stream_test` database and runs Alembic migrations to `head`
  before any test runs — there is no separate "prepare test DB" step to run
  yourself.
- `conftest.py` sets `DATABASE_URL`/`STORAGE_ROOT`/etc. env vars at *module
  import time*, before `app.config`'s `Settings()` singleton is first built
  — required because pytest imports `conftest.py` before any fixture runs.
- Tests connect as `app_user` (the RLS-restricted runtime role); a few
  fixtures (`db_session`, `_clean_tables`) connect as the Postgres owner
  role instead, deliberately bypassing RLS, when a test needs to see rows
  across multiple tenants or truncate tables for cleanup.

Frontend commands run from `frontend/`:

```bash
npm run dev             # next dev
npm run build
npm run lint             # eslint .
npm run test:e2e         # playwright test
npm run generate:api-types  # regenerate lib/api/types.ts from the committed backend/openapi.json snapshot — never hand-edit that file
```

CI: `.github/workflows/backend-ci.yml` runs `ruff check .`, `mypy` (scoped
to `app/` via pyproject's `[tool.mypy]` — tests stay outside the type
gate), an OpenAPI schema-diff against the committed `backend/openapi.json`
snapshot, and `pytest -v` against real Postgres 16 + Redis 7 service
containers (not mocks/SQLite) — the tenant-isolation and RLS regression
tests require a real Postgres. `frontend-ci.yml` (eslint + typechecked
build) and `e2e-ci.yml` (full stack + Playwright) run alongside. All gate
every merge to `main`.

## Architecture

### Modular monolith, one FastAPI app

`backend/app/main.py` wires together one router per bounded module (`auth`,
`leads`, `projects`, `estimates`, `compliance`, `invoices`/`bills`/`expenses`,
`integrations`, `subscriptions`, ...). Router include order occasionally
matters: `branding.router` is registered before `companies.router` because
Starlette matches routes by registration order, and `companies.router`'s
generic `/companies/{company_id}` would otherwise shadow `branding.router`'s
literal `/companies/branding` paths.

Modules should only reach another module's data through its service layer
(`app/services/`), never by querying another module's tables directly — a
convention enforced by review, not tooling.

### Multi-tenancy: PostgreSQL RLS is the enforcement boundary, not app code

This is the most important thing to get right when touching auth, routers,
or migrations:

- `companies.parent_id` self-references, forming a tenant hierarchy — a
  parent branch's users can see their descendants' data.
- Every tenant table has an RLS policy scoped by
  `get_all_descendant_ids(current_setting('app.current_tenant')::uuid)` /
  `get_root_company_id(...)` (Postgres functions defined in migrations).
- `TenantMiddleware` (`app/core/middleware.py`) only extracts the bearer
  token and `X-Tenant-ID` header into contextvars (`app/core/context.py`)
  — it does **not** verify anything, because it has no DB access.
- `get_current_user` (`app/core/deps.py`) is where the claimed tenant is
  actually verified: it opens a transaction, calls `set_current_user`, looks
  up `CompanyUser` membership for the claimed `company_id` (this is what
  stops a spoofed `X-Tenant-ID` from granting cross-tenant access), *then*
  calls `set_current_tenant`, and yields `CurrentUser` **without committing**.
  The commit happens after the route handler returns, because
  `set_config(..., is_local=true)` (used instead of `SET LOCAL` because that
  syntax can't take a bound parameter) is transaction-scoped — committing
  early would drop the tenant context before the route handler's own
  queries ran under it.
- The runtime DB connection uses a restricted `app_user` Postgres role;
  table owners (used in some test fixtures) bypass RLS entirely, so never
  reach for an owner-role connection in application code.
- Any new tenant-owned table needs its own RLS policy in the same migration
  that creates it — there's no global catch-all policy.

### Per-route authorization stack

Write routes commonly compose three independent, orthogonal dependencies:

- `require_role(*roles)` (`app/core/deps.py`) — who within the company may act.
- `block_if_read_only` (`app/core/deps.py`) — is the caller's root company's
  subscription in good standing (blocks writes if not `trialing`/`active`).
- `require_module(module_name)` (`app/core/tier_gating.py`) — does the root
  company's subscription tier (`starter`/`pro`/`enterprise`, ranked in
  `TIER_RANK`) meet or exceed `MODULE_MIN_TIER` for this module. Deliberately
  attached only to mutating routes (reads stay open across tiers), including
  GET-based OAuth callback routes in `integrations` — there's no
  method-sniffing shortcut, since the routes needing the gate most are GETs.
  A missing subscription row fails **open** in both `block_if_read_only` and
  `tier_allows` (treated as an unreachable state, not defended against).

All three depend on `get_current_user`, which FastAPI caches per-request by
callable+params, so stacking them doesn't cost extra JWT decodes or DB
round trips.

### Cross-module communication: in-process synchronous event bus

`app/core/events.py` is a minimal `register`/`publish`/`clear` dispatcher —
deliberately in-process and synchronous (awaited in sequence, in the same
request transaction), not Redis-backed. A handler's exception propagates
(not swallowed) so it can roll back the enclosing transaction. Real handlers
are wired once at process start via `register_event_handlers()`
(`app/core/event_handlers.py`), called from `app/main.py` at import time —
tests instead call `register_event_handlers()` themselves per-test, since
the autouse `_clean_event_registry` fixture clears the registry before/after
every test. Current/planned events: `LEAD_WON` → drafts a Project,
`ESTIMATE_APPROVED` → drafts a deposit invoice, `INVOICE_CREATED` /
`EXPENSE_CREATED` / `BILL_CREATED` → enqueue accounting-integration syncs.

### Layering within a module

`app/routers/*.py` (HTTP/validation) → `app/services/*.py` (business logic,
reusable across routers/event handlers) → `app/models/*.py` (SQLAlchemy ORM,
one file per table, composing `UUIDPKMixin`/`TimestampMixin`/`UpdatedAtMixin`
from `app/models/base.py`). Background jobs live in `app/tasks/` (Dramatiq,
Redis-backed — e.g. PDF export, compliance-expiry checks fired daily by
`app/scheduler.py` via APScheduler). Money columns are `Numeric(12,2)`,
quantized with the shared `CENTS = Decimal("0.01")` constant
(`app/core/money.py`) using `ROUND_HALF_UP` to match Postgres `NUMERIC`
rounding. List endpoints use opaque, base64-encoded cursor pagination
(`app/core/pagination.py`) over `(created_at, id)`, not offset pagination —
offset scans get slower as tables grow and are unstable under concurrent
inserts; `id` is the tiebreaker because `created_at` alone isn't unique
enough under bulk inserts or same-tick requests.

### Documented, deliberate substitutions vs. the design docs

A few implementation choices intentionally diverge from `docs/`, each
documented inline where introduced — check `backend/pyproject.toml`'s
comments and the relevant router/service docstring before "fixing" these
back to match the docs:

- **PDF export:** `xhtml2pdf`, not WeasyPrint (WeasyPrint's native
  GTK3/Pango/Cairo dependency has no simple Windows dev-install path).
  Same Jinja2 → HTML → PDF architecture; reduced CSS support is an accepted
  tradeoff.
- **Auth:** plain PyJWT + Argon2id, not an OIDC/Keycloak provider.
- **Background jobs:** Dramatiq, not Celery.

### Frontend

Next.js App Router with route groups: `app/(app)/` (authenticated product
UI) and `app/(marketing)/` (public pages). TypeScript API types in
`lib/api/types.ts` are generated from the committed `backend/openapi.json`
snapshot via `npm run generate:api-types` — never hand-edit either file;
after a backend route/schema change, regenerate the snapshot
(`backend/scripts/export_openapi.py`) and then the types (CI's schema-diff
gate fails if the snapshot drifts from the code). `marketing-site/` (static HTML/CSS/JS) and `marketing/` (copy docs)
are a separate, pre-existing marketing site, unrelated to the Next.js app.

## Tests

Backend tests are organized per-router/service, plus dedicated regression
suites that matter architecturally:
- `test_*_tenant_isolation.py` files (billing, invoicing/AP, integrations) —
  assert cross-tenant access is actually denied by RLS, not just "your own
  company's query looks right."
- `test_tier_gating.py` — introspects routes to assert every mutating route
  in a gated module carries `require_module`'s *correct* module tag
  (`dependency.tier_module`), not just that some gate is present.

When adding a new tenant-owned table or a new mutating route in a
tier-gated module, add/extend the corresponding isolation or gating test,
following the existing pattern in that test file rather than inventing a
new style.
