# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Builders Stream — a multi-tenant SaaS platform for small/medium construction
companies (CRM, project management, cost estimation/e-signature,
accounting/billing, compliance tracking, QuickBooks/FreshBooks integration).
Backend is FastAPI (Python 3.12); frontend is Next.js 16 (App Router,
TypeScript). The full design doc set is in `docs/` — start with
`docs/01-prd.md` (why) and `docs/03-technical-architecture.md` (how).
`CONTRIBUTING.md` covers the mechanics of getting a change in — the CI
gates and what each one is actually protecting, plus two environment
footguns (Python 3.11 fails `pip install` while exiting 0; `caplog` cannot
see app loggers). A third lives in `.gitattributes`: the working tree is
LF on every platform, because `deploy/backup/*.sh` are bind-mounted into
Linux containers and a CRLF shebang fails there as
`/bin/bash^M: bad interpreter` — at 01:30, unattended, on the backup job.
Do not relax that file on a Windows checkout. `docs/adr/` indexes the load-bearing decisions and points
at the code that records each one — read that index before "fixing"
anything that looks odd in auth, tenancy, pagination or deployment.
`docs/14-frontend-architecture.md` covers the frontend, which was
previously undocumented. `docs/12-project-review.md` is a dated audit
trail rather than current-state documentation: useful for *why* something
is the way it is, but its "open findings" are only accurate as of each
dated follow-up section — re-derive any status claim from the code or the
tooling before repeating it.

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

# Run the full test suite (1111 tests; needs Postgres + Redis reachable per .env)
pytest

# Run one file / one test
pytest tests/test_leads.py
pytest tests/test_leads.py::test_create_lead_requires_admin_role -v

# Migrations (Alembic)
alembic upgrade head
alembic revision -m "description"          # hand-write the migration body

# Full stack locally (Postgres, Redis, backend, worker, scheduler, frontend)
# The schema is created for you: a one-shot `migrate` service runs
# `alembic upgrade head` first, and `backend`/`worker` are gated on it
# completing — the same shape docker-compose.prod.yml uses. Before that
# service existed, a fresh `up` gave you six healthy containers and an
# EMPTY database, and the first registration 500'd with nothing pointing
# at the cause.
docker compose up
# Only needed to migrate out of band (e.g. after hand-writing a revision
# while the stack is already running):
docker compose exec backend alembic upgrade head
# Production stack (Caddy TLS, hardened): docker-compose.prod.yml — see
# docs/11-production-deployment.md; the dev compose above is unchanged
```

**`localhost` vs `postgres` in the DB URLs is deliberate, and per-process.**
`.env` points `MIGRATIONS_DATABASE_URL`/`TEST_DATABASE_URL` at
`localhost:5432` because Alembic and pytest normally run on the HOST,
against the port the dev compose publishes. Inside a container `localhost`
is that container, so `docker-compose.yml` overrides those variables
per-service to the compose-network hostname — `backend` (for the alembic
command above) and `worker` (for the daily cross-tenant sweeps). Adding a
service that runs Alembic or reads `scanner`'s URL means adding the same
override; forgetting it produces `Connect call failed ('127.0.0.1', 5432)`,
which has now happened twice.

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

Health endpoints are deliberately split: `/health` is static liveness (is
the process serving at all — what a restart policy should act on) and
`/ready` probes Postgres + Redis with per-dependency status (what a load
balancer or compose healthcheck should gate on). Conflating them would
make a database outage restart-loop the backend for no benefit.

`/metrics` is the third operational endpoint (Prometheus exposition,
`app/core/metrics.py`, excluded from the OpenAPI schema). Two invariants
there are enforced by `tests/test_metrics.py` rather than left to review:
**no series is ever labelled with a tenant** (cardinality, plus Grafana is
a different trust boundary from the API), and **requests are labelled with
the matched route template, never the raw path** — with everything
unmatched collapsed into one `<unmatched>` bucket so a 404 scan cannot
grow Prometheus's memory from outside. `tests/test_monitoring_config.py`
additionally checks every metric name in `deploy/prometheus/alerts.yml`
and the Grafana dashboard against the app's live registry, because an
alert naming a metric that does not exist never fires and never errors.
The stack itself (Prometheus, Grafana, Alertmanager, node-exporter,
cAdvisor) lives in `docker-compose.prod.yml`, bound to loopback —
`docs/11-production-deployment.md` §10.

Error reporting is Sentry, **off unless `SENTRY_DSN` is set** —
`app/core/observability.py` returns immediately and `sentry-sdk` is never
imported, which is why it is an optional extra (`.[observability]`) rather
than a dependency. One variable turns it on for all three processes.
Two things there are deliberate and easy to undo by accident:

- **Everything outbound is scrubbed** — `_scrub` strips every secret in
  `_SECRET_NAMES` plus the `authorization`/`cookie`/`x-tenant-id` headers,
  and `send_default_pii=False` is set explicitly rather than relied on.
  The frontend has its own copy of this (`frontend/sentry.shared.ts`)
  because query strings carry credentials there: `SENSITIVE_QUERY_KEYS`
  includes a bare `id` precisely because the invitation-accept page reads
  its one-time credential from `?id=`, which a scrubber written from
  memory misses.
- **Sentry events ARE tagged with `company_id`** (`tag_current_tenant`,
  called from `get_current_user` only after membership is verified — never
  from the attacker-controlled header). That is the opposite of the rule
  for metrics above, and the difference is the point: an error event is one
  bounded record a human reads during an incident, not an unbounded time
  series exported to a dashboard.

CI: `.github/workflows/backend-ci.yml` runs a `deploy-config` job
(validates every compose file + parses the backup scripts), a
`docker-build` job, and a `test` job running `ruff check .`, `mypy` (scoped
to `app/` via pyproject's `[tool.mypy]` — tests stay outside the type
gate), an OpenAPI schema-diff against the committed `backend/openapi.json`
snapshot, and `pytest -v` against real Postgres 16 + Redis 7 service
containers (not mocks/SQLite) — the tenant-isolation and RLS regression
tests require a real Postgres. `frontend-ci.yml` (eslint + typechecked
build, plus its own `docker-build` job) and `e2e-ci.yml` (full stack +
Playwright) run alongside. All gate every merge to `main`.

The two `docker-build` jobs **run** the images, not just build them, and
that distinction is load-bearing: the production image installs the backend
non-editably (`pip install .`), while every other job — and every local
run — uses an editable install that imports from the source tree. A file
that setuptools doesn't package therefore exists everywhere except in the
artifact that ships. That is not theoretical; it is why
`backend/pyproject.toml` carries a `[tool.setuptools.package-data]` entry
for `app/templates/*.jinja` and why the backend smoke step loads that
template out of the *installed* package. **Adding any non-`.py` file that
`app/` reads at runtime means adding it to `package-data`.** The frontend
job's equivalent is booting the standalone image beside a stand-in backend
and asserting the BFF actually dials `NEXT_PUBLIC_API_URL` — proving that
value is read at run time rather than inlined into the bundle at build
time.

## Architecture

### Modular monolith, one FastAPI app

`backend/app/main.py` wires together one router per bounded module (`auth`,
`leads`, `projects`, `estimates`, `compliance`, `invoices`/`bills`/`expenses`,
`integrations`, `subscriptions`, `platform_auth`/`platform_tenants`, ...).
Router include order occasionally
matters: `branding.router` is registered before `companies.router` because
Starlette matches routes by registration order, and `companies.router`'s
generic `/companies/{company_id}` would otherwise shadow `branding.router`'s
literal `/companies/branding` paths.

Modules should only reach another module's data through its service layer
(`app/services/`), never by querying another module's tables directly.
`tests/test_module_boundaries.py` makes this a gate rather than a
convention: no `app/routers/*.py` may import another router (AST sweep,
absolute and relative forms), and no router outside `projects.py` may
hand-roll a `select(Project).where(Project.id == ...)` lookup instead of
calling `app.services.project_lookup.get_project_or_404` — which is where
field_crew's assigned-only scope and the `client` role's row scope live,
so an open-coded lookup silently drops both.

### Multi-tenancy: PostgreSQL RLS is the enforcement boundary, not app code

This is the most important thing to get right when touching auth, routers,
or migrations:

- `companies.parent_id` self-references, forming a tenant hierarchy — a
  parent branch's users can see their descendants' data. It is **immutable**
  (migration 0021's `companies_parent_id_immutable` trigger): re-parenting
  moves a subtree between tenants and detaches it from its subscription, so
  it is a migration that disables the trigger, never a write.
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
  reach for an owner-role connection in application code —
  `tests/test_worker_db_roles.py` enforces this with an AST sweep, so it is
  a gate rather than a convention. The three genuinely cross-tenant daily
  sweeps use the `scanner` role instead (migration 0020): BYPASSRLS, DML
  grants, owning nothing, so it can read every tenant but cannot alter a
  policy or drop a table. Single-tenant jobs resolve their tenant through a
  narrow SECURITY DEFINER lookup and run under `app_user` with
  `set_current_tenant` (see `app/tasks/accounting_sync.py`).
- **RLS is company-scoped, which says nothing about two clients of the SAME
  company.** The `client` role is additionally scoped by row: a client sees
  only projects/leads they hold a `project_clients`/`lead_clients`
  membership for (migration 0019). The rule lives in
  `app/services/client_scope.py` and is applied at the by-id chokepoints
  (`_get_estimate_or_404`, `_get_change_order_or_404`,
  `_get_invoice_or_404`, and `app.services.project_lookup.get_project_or_404`
  — the last one moved out of `projects.py` when six routers turned out to
  be importing it) so `approve`/`reject`
  inherit it by construction. It raises 404, never 403, so a client cannot
  enumerate another client's document ids. A new client-facing surface must
  go through one of those helpers or apply `client_scope` itself.
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
  Since migration 0023 a per-tenant **override** takes precedence over the
  tier — see the platform console below. `_module_allowed` is the single
  place that resolves this, so `require_module`, the OAuth callback and the
  event handlers cannot drift apart on what a tenant may use.

All three depend on `get_current_user`, which FastAPI caches per-request by
callable+params, so stacking them doesn't cost extra JWT decodes or DB
round trips.

### The platform console: a trust tier above every tenant

`/platform/*` (migration 0023) is cross-tenant administration — changing a
customer's plan, status or module entitlements without a psql session. It is
deliberately built *beside* the product's auth path rather than inside it,
and the separation is the design:

- **A different dependency.** `get_platform_admin` (`app/core/platform_deps.py`),
  not `get_current_user`. Adding cross-tenant administration required no
  change to the most security-critical function in the codebase beyond a
  two-line scope check that *narrows* what it accepts.
- **A different token.** `create_platform_token` mints `scope: "platform"`
  and carries **no** `default_company_id`, so it cannot name a tenant even if
  the scope check were bypassed. `get_current_user` reads `scope` as an
  allow-list (defaulting to `tenant`), so a scope added later fails closed
  there rather than silently inheriting tenant access.
- **A different database role.** `platform_admin` (BYPASSRLS, owning
  nothing) — SELECT everywhere, DML on `company_module_overrides`, UPDATE on
  `subscriptions`, INSERT on `audit_log`, nothing else. `PLATFORM_DATABASE_URL`
  has **no fallback**: unset disables the console (every route 503s) rather
  than quietly running it on a wider connection.
- **Privilege is re-checked every request**, not baked into the token, so
  revoking takes effect within one request rather than one token lifetime.
  That check reads through the ordinary RLS-scoped `app_user` connection
  under a `self_read` policy — a caller can ask "am *I* an admin?" without
  being able to enumerate who else is.
- **Asked for with `scope="function"`, like `get_current_user`.** Both hold
  a transaction open across the handler and commit in their exit code, and a
  generator dependency defaults to FastAPI's *request* exit stack — which
  closes after the response has been sent, handing the client a 200 for a
  write that has not committed. The console shipped without it and the
  symptom was a tier change that read back as its old value, intermittently.
  `tests/test_dependency_exit_scope.py` now discovers every
  transaction-holding dependency and fails on any call site that omits the
  scope, so this is a gate rather than a docstring.
- **No route grants the privilege.** `platform_admins` revokes writes from
  both `app_user` and `scanner`, so escalation into this tier is removed as a
  category rather than defended against. Minting an account is
  `backend/scripts/grant_platform_admin.py` (table owner, so: database access
  and a shell), and the account still cannot sign in until it enrols TOTP.

Two behaviours worth knowing before touching this area: overrides are
**three-state** (`true` grants what the tier withholds, `false` withholds
what it grants, no row defers to the tier — collapsing the first two makes
"off" unexpressible), and `subscriptions.manual_status_override` exists
because `POST /webhooks/stripe` is otherwise last-write-wins on `status` —
without it the next routine `customer.subscription.updated` silently reverts
an operator's change. Entitlement changes are audited into the **target
tenant's** `audit_log`, not a separate platform log.

**Tenant lifecycle (migration 0024)** added create, rename and
take-out-of-service to that surface, and the grant model is still the point:
`platform_admin` gained INSERT on `companies`/`users`/`company_users`/
`subscriptions` and UPDATE on `companies`, and **holds DELETE on nothing**
(bar clearing a module override, which predates it and is how the third
state is expressed). So `DELETE /platform/companies/{id}` is a SOFT delete
setting `companies.deleted_at` — destroying a tenant for real means ~40
tables of NO ACTION foreign keys and stays in
`scripts/prune_dev_tenants.py`, behind a shell and the table owner.
`tests/test_platform_admin.py` asserts both halves at the catalog level
(which tables are writable, and that DELETE is held nowhere) rather than
trusting this paragraph.

Soft delete is enforced by `is_company_live(uuid)` — a **SECURITY DEFINER**
function, called from `get_current_user` at the same chokepoint as
membership, so a token already issued stops working within one request. It
is SECURITY DEFINER because it walks **ancestors**: `companies`' RLS policy
scopes rows to `get_all_descendant_ids(app.current_tenant)`, which contains
a company's descendants and never its ancestors, so a branch asking "has my
parent been retired?" would read zero rows and answer "no".

`companies.is_active` is **generated from `deleted_at`** since migration
0025 (`GENERATED ALWAYS AS (deleted_at IS NULL) STORED`) — it had existed
since 0001 reading nothing, 0024 had the routes write both together, and
0025 removed the possibility of them disagreeing rather than maintaining
the discipline. No application code can write it; assign to `deleted_at`.
Two consequences are easy to undo by accident, both commented where they
live:

- `Company` sets `__mapper_args__ = {"eager_defaults": False}`. Without it
  SQLAlchemy reads generated columns back with `INSERT ... RETURNING`, and
  `POST /auth/register` inserts the root company *before* any tenant
  context exists — so that RETURNING is evaluated under `tenant_select`
  with `app.current_tenant` unset, and registration fails outright with
  "new row violates row-level security policy". This broke 28 tests when
  the column was first converted.
- The column is deliberately **not** `deferred`. A deferred attribute
  lazy-loads on access, and `CompanyResponse.model_validate(company)`
  reaches for it from synchronous Pydantic code, which asyncpg cannot serve
  (`MissingGreenlet`, not a slow query). **Any route that writes a `Company`
  and then returns a `CompanyResponse` must call
  `session.refresh(company, ["is_active"])` after its flush** — the two that
  do today are `POST /companies/{id}/children` and
  `PATCH /companies/{company_id}`. Missing it fails loudly in tests rather
  than silently, which is how both were found.

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
`EXPENSE_CREATED` / `BILL_CREATED` → enqueue accounting-integration syncs,
`PROJECT_COMPLETED` → drafts a final invoice for the uninvoiced remainder.

**Enqueue background work AFTER the commit, never inside the handler.**
The bus runs inside the request transaction; Redis does not roll back with
it. A handler that calls `.send()` directly queues a job naming a row that
a later rollback means will never exist — and the worker then burns all
three retries on it. `app/core/after_commit.py`'s `enqueue_after_commit`
parks the call on the session and `get_current_user` fires it immediately
after `session.commit()`, inside the same `try`, so a rollback path can
never reach it. `financial_record_sync_handler.py` is the current caller
and the pattern to copy.

Dramatiq actors are registered ONLY from the modules named on the worker's
command line, so a new actor must be added to all three compose files
(`docker-compose.yml`, `docker-compose.prod.yml`,
`deploy/split/middleware.compose.yml`) and `e2e-ci.yml` — miss one and that
topology silently dead-letters those messages with no error and no log.
`deploy-config` asserts the three compose lists agree.

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
enough under bulk inserts or same-tick requests. The cursor key must be
**immutable**: paginating on `updated_at` lets a concurrently-edited row
move to the end of the ordering and be returned twice, which is the same
instability offset paging was rejected for.

Optimistic concurrency is opt-in via an `expected_updated_at` field **in
the request body**, not an `If-Match` header (`app/services/concurrency.py`
— `guard_stale_write`). The header form would be more RESTful and does not
work here: the Next BFF forwards a fixed allowlist (`Content-Type`,
`Authorization`, `X-Tenant-ID`, `X-Forwarded-For`), so a custom header
would be silently dropped on the hop and every stale write would sail
through.

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
UI), `app/(marketing)/` (public pages), and `app/(platform)/` (the operator
console at `/platform`). The console is a **different trust tier and a
different session**: `POST /platform/auth/login` returns no refresh token, so
`lib/platform/session.ts` keeps the platform token itself in an httpOnly
`sameSite=strict` cookie whose lifetime comes from the response's
`expires_in_minutes` — it never reaches JavaScript, and one `middleware.ts`
gates both trees (selecting which cookie by path, with `/platform/login`
exempt). It has no `AuthProvider`/`AppShell` on purpose: the product nav would
offer links a platform token cannot open. `docs/14-frontend-architecture.md`
§2.1 has the rest. TypeScript API types in
`lib/api/types.ts` are generated from the committed `backend/openapi.json`
snapshot via `npm run generate:api-types` — never hand-edit either file;
after a backend route/schema change, regenerate the snapshot
(`backend/scripts/export_openapi.py`) and then the types (CI's schema-diff
gate fails if the snapshot drifts from the code). `marketing-site/` (static HTML/CSS/JS) and `marketing/` (copy docs)
are a separate, pre-existing marketing site, unrelated to the Next.js app.

Security headers live in `next.config.ts` and apply in dev too, with one
deliberate exception: **`script-src` gains `'unsafe-eval'` when
`NODE_ENV === "development"` and must never carry it in production.**
React's development build and Turbopack's HMR runtime both call `eval()`
— without the exception, `next dev` throws "eval() is not supported in
this environment" on every page, which is exactly what an unconditional
CSP produced. React never uses `eval()` in production, so the relaxation
buys nothing there and costs the policy its point.
`e2e/security-headers.spec.ts` asserts every header and, under CI
specifically, that `'unsafe-eval'` is **absent** — `e2e-ci` builds and
serves a production frontend, so that assertion runs against the real
artifact. HSTS is deliberately NOT here: it belongs at Caddy
(`deploy/Caddyfile`), being meaningless without TLS and harmful over dev
HTTP. A nonce-based strict CSP that would remove `'unsafe-inline'`
entirely is a tracked follow-up, not a blocker.

`lib/use-cursor-list.ts` is the shared loader for cursor-paginated lists,
and the one to reach for in new code. It carries the stale-response guard
— a generation ref checked **above** `if (!response.ok)`, so a superseded
request can write neither data nor an error — which one of eight
copy-pasted loaders had silently been missing.

**Six surfaces use it today** (leads, estimates, subcontractors, and the
three billing panels); roughly seventeen others still hand-roll the fetch,
so do not assume a list page you are editing is on the hook — check.
It is split into `useCursorListCore` (the loader) and `useCursorList` (the
core plus AuthContext's bearer token) because the platform console cannot
call the wrapper: `useAuth()` throws without a provider, and the console has
none. Reach for the core, not a copy, if you ever need it outside `(app)`.
`integrations/page.tsx` is a deliberate permanent exception: different
response envelope, and a 404 there means "not connected", not an error.

## Tests

Backend tests are organized per-router/service, plus dedicated regression
suites that matter architecturally:
- `test_*_tenant_isolation.py` files (billing, invoicing/AP, integrations) —
  assert cross-tenant access is actually denied by RLS, not just "your own
  company's query looks right."
- `test_tier_gating.py` — introspects routes to assert every mutating route
  in a gated module carries `require_module`'s *correct* module tag
  (`dependency.tier_module`), not just that some gate is present.
- `test_rls_policy_coverage.py` + `test_company_id_index_coverage.py` — a
  pair of catalog-driven sweeps over *every* table Postgres reports, so a
  future tenant table is covered without anyone remembering to add it. The
  policy sweep asserts RLS is enabled, that the `FOR ALL` policy's USING
  **and** check expressions really call `get_all_descendant_ids` /
  `get_root_company_id` (a `USING (true)` policy would satisfy a
  "has a policy" check and leak everything), that any extra permissive
  policy is on a reviewed allowlist, and that a table with no `company_id`
  column is either RLS-protected or explicitly declared non-tenant. Adding
  a table means adding a policy or an allowlist entry — there is no third
  option that passes.

- `test_client_role_isolation.py` — two clients of the SAME company, which
  is the case company-scoped RLS says nothing about. A two-company version
  of these tests passes against the vulnerable code, so the shared company
  is the point.
- `test_worker_db_roles.py` — no `app/` module may read
  `migrations_database_url` (AST sweep), `scanner` holds BYPASSRLS and
  nothing else, and `companies.parent_id` cannot change.
- `test_platform_admin.py` — the two-way boundary between the product API
  and the platform console, asserted from both sides: a product token
  cannot reach `/platform` and a platform token cannot reach the product
  API, and being a platform admin does **not** elevate that user's ordinary
  token. It also asserts the grant model at the database level — that no
  runtime role can write `platform_admins` or `company_module_overrides` —
  which is the part a route-level test would miss entirely.
- `test_migration_downgrade.py` — walks the migration chain to `base` and
  back to `head` on a scratch database. The re-upgrade is the half that
  matters: it proves a downgrade actually removed things rather than merely
  not raising.
- `test_module_boundaries.py`, `test_metrics.py`, `test_monitoring_config.py`
  — described in their sections above. All three share a shape worth
  copying: they assert against something the code *generates* (the AST, the
  live Prometheus registry, the compose file) rather than against a
  transcribed list, and each carries a non-vacuity floor, because every one
  of these sweeps passes trivially over an empty set.

Shared setup lives in `tests/conftest.py` — `register_and_login`,
`set_subscription_tier`, `grant_client_access`. `register_and_login` in
particular replaced 24 divergent per-file copies; prefer a two-line
wrapper in your own file over adding a parameter to it.

When adding a new tenant-owned table or a new mutating route in a
tier-gated module, add/extend the corresponding isolation or gating test,
following the existing pattern in that test file rather than inventing a
new style.
