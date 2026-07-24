# Production Deployment Runbook

Self-hosted Docker Compose deployment (the topology `docs/03` and `docs/06`
specify: one VM, Caddy terminating TLS, everything else on an internal
Docker network). The stack file is `docker-compose.prod.yml`; the dev
`docker-compose.yml` is unchanged and remains the local-development stack.

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
| `REDIS_URL` | `redis://redis:6379/0` |
| `TZ` | your zone, e.g. `America/Chicago` |
| `BACKUP_DIR` | host path for backups, e.g. `/opt/builders-stream-backups` |
| `SMTP_*` | set to enable invitation emails; unset = recording fake (no email leaves the box) |

**`app_user` password — first-deploy tripwire.** Migration `0001` creates
the runtime role with the password literally hardcoded as `'app_password'`.
After the first `up` (migrations applied), set the real one:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U postgres -d builders_stream \
  -c "ALTER ROLE app_user PASSWORD '<APP_DB_PASSWORD>'"
docker compose -f docker-compose.prod.yml restart backend worker
```

(The boot validator rejects `DATABASE_URL` containing `app_password`, so
skipping this step cannot go unnoticed — the backend won't start until the
URL carries the real password, and the real password won't work until the
ALTER ROLE ran.)

## 3. First deploy

```bash
git clone <repo> /opt/builders-stream && cd /opt/builders-stream
cp .env.example .env   # then edit per the table above
docker compose -f docker-compose.prod.yml up -d --build
# migrations run automatically (the one-shot `migrate` service gates the backend)
# then do the ALTER ROLE step above, then run the smoke-test checklist
```

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
8. **Forged webhook**: from the box,
   `docker compose -f docker-compose.prod.yml exec frontend sh -c "wget -qO- --post-data='{}' --header='X-Stripe-Signature: bad' http://backend:8000/webhooks/stripe"`
   → rejected (and note the route isn't reachable from the internet at all).
9. **Reboot**: `sudo reboot` → stack comes back on its own (restart
   policies); queued jobs survived (Redis AOF).
10. **Backup + drill**: run the backup once by hand
    (`docker compose -f docker-compose.prod.yml run --rm db-backup`), then
    `./deploy/backup/restore-drill.sh "$BACKUP_DIR"` → `PASS`.

## 5. Upgrades

```bash
cd /opt/builders-stream
git pull
docker compose -f docker-compose.prod.yml up -d --build   # migrate gates backend
```

Then smoke-test items 2–4. **Rollback**: `git checkout <previous-tag>` +
`up -d --build`; if a migration was applied, restore the latest pre-upgrade
dump (`deploy/backup/restore.sh`).

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
  newest dump into a throwaway container and asserts real data + schema
  head. Never touches the live database. This satisfies docs/06's "RTO
  documented and tested via a real restore drill".
- Retention note: the 7-year audit-log requirement (docs/07 §retention)
  rides on these database backups — the 30-day *file* rotation is fine
  because every dump contains the full, append-only audit_log table; do
  not add table-level pruning to audit_log.

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
  confirm port 80 still reachable from the internet.
- **Incident response skeleton** (docs/07 requires this in writing):
  detect (logs/user report) → assess scope (single tenant or all? data
  exposure?) → contain (`docker compose stop <svc>`; worst case `stop caddy`
  takes the site offline cleanly) → eradicate/recover (fix, redeploy,
  restore from backup if data was corrupted) → notify affected tenants if
  data was exposed → post-mortem in `docs/`.

## 8. Deferred follow-ups (not blocking production)

| Item | Note |
|---|---|
| Sentry | `pip install sentry-sdk[fastapi]`, then in `app/main.py`: `if settings.sentry_dsn: sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.1)` + a `sentry_dsn` setting; same env-gated pattern in the frontend. |
| Prometheus/Grafana + alerting | docs/06 §5's full stack (service-down, backup-failure, disk >85%, queue-depth alerts). Until then: restart policies + cron mail + `docker compose ps`. |
| PostHog | Product analytics, needs an account decision. |
| Nonce-based strict CSP | Current CSP allows `'unsafe-inline'` scripts (Next.js bootstrap); a nonce pipeline removes it. |
| WAL archiving / pgBackRest | Only if RPO must shrink below 24h; adds archive monitoring burden. |
| Real Stripe/QuickBooks/FreshBooks clients | Needs credentials; on Stripe arrival, route `/webhooks/stripe` in the Caddyfile and use Stripe's own `t=...,v1=...` signature scheme (timestamp/replay protection) in a `RealStripeClient`. |
| Worker healthcheck | No HTTP surface today; would need a heartbeat file or queue-depth probe. |
