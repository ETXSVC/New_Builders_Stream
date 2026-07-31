# Contributing

This file records the conventions that are already enforced somewhere —
by a test, a CI job, or a migration — so that finding them does not
require reading the enforcement first. Where a rule has a gate, the gate
is named. A rule with no gate is a preference, and is marked as one.

`CLAUDE.md` is the orientation document: what the system is, how it is
laid out, and why the load-bearing decisions are what they are. Read that
first. This file is narrower — it is about the mechanics of getting a
change in.

## Running things

Backend, from `backend/`:

```bash
pip install -e ".[dev]"     # needs Python 3.12; 3.11 fails at install
pytest                      # needs Postgres + Redis reachable
ruff check .                # covers tests/ too, not just app/
mypy                        # scoped to app/ by pyproject
alembic upgrade head
```

Frontend, from `frontend/`:

```bash
npm ci
npm run lint
npm run build               # this is the typecheck
npm run test:e2e            # needs the full stack up
```

The platform console's signed-in specs skip unless an operator exists —
they need a `platform_admins` row (table owner only, by design) and a live
TOTP secret. One command provides both, from `backend/`:

```bash
eval "$(python scripts/provision_e2e_operator.py --base-url http://localhost:8000 | sed 's/^/export /')"
cd ../frontend && npm run test:e2e
```

Run them against a **production** frontend build, not `next dev` — the
README's "Local development notes" explains why and gives the compose
overlay. Two things that will bite otherwise: the login rate limiter counts
these attempts (the
spec signs in once per run for exactly that reason, but repeated debugging
runs will trip it — clear `platform-login:*` from Redis), and re-running
provisioning against an account whose MFA is already active fails with a
409, so pass a fresh `--email`.

`test:e2e` registers a fresh company per spec file and has no teardown, so
a dev database accretes roughly 29 companies and 37 users per full run,
cumulatively. When the tenant list gets noisy enough to make debugging
harder, prune it from `backend/`:

```bash
python scripts/prune_dev_tenants.py         # dry run: reports, deletes nothing
python scripts/prune_dev_tenants.py --yes   # actually deletes
```

It targets `E2E %` companies and `e2e-%` users by default, refuses to run
with `app_env=production`, and discovers both the table list and the
foreign-key order from the catalog rather than a transcribed list. Read the
dry run before passing `--yes` — a widened `--company-like` would take your
real dev tenants with it.

Whole stack:

```bash
docker compose up          # creates the schema for you, then serves
```

`up` alone is enough: a one-shot `migrate` service runs `alembic upgrade
head` first and `backend`/`worker` are gated on it completing, the same
shape the production stack uses. Until that service was added, `up` alone
left you with six healthy containers and an empty database — and the
first registration 500'd with nothing pointing at why.

To migrate out of band — say you hand-wrote a revision while the stack was
already running — it is still `docker compose exec backend alembic upgrade
head`.

Four footguns worth knowing before you lose an hour to them:

- **Python 3.12 is required and 3.11 fails confusingly.** `pip install -e .`
  under 3.11 prints `ERROR: Package requires a different Python` and then
  **exits 0**. A script that checks the exit code will believe it worked.
- **`caplog` does not capture app loggers.** `app/core/logging.py` calls
  `basicConfig(force=True)` at import, which removes the handler pytest
  installs on the root logger. Substitute the module's logger instead —
  `tests/test_financial_record_sync_handler.py` has a worked example.
- **`localhost` in the DB URLs is host-side only.** `.env` points
  `MIGRATIONS_DATABASE_URL`/`TEST_DATABASE_URL` at `localhost:5432` because
  Alembic and pytest normally run on the host. Inside a container that is
  the container, so `docker-compose.yml` overrides them per-service for
  `backend` and `worker`. A new service that runs Alembic, or reads
  `scanner`'s URL, needs the same override — without it you get
  `Connect call failed ('127.0.0.1', 5432)`, which has now happened twice.
- **Line endings are LF, enforced by `.gitattributes`.** `deploy/backup/*.sh`
  are bind-mounted into Linux containers; a CRLF shebang fails there as
  `/bin/bash^M: bad interpreter`, at 01:30, on the unattended backup job.
  Do not set `core.autocrlf=true` for this repo.

## The gates, and what each one is actually protecting

These are not style checks. Each exists because something went wrong once.

| Gate | Protects |
|---|---|
| `tests/test_rls_policy_coverage.py` | Every tenant table has a *real* RLS policy. A `USING (true)` policy satisfies "has a policy" and leaks everything, so the sweep inspects the expression. |
| `tests/test_module_boundaries.py` | No router imports another router; no router outside `projects.py` hand-rolls a `select(Project)` by-id lookup. The second half exists because a router that writes its own lookup imports nothing, and so passes the first half while dropping the field-crew and client row scopes. |
| `tests/test_tier_gating.py` | Every mutating route in a gated module carries the *correct* module tag, with counts pinned so a new route cannot slip through unclassified. |
| `tests/test_worker_db_roles.py` | No `app/` module reads `migrations_database_url`; `scanner` holds BYPASSRLS and nothing else. |
| `tests/test_migration_downgrade.py` | Walks to `base` and back. The re-upgrade is the half that matters — it proves a downgrade *removed* things rather than merely not raising. |
| `tests/test_stale_write_guard_coverage.py` | Every PATCH route is classified as guarded or explicitly unguardable. Count pinned. |
| OpenAPI schema-diff (CI) | `backend/openapi.json` matches the code. Regenerate, never hand-edit. |
| `failOnFlakyTests` (CI) | A Playwright test that passes on retry fails the job. It has caught two real product bugs that a green check had been hiding. |
| `deploy-config` (CI) | Every compose file parses, the worker's actor lists agree across all three topologies, the backup scripts parse, and both Caddyfiles validate. |

**A pinned count failing is the gate working.** Classify the new route or
table; do not bump the number to make it pass.

## Conventions with teeth

- **Regenerate, never hand-edit** `backend/openapi.json` and
  `frontend/lib/api/types.ts`. Route *docstrings* flow into the snapshot's
  `description` fields, so editing a docstring moves it.
- **A new tenant-owned table needs its RLS policy in the same migration
  that creates it.** There is no catch-all.
- **A new Dramatiq actor must be added to all three compose files** and to
  `e2e-ci.yml`. Miss one and that topology dead-letters those messages
  silently — no error, no log.
- **Any non-`.py` file `app/` reads at runtime** must be added to
  `[tool.setuptools.package-data]`, or it exists everywhere except the
  production image.
- **Money is `Numeric(12,2)`**, quantized with `CENTS` and an explicit
  `rounding=ROUND_HALF_UP`. Without the explicit rounding you get Python's
  banker's default, which disagrees with Postgres at `.xx5`.

## Shared code

Extract when the copies are the same thing. Do **not** extract when they
are merely similar — a helper that grows a boolean per caller is worse
than the duplication it replaced. Two decisions in this repo record the
line: `lib/use-cursor-list.ts` covers six list loaders and deliberately
excludes `integrations/page.tsx` (different envelope, and a 404 there is a
normal state rather than an error); `tests/conftest.py`'s
`register_and_login` takes one optional `tier` keyword, and the callers
that need more keep a two-line local wrapper.

## Tests

- Write the test so it **fails against the unfixed code**, and say so in
  the PR. A regression test that never failed is a restatement.
- Prefer asserting the *observable* contract over the mechanism.
- When adding a tenant table or a mutating route in a gated module, extend
  the corresponding sweep, following the existing style in that file.

## Pull requests

Explain *why*, and be specific about what you verified and what you did
not. "Full suite passes" is worth more than a summary of the diff, which
the diff already contains. If you deliberately did not fix something in
scope, say so and why — a documented non-fix is a decision; a silent one
is a bug someone finds later.
