# Production Deployment Runbook

Self-hosted Docker Compose deployment (the topology `docs/03` and `docs/06`
specify: one VM, Caddy terminating TLS, everything else on an internal
Docker network). The stack file is `docker-compose.prod.yml`; the dev
`docker-compose.yml` is unchanged and remains the local-development stack.
To run the tiers on separate machines instead (backend API / middleware
worker tier / frontend, each independently deployable), see §8 — the
split stacks under `deploy/split/`.

Topology: **browser → Caddy (80/443, the only published ports) → Next.js
frontend (BFF) → FastAPI backend → Postgres/Redis**. The backend, worker,
scheduler, Postgres, and Redis have no published ports. The backend is not
routed by Caddy at all — the BFF proxies every product request, and until
real Stripe credentials exist there is no legitimate external caller of
`/webhooks/stripe` (see `deploy/Caddyfile` for the two-line addition when
that changes).

---

## 1. Prerequisites

- A VM (the Proxmox box) with Docker Engine + the compose plugin.
- A DNS A record for your domain (e.g. `app.example.com`) pointing at the
  VM's public IP; ports **80 and 443** reachable from the internet (Let's
  Encrypt issuance and renewal need both).
- A decision on `TZ` — the daily scheduler jobs (compliance expiry 02:00,
  seat usage 03:00, overdue flagging 04:00) fire in this zone; default UTC.

### If the box is LAN-only (no public domain)

Let's Encrypt cannot issue a certificate for a host it cannot reach, so a
box that is not published to the internet needs `SITE_ADDRESS` set to a
name ACME will never be asked about — anything under `.local` / `.lan` /
`.internal`, or a bare IP:

```
SITE_ADDRESS=builders.lan          # or 192.168.1.50
FRONTEND_BASE_URL=https://builders.lan
```

Caddy recognises those as non-public and switches to its **internal CA**
automatically — still real TLS, still HSTS, no ACME attempt, nothing to
configure beyond the two values above. Browsers will warn until you trust
Caddy's root once per client:

```bash
docker compose -f docker-compose.prod.yml exec caddy \
  cat /data/caddy/pki/authorities/local/root.crt > builders-root.crt
# then import builders-root.crt into each machine's trust store
```

Two consequences worth knowing before you choose this path. `FRONTEND_BASE_URL`
must still be `https://` — it is what invitation-email links and OAuth
redirects point at, and the boot validator rejects `http://localhost` but
cannot tell whether a LAN name resolves for the person clicking the link.
And smoke-test item 3 (§4) changes shape: the padlock will show a warning
until the root is trusted, which is expected here rather than a failure.

Move to a real domain later by changing `SITE_ADDRESS` and
`FRONTEND_BASE_URL` and recreating Caddy; nothing else in the stack knows
the difference.

## 2. Server `.env`

Copy `.env.example` to `.env` on the server and set **every** value below.
With `APP_ENV=production`, the backend **refuses to boot** while any
example secret remains, listing every violation at once — so a mistake here
is loud, not silent.

| Variable | Value / generation |
|---|---|
| `APP_ENV` | `production` |
| `SITE_ADDRESS` | your domain, e.g. `app.example.com` |
| `POSTGRES_USER` / `POSTGRES_DB` | keep `postgres` / `builders_stream` |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` |
| `APP_DB_PASSWORD` | `openssl rand -hex 24` (see the ALTER ROLE step below) |
| `DATABASE_URL` | `postgresql+asyncpg://app_user:<APP_DB_PASSWORD>@postgres:5432/builders_stream` |
| `MIGRATIONS_DATABASE_URL` | `postgresql+asyncpg://postgres:<POSTGRES_PASSWORD>@postgres:5432/builders_stream` (the prod compose also overrides this per-service with the same value) |
| `JWT_SECRET` | `openssl rand -hex 32` |
| `INTEGRATION_TOKEN_ENCRYPTION_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `STRIPE_WEBHOOK_SECRET` | `openssl rand -hex 32` |
| `FRONTEND_BASE_URL` | `https://<SITE_ADDRESS>` |
| `TEST_DATABASE_URL` | any valid URL — a required Settings field, unused in production; reuse the `MIGRATIONS_DATABASE_URL` value |
| `REDIS_URL` | `redis://redis:6379/0` |
| `TZ` | your zone, e.g. `America/Chicago` |
| `BACKUP_DIR` | host path for backups, e.g. `/opt/builders-stream-backups` |
| `GRAFANA_ADMIN_PASSWORD` | `openssl rand -base64 24` — the monitoring stack (§10) will not start without it |
| `APP_DB_PASSWORD` | `openssl rand -hex 24` — migration `0020` applies it to `app_user` (see below) |
| `SCANNER_DB_PASSWORD` | `openssl rand -hex 24` — same, for the `scanner` role |
| `SCANNER_DATABASE_URL` | `postgresql+asyncpg://scanner:<SCANNER_DB_PASSWORD>@postgres:5432/builders_stream` (the prod compose sets this per-service from the two values above) |
| `PLATFORM_DB_PASSWORD` | `openssl rand -hex 24` — same again, for the `platform_admin` role created by migration `0023`. **Set it before the first migration run**: `migrate` reads it when creating the role, and `backend` builds its connection URL from the same value, so the two only agree if it is present from the start |
| `PLATFORM_DATABASE_URL` | leave unset — the prod compose sets it per-service from `PLATFORM_DB_PASSWORD`. Unlike `SCANNER_DATABASE_URL` this setting has **no fallback**: if it is somehow empty the platform console 503s rather than running on a wider connection |
| `SMTP_*` | set to enable invitation emails; unset = recording fake (no email leaves the box). `SMTP_HOST` is the switch; `SMTP_FROM_ADDRESS` must be a real address on a domain you control — the backend **refuses to boot** on the `no-reply@localhost` default once `SMTP_HOST` is set, because mail from an unroutable sender is rejected or bounces into nothing and the invitation is lost silently. Verify with `docker compose -f docker-compose.prod.yml exec backend python scripts/send_test_email.py you@yourdomain.com`, which sends one message through exactly the client the app uses and prints what the relay said |

**Database role passwords.** Migration `0001` creates `app_user` with the
password hardcoded as `'app_password'` — a value published in this
repository. Migration `0020` rotates it, and creates the `scanner` role,
using `APP_DB_PASSWORD` and `SCANNER_DB_PASSWORD` **read from the
environment when `alembic upgrade head` runs**. Migration `0023` creates
`platform_admin` the same way, from `PLATFORM_DB_PASSWORD`, and defaults to a
published value if that variable is absent — so an unset value there is the
same class of mistake as leaving `app_password` in place.

Because the prod compose's one-shot `migrate` service reads `.env`, setting
all three values in `.env` before the first `up` is all that is required —
the rotation happens as part of the normal deploy, with no manual
`ALTER ROLE` step. Verify afterwards:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U postgres -d builders_stream -c "\du app_user scanner platform_admin"
```

Rotating later is the same mechanism: change the values in `.env` and
re-run `docker compose -f docker-compose.prod.yml up -d migrate`, then
restart `backend`, `worker` and `scheduler`.

(The boot validator rejects a `DATABASE_URL` containing `app_password`, so
skipping this cannot go unnoticed — the backend won't start until the URL
carries the real password, and the real password won't work until the
migration applied it.)

**The `scanner` role, and why the worker isn't the superuser.** The three
daily cross-tenant sweeps (compliance expiry, seat usage, overdue financial
records) genuinely have to read every company's rows in one pass. They used
to get that by connecting as the Postgres table owner — a role that is not
only exempt from RLS but can also drop tables and rewrite the policies that
enforce tenant isolation. `scanner` is `LOGIN BYPASSRLS` with DML grants
and no ownership: same reach, none of the ability to change the rules.
`tests/test_worker_db_roles.py` asserts both halves, including that
`scanner` cannot `DISABLE ROW LEVEL SECURITY` or drop a policy.

## 3. First deploy

```bash
git clone <repo> /opt/builders-stream && cd /opt/builders-stream
cp .env.example .env   # then edit per the table above — every value
docker compose -f docker-compose.prod.yml up -d --build
```

That is the whole first deploy. The one-shot `migrate` service runs
`alembic upgrade head` before the backend starts, and migration `0020`
applies `APP_DB_PASSWORD` / `SCANNER_DB_PASSWORD` from the `.env` you just
wrote — there is **no manual `ALTER ROLE` step**, contrary to what this
block used to say. Verify the roles and then run §4's checklist:

```bash
docker compose -f docker-compose.prod.yml ps          # every service Up/healthy
docker compose -f docker-compose.prod.yml logs migrate # "Running upgrade ... -> 0023"
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U postgres -d builders_stream -c "\du app_user scanner"
```

First `up` takes a few minutes — it builds two images from source and
pulls seven more (Caddy, Postgres, Redis, Prometheus, Grafana,
Alertmanager, node-exporter, cAdvisor).

## 4. Smoke-test checklist (run top-to-bottom on the box)

The CI suite can't docker-build or terminate TLS — this checklist is the
real-world verification of the production stack:

1. **Fail-fast proof**: temporarily set `JWT_SECRET=dev-only-secret-change-me`
   in `.env`, `up -d backend` → `docker compose -f docker-compose.prod.yml logs backend`
   shows the refusal listing the violation. Restore the real value, `up -d`.
2. **Readiness**: `docker compose -f docker-compose.prod.yml exec backend python -c
   "import urllib.request;print(urllib.request.urlopen('http://localhost:8000/ready').read())"`
   → `{"status": "ready", ...}`.
3. **TLS**: browse `https://<SITE_ADDRESS>/register` — padlock present;
   `curl -sI https://<SITE_ADDRESS> | grep -i strict-transport` shows HSTS.
4. **Product flow**: register a company → create a lead → mark it won
   (project auto-drafts) → build + calculate an estimate → **Generate PDF**
   completes (proves worker + documents volume) → upload a project document.
5. **Upload cap**: upload a file larger than 25 MiB → the UI surfaces a
   413 error.
6. **Client IP (ESIGN evidence)**: invite a client, approve the estimate
   from another device/network, then:
   `docker compose -f docker-compose.prod.yml exec postgres psql -U postgres -d builders_stream -c "SELECT ip_address FROM esignatures ORDER BY signed_at DESC LIMIT 1"`
   → the client's REAL public IP, not a `172.x` container address. If you
   see a container IP, the X-Forwarded-For chain is broken — do not accept
   signatures until fixed.
7. **Fail-open**: `docker compose -f docker-compose.prod.yml stop redis` →
   registration still works; backend logs show the rate-limiter WARNING →
   `start redis`.
8. **Forged webhook**: from the box (node:22-slim ships no curl/wget, so
   use node's own fetch):
   `docker compose -f docker-compose.prod.yml exec frontend node -e "fetch('http://backend:8000/webhooks/stripe',{method:'POST',headers:{'X-Stripe-Signature':'bad','Content-Type':'application/json'},body:'{}'}).then(r=>console.log(r.status))"`
   → a 4xx (and note the route isn't reachable from the internet at all).
9. **Reboot**: `sudo reboot` → stack comes back on its own (restart
   policies); queued jobs survived (Redis AOF).
10. **Backup + drill**: run the backup once by hand
    (`docker compose -f docker-compose.prod.yml run --rm db-backup`), then
    `./deploy/backup/restore-drill.sh "$BACKUP_DIR"` → `PASS`.
11. **Monitoring**: see §10 — Prometheus `/targets` all `UP`, Grafana's
    overview dashboard renders, and the backup tile shows an age rather
    than `No data`.

## 5. Upgrades

```bash
cd /opt/builders-stream
git pull
docker compose -f docker-compose.prod.yml up -d --build   # migrate gates backend
```

Then smoke-test items 2–4. **Rollback**: `git checkout <previous-tag>` +
`up -d --build`; if a migration was applied, restore the latest pre-upgrade
dump (`deploy/backup/restore.sh`).

**If `migrate` fails mid-upgrade**: compose reports "dependency failed to
start" and does NOT start the new backend — but the OLD backend and worker
keep running against a possibly half-migrated schema (Alembic revisions are
individually transactional, so revisions before the failing one are
committed). Do this, in order: `docker compose -f docker-compose.prod.yml
stop backend worker` (stop serving against an unknown schema), read
`docker compose -f docker-compose.prod.yml logs migrate` to identify the
failing revision, then either fix forward (`up -d --build`) or restore the
pre-upgrade dump.

## 6. Backups

- **Schedule** (host crontab):
  ```
  30 1 * * * cd /opt/builders-stream && docker compose -f docker-compose.prod.yml run --rm db-backup >> /var/log/builders-backup.log 2>&1
  ```
- What it does: `pg_dump -Fc` + documents tarball into `BACKUP_DIR`,
  pruned at 30 days (`docs/06` §4; nightly cadence = RPO ≤ 24h). A nonzero
  exit in the log/cron mail is the failure alert.
- **Off-host + encrypted** (docs/06 "stored off the Proxmox host", docs/07
  "backups encrypted at rest off-host") — host cron, your choice of:
  ```
  0 3 * * * rclone sync /opt/builders-stream-backups encrypted-remote:builders-backups
  # (rclone crypt remote — encrypts before upload), or restic backup, or
  # rsync -a --delete to another machine that is itself encrypted at rest.
  ```
- **Restore**: `./deploy/backup/restore.sh backups/db-<ts>.dump [backups/documents-<ts>.tar.gz]`.
- **Drill**: `./deploy/backup/restore-drill.sh` **quarterly** — restores the
  newest dump into a throwaway container and asserts, in order: non-zero
  companies and users; that **RLS survived** (at least one policy, and no
  table carrying `company_id` left with row security disabled); and that
  the dump's `alembic_version` is a revision this repo actually contains.
  Never touches the live database. This satisfies docs/06's "RTO documented
  and tested via a real restore drill".

  The RLS assertion is the one that earns the drill its place. Row counts
  passing while policies are missing is not a partial restore — it is every
  tenant reading every other tenant's data, from a backup that looked
  healthy. Verified against a real dump: policies do survive `pg_dump -Fc`
  today (39 of them, identical either side), and stripping them from a
  restored copy makes both assertions fire while the row counts stay
  perfect.

  The revision check is deliberately *not* "must equal head" — a backup is
  from the past, so the night after a migration deploys, last night's dump
  is legitimately one behind. It fails only on a revision this codebase has
  never heard of, and prints a NOTE (not a failure) when the dump is simply
  older than head.
- Retention note: the 7-year audit-log requirement (docs/07 §retention)
  rides on these database backups — the 30-day *file* rotation is fine
  because every dump contains the full, append-only audit_log table; do
  not add table-level pruning to audit_log.

## 6b. Network topology (single-box)

`docker-compose.prod.yml` defines two networks: **`edge`** (caddy +
frontend) and **`internal`** (frontend, backend, worker, scheduler,
migrate, postgres, redis, db-backup). Only caddy publishes ports. The
frontend straddles both — that is exactly what keeps Postgres, Redis, and
the backend unreachable from the host network and the internet
(docs/06 §6). Adding a `ports:` mapping to any internal service silently
undoes it, and would additionally make the backend's
`--forwarded-allow-ips=*` unsafe (see backend/Dockerfile's comment).

## 6c. The platform console (cross-tenant support access)

`/platform/*` is how you change a customer's plan, status or module
entitlements without a `psql` session (migration `0023`; the API is
[docs/05](05-api-specification.md) §10). It has no UI — an HTTP client is the
interface today.

**Creating the first operator.** There is no route that grants this, by
design: no runtime database role can write `platform_admins`, so the only
path is the table owner, which means a shell on the box.

```bash
docker compose -f docker-compose.prod.yml exec backend \
  python scripts/grant_platform_admin.py grant ops@yourcompany.com
docker compose -f docker-compose.prod.yml exec backend \
  python scripts/grant_platform_admin.py list
```

The account must already exist as an ordinary user, and **cannot sign in to
the console until it enrols TOTP** — `POST /platform/auth/mfa/enroll`, then
`/platform/auth/mfa/activate`. `grant` says so in its own output.

**Revoking is immediate.** `revoke <email>` takes effect on that operator's
very next request, not when their token expires — the grant is re-checked
every request rather than trusted from the token. Use it the moment someone
leaves; there is no session to hunt down.

**What an operator can and cannot do.** They can read every tenant and change
subscriptions and module overrides. They cannot write tenant business
data — the `platform_admin` role holds no grant on those tables — so this
access cannot be used to alter a customer's projects, invoices or estimates.
Entitlement changes land in the **target tenant's** audit log.

**One thing to know before setting a status by hand.** Doing so sets
`manual_status_override`, which deliberately stops `/webhooks/stripe`
applying Stripe's `status` to that subscription — otherwise the next routine
event would revert you. That means the row can then disagree with Stripe
indefinitely. Clear the override when the manual intervention is over.

## 7. Incident basics

- **Logs**: `docker compose -f docker-compose.prod.yml logs -f backend`
  (structured lines; unhandled 500s appear as `ERROR app unhandled error on
  <METHOD> <path>` with tracebacks). Same for `worker` / `scheduler` /
  `caddy` / `frontend`.
- **Worker dead-letters**: a Dramatiq message for an actor module missing
  from the worker's command line is silently dead-lettered — the module
  list in `docker-compose.prod.yml` must contain every module in
  `backend/app/tasks/` that defines an actor (the dev compose documents the
  incident that taught this).
- **Two most likely incidents**: (1) disk full — check `BACKUP_DIR` growth
  and `docker system df`; (2) certificate renewal failure — `logs caddy`,
  confirm port 80 still reachable from the internet. Both are alerted on
  (§10): `DiskUsageAbove85Percent` gives warning on the first, and Caddy
  failing to renew eventually shows as `ServiceDown` on the frontend path.
- **Dashboards**: §10 — Prometheus `/alerts` for what is currently firing,
  Grafana for what the box looked like when it started. Reachable over an
  SSH tunnel only.
- **Incident response skeleton** (docs/07 requires this in writing):
  detect (logs/user report) → assess scope (single tenant or all? data
  exposure?) → contain (`docker compose stop <svc>`; worst case `stop caddy`
  takes the site offline cleanly) → eradicate/recover (fix, redeploy,
  restore from backup if data was corrupted) → notify affected tenants if
  data was exposed → post-mortem in `docs/`.

## Error reporting

Off by default. `SENTRY_DSN` unset means `sentry-sdk` is never imported and
nothing leaves the box — that is the shipped default, not an oversight.

To turn it on:

1. Install the extra — the production image already does:
   `pip install -e ".[observability]"`
2. Set `SENTRY_DSN` in the server `.env`. Optionally
   `SENTRY_TRACES_SAMPLE_RATE` (default `0.0`; errors are the need,
   traces mostly spend quota on a single box).
3. Restart. All three processes pick it up — the API, the Dramatiq worker
   and the scheduler each initialise independently and tag themselves with
   `component=api|worker|scheduler`, because they fail in different ways
   and an event that cannot say which one it came from costs triage time.

**What it sends.** `app_env` as the environment, so staging noise never
lands in the production feed, and — on authenticated requests — the
**verified** `company_id` and `role`. Verified matters: the tag is set
after the membership check, not from the `X-Tenant-ID` header, which is
attacker-controlled. A company UUID is not personal data, and it is the
difference between "500s are up" and "one company cannot invoice".

**What it does not send.** `send_default_pii=False`, set explicitly rather
than relying on the SDK default, because that default silently changing in
a future release would put an ESIGN IP address — legal evidence under
docs/07 — into a third-party service. A `before_send` scrubber then strips
the `Authorization`, `Cookie` and `X-Tenant-ID` headers, and this
codebase's own secret names (`jwt_secret`,
`integration_token_encryption_key`, `stripe_webhook_secret`,
`smtp_password`, every database URL) out of stack-frame locals. Sentry's
default denylist covers `password` and `token`; it has never heard of the
Fernet key that decrypts every tenant's stored OAuth credentials.

Verified end to end against a real `sentry_sdk.init` with a stub
transport: the tags arrive, both secrets are redacted in the frame, and
neither secret string appears anywhere in the serialized payload — while
ordinary locals survive, so the traceback is still worth reading.

### The frontend half

Same defaults, one asymmetry worth knowing before an incident rather than
during one.

The Next **server** runtime reads `SENTRY_DSN` at run time, so it behaves
exactly like the backend — set it, restart, done. The **browser** runtime
cannot: `NEXT_PUBLIC_SENTRY_DSN` is inlined into the client bundle at build
time, so turning client-side reporting on needs a rebuild. That is inherent
to shipping a DSN to a browser, not a choice made here. (A Sentry DSN is a
write-only ingest key and is public by design, so being in the bundle is
not a leak.)

Browser events **tunnel through the app's own origin** (`/monitoring`,
configured in `next.config.ts`). This is load-bearing: the CSP pins
`connect-src 'self'`, so a direct POST to ingest.sentry.io would be
blocked, and the alternative — widening `connect-src` for every page — is
a worse trade than routing telemetry through a rewrite. It also survives
ad blockers, which routinely block known telemetry hosts. Cost: a total
frontend outage reports nothing through the tunnel; server-side errors
still report directly.

**Session Replay is deliberately off.** Replay records the DOM, and the
screens it would capture here are the e-signature flow, client names and
addresses, invoice amounts, and subcontractor compliance documents —
exactly what docs/07 treats as sensitive. Turning it on needs
`maskAllText` + `blockAllMedia` at minimum, and a decision written down.

URLs are scrubbed before events leave: the invitation-accept link's `id`
(which *is* the authorisation to join a company), and OAuth `code`/`state`.
`e2e/sentry-scrubbing.spec.ts` pins this, and found a real defect on its
first run — the key list had been written from memory as
`token`/`invitation`, so the actual `?id=` credential survived.

**If the DSN is set but the package is missing**, the app logs a warning
and carries on. Refusing to boot because telemetry is unavailable would
make the monitoring into the outage.

## 8. Split deployment (backend / middleware / frontend on separate machines)

`docker-compose.prod.yml` is the single-box default. When you want the
tiers on separate machines — backend API on one, frontend on another, the
async middleware tier deployable on its own lifecycle — use the three
standalone stacks under `deploy/split/`:

| Stack | File | Runs | Machine |
|---|---|---|---|
| Backend | `deploy/split/backend.compose.yml` | api-Caddy (TLS at `api.<domain>`), Postgres, Redis, migrate, FastAPI, db-backup | A |
| Middleware | `deploy/split/middleware.compose.yml` | Dramatiq worker + APScheduler scheduler | A (see placement note) |
| Frontend | `deploy/split/frontend.compose.yml` | Caddy (TLS at `app.<domain>`), Next.js | B |

No application code changes are involved — every cross-tier address is
already env-driven. The wiring rules:

1. **Once per machine**: `docker network create builders-net` (the stacks
   join it as an external network, so independently-deployed stacks on the
   same machine can still reach each other by service name).
2. **DNS**: `app.<domain>` → machine B, `api.<domain>` → machine A. Both
   machines need 80/443 open for Let's Encrypt.
2b. **Where the split `.env` lives**: `-f deploy/split/<stack>.compose.yml`
   makes `deploy/split/` the compose project directory, so both `${VAR}`
   interpolation and each service's `env_file: .env` resolve to
   **`deploy/split/.env`** — not the repo-root `.env`. Put each machine's
   env file there (and note `BACKUP_DIR`'s `./backups` default is likewise
   relative to `deploy/split/`; set it to an absolute path).
2c. **Document directory ownership**: the split stacks bind-mount
   `DOCUMENTS_DIR` from the host, and the backend image runs as uid 1000 —
   Docker does not chown bind mounts, so create it correctly first or every
   upload and PDF write fails with EACCES:
   `sudo mkdir -p /opt/builders-documents && sudo chown -R 1000:1000 /opt/builders-documents`
3. **Backend machine `.env`** (same table as §2, plus): `API_ADDRESS=api.<domain>`,
   `FRONTEND_SERVER_IP=<machine B's public IP>`,
   `DOCUMENTS_DIR=/opt/builders-documents` (a host path now, not a named
   volume, so the middleware stack can share it),
   `FRONTEND_BASE_URL=https://app.<domain>`.
4. **Frontend machine `.env`**: `SITE_ADDRESS=app.<domain>`,
   `NEXT_PUBLIC_API_URL=https://api.<domain>`.
5. **Access control**: the API Caddy (`deploy/Caddyfile.api`) allowlists
   `FRONTEND_SERVER_IP` — the frontend server is the only legitimate
   caller (the BFF proxies everything server-side; browsers never call the
   API), so the backend keeps its no-public-surface property even split.
6. **Client-IP chain**: `Caddyfile.api` declares the frontend server a
   trusted proxy so the BFF-forwarded `X-Forwarded-For` (the real end
   client) passes through. **Re-run smoke-test item 6 (ESIGN IP) after any
   topology change** — if the recorded IP is machine B's address, the
   trusted-proxies wiring is broken.
7. **Middleware placement**: run the middleware stack on machine A
   (joining `builders-net`, resolving `postgres`/`redis` by name, sharing
   `DOCUMENTS_DIR`). It deploys independently:
   `docker compose -f deploy/split/middleware.compose.yml up -d --build`
   restarts workers with zero API downtime. Running it on a **third**
   machine additionally requires (a) `DATABASE_URL`/`REDIS_URL` in its
   `.env` pointing at machine A over a **private network/VPN** — never
   publish Postgres/Redis publicly — and (b) shared document storage
   (NFS `DOCUMENTS_DIR`, or wait for the S3 seam): the PDF worker writes
   files the API serves.
8. **Backups** run on machine A exactly as §6 (the db-backup service lives
   in the backend stack).

Independent deploys, per tier:

```bash
# machine A — API only (middleware/frontend untouched):
docker compose -f deploy/split/backend.compose.yml up -d --build backend
# machine A — middleware only:
docker compose -f deploy/split/middleware.compose.yml up -d --build
# machine B — frontend only:
docker compose -f deploy/split/frontend.compose.yml up -d --build
```

Upgrade ordering when a change spans tiers: **backend first** (migrations
gate it), then middleware (same image lineage), then frontend (its
generated API types always trail the deployed backend, never lead it —
the OpenAPI snapshot workflow guarantees backward-compatible reads).

## 9. Deferred follow-ups (not blocking production)

| Item | Note |
|---|---|
| Alertmanager email delivery | The stack ships with alerts evaluating and grouping, but no notifier wired up — Alertmanager cannot read environment variables, so the SMTP details have to be literals someone fills in. Section 10 has the eight lines. Backup failure has an independent path (cron mail) regardless. |
| Monitoring on the split topology | Section 10's stack is defined in `docker-compose.prod.yml` (single box) only. Scraping across machines needs the metrics endpoints exposed between tiers, which is a network-policy decision, not a config one. |
| PostHog | Product analytics, needs an account decision. |
| Nonce-based strict CSP | Current CSP allows `'unsafe-inline'` scripts (Next.js bootstrap); a nonce pipeline removes it. |
| WAL archiving / pgBackRest | Only if RPO must shrink below 24h; adds archive monitoring burden. |
| Real Stripe/QuickBooks/FreshBooks clients | Needs credentials; on Stripe arrival, route `/webhooks/stripe` in the Caddyfile and use Stripe's own `t=...,v1=...` signature scheme (timestamp/replay protection) in a `RealStripeClient`. |
| Worker healthcheck | No HTTP surface today; would need a heartbeat file or queue-depth probe. |

## 10. Monitoring & alerting (Prometheus + Grafana)

docs/06 §5 asks for container health, host CPU/memory/disk, PostgreSQL
pool saturation and Dramatiq queue depth, with alerts on **service down,
backup failure, disk above 85%, and queue depth**. That is what this
section covers. It comes up with the rest of the stack — no profile, no
extra command — because monitoring you have to remember to start is
monitoring that is off when it matters.

### What runs, and what each part is for

| Service | Port (loopback only) | Provides |
|---|---|---|
| `prometheus` | 9090 | Scrapes everything below; evaluates `deploy/prometheus/alerts.yml`; 30 days / 4 GB retention |
| `grafana` | 3001 | The "Builders Stream — Overview" dashboard, provisioned read-only from `deploy/grafana/` |
| `alertmanager` | 9093 | Groups, dedupes and inhibits firing alerts; delivery is opt-in (below) |
| `node-exporter` | — | Host CPU, memory, disk, plus the backup textfile metrics |
| `cadvisor` | — | Per-container CPU, memory and restart counts |
| `backend` `/metrics` | — | Request rate/latency by **route template**, Postgres pool saturation, Dramatiq queue and dead-letter depth |

### Reaching the UIs

None of the three publish to the internet. They bind to `127.0.0.1`, so
the only way in is a tunnel from your laptop:

```bash
ssh -N -L 3001:127.0.0.1:3001 \
       -L 9090:127.0.0.1:9090 \
       -L 9093:127.0.0.1:9093 you@your-box
```

Then Grafana is `http://localhost:3001` (user `admin`, password
`GRAFANA_ADMIN_PASSWORD` from `.env`), Prometheus `http://localhost:9090`
(`/alerts` shows rule state, `/targets` shows scrape health), Alertmanager
`http://localhost:9093`.

This is the whole access-control story and it is deliberate. Caddy is the
only intended front door on this box; publishing Grafana would add a
second login surface to keep patched, and publishing Prometheus would put
an **unauthenticated** query API — one that can read every metric here —
on the public interface. `backend/tests/test_monitoring_config.py` asserts
all three stay bound to loopback, because the mistake is a one-character
diff.

### Turning on notifications

Alerts evaluate and appear in the Alertmanager UI out of the box; nothing
is emailed until you say where. Alertmanager does not expand environment
variables in its config file (a long-standing upstream position), so the
smarthost, sender and recipient have to be literals:

1. Put the mailbox password in `deploy/alertmanager/smtp_password` on the
   host (`chmod 600`; it is gitignored).
2. Uncomment the `email` receiver at the bottom of
   `deploy/alertmanager/alertmanager.yml` and fill in the four addresses.
3. Point both `receiver:` keys in the `route` block at `email`.
4. `docker compose -f docker-compose.prod.yml up -d --force-recreate alertmanager`

The password goes in a file rather than inline so it is not in this
repository, not in the container's environment, and not in `docker
inspect` output.

**Backup failure does not depend on this.** `deploy/backup/backup.sh`
exits nonzero on failure and host cron mails that output — an independent
path that keeps working when the monitoring stack itself is down.

### How backup alerting works

`backup.sh` writes three metrics into node-exporter's textfile collector
(a volume shared between the short-lived backup container and the
long-lived exporter), and three rules read them:

- `BackupFailed` — the last run exited nonzero. Written from an `EXIT`
  trap, so a killed `pg_dump` or a full disk reports failure too, not just
  a clean `exit 1`.
- `BackupStale` — no *successful* backup in 36 hours. This is the one that
  protects the RPO ≤ 24h in docs/06 §4, and the one that catches the case
  `BackupFailed` cannot: a cron entry someone removed, so the script never
  ran at all. A failed run deliberately preserves the previous success
  timestamp rather than resetting it — otherwise one bad night would hide
  the outage.
- `BackupMetricsMissing` — the textfile is gone entirely, which would
  otherwise leave both rules above sitting silently at "no data".

`deploy/backup/test-backup-metrics.sh` exercises all of this with a
stubbed `pg_dump` and runs in CI, because a broken metrics path is
invisible: backups keep succeeding and the alert simply never fires.

### Adding a metric

`backend/app/core/metrics.py` has the two rules that are not negotiable,
with the reasoning:

- **Never label a series with a tenant.** No `company_id`, no `user_id`.
  Cardinality (one series per customer, forever, including churned ones)
  and disclosure (Grafana is a different trust boundary; per-tenant request
  counts tell anyone with dashboard access which customers are large and
  which are failing). Sentry tagging events with `company_id` is
  deliberately different — one bounded record a human reads during an
  incident, not an unbounded time series.
- **Label with the route template, never the raw path.**
  `/projects/{project_id}` is one series; the paths behind it are one per
  project. Unmatched requests collapse into a single `<unmatched>` bucket
  so a 404 scan cannot grow Prometheus's memory from outside.

`backend/tests/test_metrics.py` enforces both, and
`backend/tests/test_monitoring_config.py` checks every metric name in the
alert rules and the Grafana dashboard against the app's live registry —
an alert naming a metric that does not exist never fires and never errors.

### Smoke test (add to Section 4's checklist)

11. **Monitoring**: over the tunnel above, Prometheus `/targets` shows all
    five jobs `UP`; Grafana's "Builders Stream — Overview" renders request
    rate and pool utilisation; after running the manual backup from item
    10, the "Since last successful backup" tile shows minutes, not `No
    data`.

    If the `cadvisor` target is down or its panels are empty, add
    `privileged: true` to that service and recreate it. The compose file
    deliberately omits it — a privileged container would have more access
    to this host than the database does — and this step is how you find
    out whether your kernel needs it.
