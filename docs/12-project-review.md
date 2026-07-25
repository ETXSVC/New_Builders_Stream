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

3. **M1** — quantize the three remaining monetary writes (small, mechanical, correctness-affecting).
4. **M5** — surface the silent compliance failures (a user-visible dead end today).
5. **M10** — reconcile the docs (test counts, the missing event, `/health` vs `/ready`); then **M2/M3** when touching those modules.
6. Everything else as encountered; the Low list is genuinely low.

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
