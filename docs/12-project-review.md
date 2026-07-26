# Project Review — 2026-07-24

A full-codebase review across six dimensions (backend architecture,
security/tenant isolation, frontend, tests/CI, deployment/operations,
documentation), conducted against the branch carrying the production-
readiness work. Findings that were **fixed during the review** are marked
✅ and described in past tense; everything else is open, with a severity
and enough detail to act on.

**Headline:** the architecture is sound and unusually well-documented in
place; the defects that matter cluster in two areas — the `company_id`
stamping convention (three surviving violations of a bug class fixed six
times before) and deployment artifacts that had never been executed
(five issues that would have broken a first real deploy).

---

## 1. Fixed during this review

| Area | Issue | Fix |
|---|---|---|
| Security | `EstimateLineItem` stamped with the acting session's company, not the estimate's (`estimates.py:538`) | Uses `estimate.company_id`; a child branch's estimate no longer shows zero line items to the branch that owns it |
| Security | `Esignature` stamped with the acting company (`estimates.py:831`) — broke the ESIGN evidence chain for a child branch's signed document | Uses `estimate.company_id`, matching the route's own `publish()` and `change_orders.py`'s equivalent |
| Security | `CommunicationLog` stamped with the acting company (`leads.py:217`) | Uses `lead.company_id` (the lead was already fetched; its return value had been discarded) |
| Uploads | `branding.py:63` still did a bare `await file.read()` — the one multipart route the upload-cap sweep missed | Uses `read_upload_limited`; capped before the body is in memory |
| Deploy | `frontend/.dockerignore` lacked `.env*`: `COPY . .` could pull a developer's `.env.local`, and Next **inlines** `NEXT_PUBLIC_*` at build time — permanently baking `localhost:8000` into the image and making the compose-provided runtime value a no-op (every BFF fetch would ECONNREFUSED) | `.env` / `.env.*` excluded, with the reasoning in the file |
| Deploy | `restore.sh` built container paths as `$(pwd)/$arg`, breaking on the absolute paths `BACKUP_DIR` normally produces (silently mounting an empty directory) | Paths resolved to absolute and existence-checked before use |
| Deploy | `restore.sh` re-mounted the documents volume that `db-backup` already mounts **read-only** — duplicate mount target, or a silent no-op restore | Documents restore runs as a plain `docker run` against the named volume |
| Deploy | `restore-drill.sh` would fail on a **good** backup: the dump's GRANT/RLS statements reference `app_user`, which `pg_dump -Fc` doesn't carry, so `pg_restore` errored and `set -e` aborted before the assertions | Creates the referenced role before restoring |
| Deploy | `backup.sh` treated an empty/wrong-database dump as success — undetectable until the next quarterly drill | Asserts a minimum dump size; a broken backup fails that night's cron |
| Runbook | `TEST_DATABASE_URL` (a required Settings field) missing from the env table — a `.env` built strictly from the runbook would fail to boot backend, worker, and migrate | Added to the table |
| Runbook | Smoke-test item 8 used `wget`, absent from `node:22-slim` | Rewritten using node's own `fetch` |
| Runbook | Split topology didn't say where the split `.env` lives (compose resolves it to `deploy/split/.env`, not the repo root) or that `DOCUMENTS_DIR` must be `chown 1000` (the image runs non-root; Docker doesn't chown binds — every upload would EACCES) | Both documented as explicit pre-deploy steps |
| Runbook | Migrate-failure mode during upgrades undocumented (the old backend keeps serving against a half-migrated schema) | Documented with the stop-first recovery order |
| Runbook | The prod `edge`/`internal` network split — the property keeping Postgres/Redis unreachable — was never explained | New §6b |

Four regression tests were added (`tests/test_child_branch_stamping.py`),
each verified to fail without its fix.

---

## 2. Open findings

### High

Both High findings were **closed on 2026-07-25** — see §6.

### Medium

| # | Area | Finding | Where |
|---|---|---|---|
| M1 | Backend | Payment amounts (`invoices.py:226`, `bills.py:290`) and `cost_delta` (`change_orders.py:252`) are **not quantized** before hitting `Numeric(12,2)`. The overpayment guard compares an unquantized input against `remaining`, so `10.004` passes and persists as `10.00`; `cost_delta` feeds the final-invoice remainder math. | 3 sites |
| M2 | Backend | Routers import **other routers' private helpers** (`_get_project_or_404`, `_get_subcontractor_or_404`) across six modules — the cross-module coupling CLAUDE.md says must go through `app/services/`. | bills, expenses, invoices, tasks, change_orders, subcontractor_assignments |
| M3 | Backend | Catalog list pagination uses a **mutable cursor key** (`updated_at`): a concurrent `PATCH` moves a row to the end of the ordering, so a paging client can see it twice — the instability the pagination module rejects offset paging for. | `catalogs.py:378,394` |
| M4 | Backend | `ESTIMATE_APPROVED` publishes without `actor_id`, so `invoice.auto_generated` audit rows are attributed inconsistently (null from one handler, real actor from the other) for the same action. `current.user.id` is in scope at the publish site. | `estimates.py:865` |
| M5 | Frontend | Compliance notification load + dismiss failures are **fully silent** — the user clicks Dismiss, nothing happens, no message. | `compliance/page.tsx:65,87` |
| M6 | Frontend | Two different destructive-confirm patterns: `window.confirm` in Phases/Tasks vs inline two-step confirm in billing. `window.confirm` is also untestable in Playwright without a dialog handler. | `PhasesTasksTab.tsx:155,175` |
| M7 | Frontend | Tab surfaces have `role="tablist"`/`aria-selected` but no `aria-controls`, no `role="tabpanel"`, no arrow-key handling — screen readers announce a tab with no reachable panel. | billing, catalog, project detail |
| M8 | Tests | No e2e coverage for: integrations page, my-tasks, MFA **enrollment** (only the nudge is asserted), branding, and the entire client-role view. | `frontend/e2e/` |
| M9 | Tests | `playwright.config.ts` sets no `retries`/`trace` — the next flake is a red X with no artifact. `retries: 1` + `trace: "on-first-retry"` would make it diagnosable. | `playwright.config.ts` |
| M10 | Docs | Test counts are stale and mutually inconsistent (README "880+", CLAUDE.md "765+"; actual ~902). CLAUDE.md's event list omits `PROJECT_COMPLETED`, and nothing documents the `/health` vs `/ready` split, MFA, rate limiting, upload caps, or the split topology. | README, CLAUDE.md |

### Low

- `financial_record_sync_handler.py:56` enqueues Dramatiq messages inside the uncommitted request transaction; a later rollback burns all 3 retries (documented, but an outbox/after-commit hook is the real fix).
- `send_invitation_email` has no failure surface after retry exhaustion — the one actor with no bookkeeping row.
- Five module-level owner engines across `app/tasks/`; every importing process opens all of them.
- Money schemas lack a `decimal_places=2` backstop; catalog `unit_rate` unquantized.
- `projects/page.tsx` is the only list surface missing the `requestGenRef` stale-response guard its seven peers have.
- Eight list components duplicate ~30 lines of fetch/guard boilerplate — a `useCursorList` hook would remove ~240 lines and would have prevented the omission above by construction.
- `_register_and_login` is redefined in 49 of 77 test files; a conftest fixture is warranted. Cross-test-file helper imports create fragile chains.
- Config validator misses two checks it arguably should make: Fernet key ≠ JWT secret (which `config.py`'s own comment requires) and SMTP credentials with STARTTLS disabled.
- `Caddyfile.api` lacks the `request_body max_size` cap its sibling has, and its bare `respond 403` relies on Caddy's directive ordering rather than an explicit `handle`.
- Design docs drift: `docs/05` omits several routers (declare `openapi.json` authoritative), `docs/03` still says OIDC/Keycloak + Traefik/Nginx (reality: PyJWT/Argon2id + Caddy), `docs/06` says "Celery" (reality: Dramatiq) and promises WAL archiving that `docs/11` deliberately defers, `docs/09` has no status markers.
- Missing documentation: no frontend architecture doc (the largest undocumented surface), no CONTRIBUTING, no CHANGELOG, no ADR index.

---

## 3. Strengths (verified, not assumed)

- **Tenant isolation is real.** Every tenant table has an RLS policy; the `WITH CHECK` on `companies` UPDATE blocks re-parenting; owner-role engines exist only in `app/tasks/`; `X-Tenant-ID` spoofing is stopped by the membership lookup *before* the tenant context is set. Tests run as the RLS-restricted `app_user`, so the guarantees are actually exercised.
- **Auth is carefully built**: HS256 pinned (no `none` confusion), 15-minute access tokens, opaque hashed refresh tokens with family revocation and a DB-enforced "successor implies revoked" invariant, constant-time login, MFA behind password re-auth.
- **Transaction discipline is uniform** — the commit-after-handler invariant holds across 20 routers and 4 event handlers, with zero inline commits in tenant routes.
- **Introspection-driven regression tests** (`test_company_id_index_coverage.py`, the tier-gating completeness pair) cover *future* code and fail with actionable messages.
- **The BFF boundary is airtight** and enforced by `server-only` imports rather than convention; no component holds a secret or calls the backend directly.
- **Comments explain the "why," including rejected alternatives and the specific failure that motivated a choice** — an unusually high standard, upheld even in the newest hardening code.
- **Zero offset pagination**; cursor pagination with a stable tiebreaker everywhere it matters.
- **Idempotent accounting sync** with a stable key and COALESCE-guarded external IDs — the double-post race was identified and closed, not hand-waved.

---

## 4. Recommended order of work

~~1. **H1**~~ and ~~2. **H2**~~ — both done, §6.

~~3. **M1**~~, ~~4. **M5**~~, ~~5. **M10**~~, ~~**M3**~~, ~~**M4**~~, ~~**M9**~~ — all done, §8.

What is actually left, in the order worth doing it:

1. **M8's client-role e2e spec** — the client-role scoping added in §7.1 has
   unit coverage but no browser-level proof. The rest of M8's list
   (integrations, my-tasks, MFA enrollment, branding) is ordinary coverage
   debt; this one covers code that is both new and security-relevant.
2. **M2** — route the six cross-router private-helper imports through
   `app/services/`. No defect today, purely the coupling CLAUDE.md forbids.
3. **M6/M7** — frontend confirm-pattern consistency and tab a11y.
4. The Low list, which is genuinely low.

---

## 5. Deployment: verify first on the real box

The sandbox cannot run Docker builds, so these were reviewed but never
executed. Check them on the first deploy, in this order:

1. ~~Confirm `NEXT_PUBLIC_API_URL` wasn't inlined at build time.~~ **Now automated** — `frontend-ci.yml`'s `docker-build` job boots the image against a sidecar and fails if the BFF doesn't dial the runtime value (§6).
2. Split topology only: `stat -c '%u' $DOCUMENTS_DIR` must be `1000`; then upload a document and generate a PDF. (`docker-build` now proves the *image* runs as uid 1000 with a writable `/data/documents`; the host bind's ownership still has to be checked on the box.)
3. Dry-run `restore.sh` with an **absolute** dump path against a scratch stack; confirm the documents tarball lands in the real volume.
4. Run `restore-drill.sh` **now**, not next quarter.
5. Boot from a `.env` built strictly from the runbook's §2 table (catches any other missing required field).
6. `caddy validate` in both Caddy containers, then re-run smoke-test item 6 (ESIGN records the real client IP).
7. Force a failing migration on a scratch box and confirm what keeps serving.

---

## 6. Follow-up — 2026-07-25: both High findings closed

### H1 — Docker images are now built and exercised in CI

Two new `docker-build` jobs, one per workflow, so each image is owned by
the workflow that owns its code:

- **`backend-ci.yml`** builds the `production` target, then *runs* it:
  imports `app.main`, loads the PDF template out of the installed package,
  imports the four runtimes the deploy launches from this one image
  (uvicorn / dramatiq / apscheduler / alembic), imports every actor module
  named in `docker-compose.prod.yml`'s worker command — **read out of the
  compose file**, so the list can't drift from what the worker actually
  starts — asserts the container runs as uid 1000 with a writable
  `/data/documents`, and finally builds the `dev` target the dev compose
  stack uses.
- **`frontend-ci.yml`** builds the `production` target and boots it on a
  throwaway network beside a `python -m http.server` sidecar standing in
  for the backend. `/login` and `/` must both return 200 (that is what
  proves the hand-assembled standalone layout — `server.js` +
  `.next/static` + `public`, three separate `COPY`s — is complete), then a
  POST to the BFF's login route must show up in the **sidecar's** log. That
  last assertion is the one that matters: it proves `NEXT_PUBLIC_API_URL`
  is read at run time, not inlined at build time, which was §5's first
  deploy-day check and is now automated.

**This immediately found a real, shipped defect.** `pip install .` — the
non-editable install the `builder` stage runs — was silently dropping
`app/templates/estimate_pdf.html.jinja` from site-packages, because
setuptools' `include_package_data` default only picks up files a
`MANIFEST.in` or VCS plugin declares, and this project has neither. Local
dev and every CI job run an *editable* install, which imports straight from
the source tree, so all 900+ tests passed against a package that, once
built into an image, would raise `TemplateNotFound` on the first PDF-export
job. Fixed with a `[tool.setuptools.package-data]` entry in
`backend/pyproject.toml`; the smoke step loads the template from the
installed package specifically so this cannot regress.

Note the shape of that bug: **building the image would not have caught
it.** `docker build --target production` succeeds either way. Only running
the image catches it — which is why these jobs run the images rather than
just building them.

### H2 — RLS-policy coverage test

`backend/tests/test_rls_policy_coverage.py`, the companion to
`test_company_id_index_coverage.py`, asserts five properties against
Postgres's own catalogs:

1. every table with a `company_id` column has RLS **enabled** and at least
   one policy;
2. each has a `FOR ALL` policy whose USING *and* check expressions both
   call `get_all_descendant_ids` / `get_root_company_id` — this is what
   catches a policy that exists but reads `USING (true)`, which property 1
   alone would happily pass;
3. any *additional* permissive policy is on a reviewed allowlist (Postgres
   ORs permissive policies, so each one can only widen access) — currently
   `company_users.self_membership` and `invitations.invitation_probe`, each
   documented in the file with why it can't be tenant-scoped;
4. every table *without* a `company_id` column either has RLS enabled or is
   on the `NON_TENANT_TABLES` allowlist (`users`, `refresh_tokens`,
   `alembic_version`) — so a tenant table that models ownership some other
   way can't hide from property 1;
5. `companies` — the tree's root, which has no `company_id` column and
   per-command rather than `FOR ALL` policies — is tenant-scoped on every
   expression it defines, and does **not** have `FORCE ROW LEVEL SECURITY`
   (which would make `get_all_descendant_ids` recurse into the policy that
   calls it; migration 0001 records hitting exactly that).

Plus a sixth: `app_user` has neither `BYPASSRLS` nor `SUPERUSER`, without
which every policy above is decoration.

All five defect cases were verified to fail the corresponding assertion
before being reverted: a tenant table with no RLS, a `USING (true)` policy,
an extra wide-open permissive policy, a tenant-ish table with no
`company_id` column, and `ALTER ROLE app_user BYPASSRLS`.

---

## 7. Follow-up — 2026-07-25: the 2026-07-12 audit's open items

A separate audit (`requirements-vs-implementation-comparison`, 2026-07-12
vintage) was re-verified against current `main`. Roughly half its findings
had already been closed by later work; the rest were real and are addressed
here. One turned out to rest on a false premise — see §7.9.

### 7.1 The `client` role was a tenant-wide reader (migration 0019)

The most serious finding, and confirmed present. RLS is company-scoped, so
every `test_*_tenant_isolation.py` file passed while this was open — the
hole was *inside* one tenant. Client-facing routes narrowed by document
**status** and never by identity, so a company with two customers showed
each the other's pricing, margins, invoices and executed contracts, and
`POST /estimates/{id}/approve` let either legally e-sign the other's
contract.

Root cause was schema-level: nothing said *which* client a Project,
Estimate or Invoice belonged to. Fixed with `project_clients` /
`lead_clients` membership tables rather than a denormalized FK per record —
one row grants one user access to one job, two homeowners on one contract
is a row rather than a schema change, and there is a single place to write
the linkage (a `client_user_id` copied onto four models would re-open the
`company_id`-stamping bug class already fixed seven times).

The rule lives in `app/services/client_scope.py` and is applied at the
by-id chokepoints, so `approve`/`reject` inherit it by construction. All
404s, never 403, so a client cannot enumerate another's document ids.

Turning it on broke **28 existing tests**, every one a place where a client
acted on a job they had no relationship to;
`test_get_esignature_allowed_for_read_roles` asserted the vulnerable
behaviour outright and was rewritten.

### 7.2 Signature attribution was unverified

`signer_name`/`signer_email` were free-text form fields never compared to
the caller, on a record whose entire purpose is legal evidence.
`esignatures.signed_by_user_id` now records the account and `signer_email`
must be the caller's own. `signer_name` stays free text deliberately —
people sign in varied forms, and the FK carries the identity claim.

Pre-0019 rows carry NULL and are invisible to every client rather than
visible to all of them: those are precisely the records whose attribution
was never verified.

### 7.3 No rate limit on login or TOTP

`/auth/register` was limited from the start; `/auth/login` was not. Two
counters now, both of which must pass: per-IP (one host spraying many
accounts) and per-email (a botnet grinding one account, invisible to
per-IP). Checked *before* password verification, so a blocked attacker
learns nothing and never reaches the Argon2 call. The email counter's Redis
key is SHA-256 hashed — Redis keys surface in `MONITOR`/`KEYS`/slowlogs,
and a per-address counter should not become a roster of every address that
has attempted a login.

TOTP is throttled per user id: the replay guard blocks *reuse* of a code,
never a fresh guess across the 10⁶ space.

### 7.4 Stripe webhook had no replay protection

The fake signed a bare hex digest with **no timestamp**, which makes a
replay window impossible to express — a captured body stayed valid forever
on a public route that can move any tenant's subscription to `active`. Now
implements Stripe's real `t=,v1=` format with the timestamp inside the
signed string and a 300s tolerance, checked on the absolute difference so a
far-future stamp is rejected too.

### 7.5 All four background jobs ran as the table owner (migration 0020)

Single-tenant `accounting_sync` moved to the RLS-constrained `app_user`,
resolving its tenant through a narrow SECURITY DEFINER lookup. The three
genuinely cross-tenant sweeps moved to a new `scanner` role: BYPASSRLS,
DML grants, **owning nothing** — same reach, no ability to alter a policy
or drop a table. `tests/test_worker_db_roles.py` pins it, including an AST
sweep that fails if any future `app/` module reads
`migrations_database_url`.

### 7.6 `app_user`'s password was a literal in migration 0001

Both roles' passwords now come from the environment at `alembic upgrade
head`, so rotation happens during a normal deploy and the runbook's manual
`ALTER ROLE` step is gone.

### 7.7 `companies.parent_id` was mutable (migration 0021)

`tenant_update`'s `WITH CHECK` needs its `parent_id IS NULL` branch for
INSERT, but on UPDATE that branch permits detaching a child into a new
root — leaving its parent's descendant tree *and* arriving with no
subscription row, which both `block_if_read_only` and `tier_allows` treat
as fail-open.

Fixed with a trigger, not a tighter policy: dropping the `IS NULL` branch
breaks every root company (a root's `parent_id` IS NULL, so
`NULL IN (SELECT ...)` is NULL and renaming becomes impossible). The rule
is "parent_id may not *change*", which needs the old row — a `WITH CHECK`
cannot see it, a `BEFORE UPDATE` trigger can. It also binds the table owner
and `scanner`, not just `app_user`.

### 7.8 Missing management API surface

Member role change and removal, company rename, invitation list and revoke,
subcontractor update. Offboarding an employee was previously impossible
through the API. Two guards worth noting: a company can never be left
without an admin (every administrative route is `require_role("admin")`, so
zero admins is a permanent lockout), and revoking an *accepted* invitation
is a 409 rather than a no-op, since deleting the row would not un-create
the membership it produced.

### 7.9 "No compliance-document delete" — not a gap

Migration 0009 explicitly `REVOKE UPDATE, DELETE ON compliance_documents
FROM app_user`, documented as "immutability by omission" matching the
`esignatures` precedent: a certificate is evidence, immutable from the
instant it is written. A delete route was written, failed with `permission
denied`, and was **removed** rather than re-granting DELETE in a migration
— that would deliberately undo a compliance guarantee. A test now pins
`app_user`'s grants to `{SELECT, INSERT}`.

The real complaint behind the finding (a mistaken upload keeps generating
expiry alerts) wants a supersede/void concept with its own audit trail —
a retention-policy decision, not a missing endpoint.

### 7.10 Nothing sent email except invitations

`send-for-signature` flipped a status column and notified nobody, and
compliance-expiry wrote rows visible only on the dashboard. Both now
enqueue actors. Worth noting *why* the first was hard before: there was no
recipient to resolve until migration 0019 made "which client owns this
document" answerable — one schema gap producing both a security hole and a
missing notification.

The expiry sweep enqueues **after** its commit, so a scan that fails
partway cannot tell a subcontractor about a row that was rolled back.

### 7.11 Process gaps

`requirements.lock` (99 pinned packages), dependabot across pip/npm/actions,
`pip-audit` in CI (non-blocking — an advisory published overnight is not a
reason an unrelated PR cannot merge), coverage reported without a
`fail_under` (a floor measures execution, not assertion), and a migration
**downgrade** test that walks the chain to `base` and back.

That last one found a real bug on its first run: migration 0020's own
downgrade tried to `DROP ROLE scanner`, but roles are cluster-level and
another database still granted it. Mirroring each GRANT with a REVOKE was
also insufficient — the dependencies include every per-table ACL entry and
the `ALTER DEFAULT PRIVILEGES` entry. It now uses `DROP OWNED BY` and
leaves the role, matching migration 0001's treatment of `app_user`.

### 7.12 Open, deliberately

- **`pip-audit` reports 21 advisories across 4 packages.** The two that
  matter are `starlette` 0.46.2 (9 advisories; fixes require a **FastAPI
  major bump**, since FastAPI is pinned `>=0.115,<0.116`) and
  `cryptography` 43.0.3 (5 advisories; fixes in 44.0.1+, pinned `<44.0`).
  Both need a deliberate dependency upgrade with its own review and CI run,
  not a line buried in an audit-remediation commit. Dependabot will now
  propose them.
- **`SCANNER_DATABASE_URL` falls back to the owner URL when unset**, so an
  existing deployment survives the upgrade rather than failing to start its
  worker. An operator who ignores the new variables keeps running the
  sweeps as the table owner. The compose files set it; the runbook
  documents it.
- **Invitation ids are still the accept credential** (opaque tokens were an
  explicitly excluded item in an earlier scope decision). Revocation now
  exists, which was the sharper half of the problem.

---

## 8. Follow-up — 2026-07-26: the Medium list

Six of §2's ten Medium findings are now closed. Recorded here because §4's
ordering list is what the next session reads to pick up work, and a review
doc that still lists closed findings as open is worse than no list.

| # | Closed by | Note |
|---|---|---|
| M1 | PR #41 | Quantize **before** the guard, not after — see below. |
| M4 | PR #41 | `ESTIMATE_APPROVED` now publishes `actor_id=current.user.id`. |
| M9 | PR #41 | `retries: 1` + `trace: "on-first-retry"`. |
| M5 | PR #42 | Separate `notificationsError` state, a Retry button, and an e2e spec that forces the 500 with `page.route`. |
| M10 | — | Counts reconciled; CLAUDE.md documents `PROJECT_COMPLETED`, the `/health` vs `/ready` split, MFA, rate limiting, upload caps, and the split topology. |
| M3 | PR #43 | Immutable cursor key. |
| M2 | this change | See §8.2 — the finding understated it. |

**M1's description above is wrong in one detail**, left in place rather than
edited so the correction is visible: it says `10.004` "passes the guard and
persists as `10.00`". It does not — `Decimal("10.004") > Decimal("10.00")` is
`True`, so an exact-remainder payment of `10.004` was *rejected* with a 409.
The real symptoms were a **false rejection** of a payment that rounds to the
remainder, and **response drift** (`9.999` echoed back to the caller while
`10.00` was stored). Both fixed by quantizing before the comparison rather
than letting `Numeric(12,2)` round after it.

### 8.1 M3 — the catalog cursor key was mutable

`_paginate_resolved_items` sorted on `(updated_at, id)`, chosen to echo the
`(created_at, id)` composite `paginate()` hardcodes, substituting `updated_at`
because `cost_catalog_items` has no `created_at` column. The substitution
looked cosmetic and was not: `created_at` is immutable and `updated_at` is
not. A `PATCH /catalogs/items/{id}` — an ordinary unit-rate edit, the most
likely write to land against this table while someone is paging it — rewrites
`updated_at` to `now()` and moves that row to the **end** of the ordering. A
row already returned on page 1 then sorts after the caller's cursor and comes
back a second time.

The key is now `id` alone: random, so it carries no calendar meaning, but
unique and **immutable**, which is the only property a cursor key needs. That
is the same conclusion `_paginate_markup_profiles` in the same module had
already reached from the other direction (that table has no timestamp column
at all), so the two list routes now share `_encode_id_cursor` /
`_decode_id_cursor` and one rationale. **No migration**: adding a `created_at`
column was considered and rejected — the schema doc deliberately omits it, and
an immutable key was already available.

Cost: a caller walking the catalog sees every item exactly once but in no
human-meaningful order. `category`/`search` are how this API expects a caller
to find a specific item; page order never was.

Pinned by `test_list_catalog_items_pagination_survives_a_concurrent_edit`,
which edits an already-returned row mid-walk. Against the old key it fails
with the duplicate id in the seen-list; against the new one it passes.

### 8.2 M2 — routers imported each other's private helpers

Eight imports crossed module boundaries to reach two functions:

```
app/routers/bills.py                     -> projects, subcontractors
app/routers/expenses.py                  -> projects
app/routers/invoices.py                  -> projects
app/routers/tasks.py                     -> projects
app/routers/change_orders.py             -> projects
app/routers/subcontractor_assignments.py -> projects, subcontractors
```

Each one was individually reasonable — `_get_project_or_404` is genuinely
the right check to run, and duplicating it would have been worse. That is
why review passed all eight: the cost is only visible in aggregate.
`projects.py` had become a library six other modules linked against, so its
"private" helpers could not be touched without auditing six routers, and
importing a router to borrow one function drags in that router's entire
import graph.

`get_project_or_404` and `with_field_crew_scope` now live in
`app/services/project_lookup.py`; `get_subcontractor_or_404` lives in
`app/services/subcontractor_lookup.py`. Both lost the leading underscore,
which was never accurate.

**The finding understated the problem.** M2 was filed as coupling, and the
fix is worth more than that, because `get_project_or_404` carries
authorization: field_crew's assigned-only scope and the `client` role's row
scope (migration 0019). A module that cannot conveniently import it writes
its own — and one already had. `app/routers/bom_lines.py`, merged from
`feature/bom` in PR #46, carried this:

```python
async def _get_project_or_404(current, project_id):
    result = await current.session.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project
```

Existence check only — no field_crew scope, no client scope. **Not
exploitable**: `bom_lines.py`'s `_ROLES` is `("admin", "project_manager")`,
and both scopes are no-ops for staff, so the copy behaved identically to the
real one for every caller that could reach it. It was a hole waiting for
that tuple to grow, in a module written against a pre-0019 branch. It now
calls the shared function.

`tests/test_module_boundaries.py` is the gate, and it has two halves
because the obvious half is not sufficient:

- **No router imports another router** — an AST sweep over `app/routers/`,
  covering `import app.routers.x`, `from app.routers.x import y`, and the
  relative `from .x import y` (the obvious way back in once the absolute
  form is blocked). Docstrings that *discuss* another router are untouched,
  which matters: several of them are worth reading.
- **No router outside `projects.py` hand-rolls a `select(Project)` by-id
  lookup** — because the import sweep alone cannot catch `bom_lines.py`. A
  router that writes its own unscoped lookup imports nothing at all, so it
  passes the boundary rule while committing the exact error the boundary
  rule exists to prevent. Matching is on the AST shape (a `.where(...)`
  chained off `select(Project)` comparing `Project.id`), so the many
  legitimate Project queries that scope by something else are unaffected.

Plus a non-vacuity test pinning a floor on how many routers were scanned —
every assertion above passes trivially against an empty file list, and a
renamed directory or changed glob would otherwise report green forever.

CLAUDE.md's "a convention enforced by review, not tooling" has been updated;
it is tooling now.
