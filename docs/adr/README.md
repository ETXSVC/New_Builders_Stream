# Architecture Decision Records

An index, not a new process. Every decision below was already made and
already documented — in a module docstring, a migration, or a design doc.
What was missing was a single place to find out *that* a decision exists
before spending an afternoon rediscovering the reasoning, or worse,
"fixing" it.

Each row points at the code that is the real record. If a row and the code
disagree, the code is right and this file is stale — say so in a PR.

## Decisions

| # | Decision | Why | Recorded in |
|---|---|---|---|
| 1 | **PostgreSQL RLS is the tenant boundary**, not application code | App-layer filtering is one forgotten `WHERE` from a cross-tenant leak. A policy cannot be forgotten per-query. | migrations 0001/0019/0020/0021, `tests/test_rls_policy_coverage.py` |
| 2 | **The runtime connects as `app_user`**, never a table owner | Owners bypass RLS entirely, which would make every isolation test vacuous. | migration 0001, `tests/test_worker_db_roles.py` |
| 3 | **`companies.parent_id` is immutable** | Re-parenting moves a subtree between tenants and detaches it from its subscription. It is a migration, never a write. | migration 0021 |
| 4 | **The `client` role is scoped by row, not just by company** | RLS is company-scoped and says nothing about two customers of the *same* builder. | migration 0019, `app/services/client_scope.py`, `tests/test_client_role_isolation.py` |
| 5 | **The event bus is in-process and synchronous** | A handler runs in the request transaction so its failure can roll the whole thing back. "Synchronous" means same-transaction, not non-async. | `app/core/events.py` |
| 6 | **Background enqueues happen after the commit** | The bus runs inside the transaction; Redis does not roll back with it. A rollback used to queue work naming a row that would never exist. | `app/core/after_commit.py` |
| 7 | **`get_current_user` holds the transaction open past `yield`, with `scope="function"`** | The tenant GUC is transaction-scoped, so committing early drops it; and the *request* exit stack closes after the response is sent, so a caller could beat the commit. | `app/core/deps.py` |
| 8 | **Cursor pagination on an immutable key** | Offset paging is unstable under concurrent inserts; a *mutable* cursor key (`updated_at`) lets an edited row be returned twice. | `app/core/pagination.py`, `app/routers/catalogs.py` |
| 9 | **404, never 403, for "exists but isn't yours"** | A 403 confirms existence, which makes other tenants' ids enumerable. | `app/services/client_scope.py` |
| 10 | **Dramatiq over Celery; PyJWT+Argon2id over OIDC; Caddy over Traefik/Nginx; xhtml2pdf over WeasyPrint** | Each trades a capability the product does not need for materially less operational surface. | `docs/03` (rows marked *as built*), `backend/pyproject.toml` |
| 11 | **Optimistic concurrency via `expected_updated_at` in the body**, not `If-Match` | The Next BFF forwards a fixed header allowlist, so a custom header would not survive the hop. | `app/services/concurrency.py` |
| 12 | **CI runs the Docker images, not just builds them** | Production installs non-editably; a file setuptools does not package exists everywhere except the artifact that ships. | `.github/workflows/backend-ci.yml` |
| 13 | **A flaky Playwright test fails the job** | Passing-on-retry exits 0, so the check went green while hiding two real product bugs. | `frontend/playwright.config.ts` |
| 14 | **WAL archiving deferred; daily `pg_dump` only** | Meets the stated RPO ≤ 24h. PITR buys a far more error-prone restore for archive plumbing not justified pre-revenue. | `docs/11-production-deployment.md` |
| 15 | **No metric is ever labelled with a tenant; routes are labelled by template** | A `company_id` label is one series per customer forever (cardinality) and exports a per-tenant activity feed to a dashboard with a different trust boundary (disclosure). A raw path is one series per project id, and lets an outsider grow Prometheus's memory by scanning 404s. | `app/core/metrics.py`, `tests/test_metrics.py` |
| 16 | **Monitoring UIs bind to loopback and are reached by SSH tunnel** | Caddy is the only intended front door; publishing Prometheus would put an unauthenticated query API on the public interface, and Grafana a second login surface to patch. | `docker-compose.prod.yml`, `tests/test_monitoring_config.py` |
| 17 | **The platform console authenticates through its own dependency, token scope and database role** | Cross-tenant administration must not be a branch inside `get_current_user` — the one function every product request already depends on. A separate path meant adding it changed that function by two lines that only *narrow* what it accepts. | migration 0023, `app/core/platform_deps.py`, `tests/test_platform_admin.py` |
| 18 | **No route can grant platform privilege; `platform_admins` is unwritable by every runtime role** | A guarded escalation route is a role check somebody later gets wrong. Revoking the grant from `app_user` and `scanner` removes the category instead — minting an operator needs database access and a shell. | migration 0023, `backend/scripts/grant_platform_admin.py` |
| 19 | **Module entitlement is three-state, and an override beats the tier** | Support needs to grant a module the plan withholds *and* withhold one it grants, without moving the customer's plan or touching Stripe. Two states would make "off" unexpressible. | migration 0023, `app/core/tier_gating.py` |
| 20 | **A hand-set subscription status pins itself against Stripe** | `/webhooks/stripe` is last-write-wins on `status`, so without a flag the next routine `customer.subscription.updated` silently reverts an operator's decision. `current_period_end` still syncs — that one is Stripe's fact to own. | `app/models/subscription.py`, `app/routers/webhooks.py` |

## Adding one

Only when a future reader would otherwise undo the decision by accident.
Put the reasoning where the code is, and add a row here pointing at it —
a record nobody can find is not a record.
