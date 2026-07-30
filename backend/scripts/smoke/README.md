# Smoke checklist automation

Two scripts that automate most of [the deployment runbook's §4
checklist](../../../docs/11-production-deployment.md). They exist because
that checklist is the **only** verification of things CI structurally
cannot reach: CI never docker-builds and terminates TLS, never reboots a
host, and — the one that matters most — never sends a request over a real
network hop, so it cannot prove the client IP survives to the e-signature
record.

Running §4 by hand takes the better part of an hour and is easy to do
partially. These cover the mechanical two-thirds so the remaining items
get real attention.

## The two scripts

| Script | Runbook items | Needs |
|---|---|---|
| `check_production_config.py` | 1 | nothing — no database, no stack |
| `run_checklist.py` | 2, 4, 5, 6, 8, and the application half of 11 | a running stack + `MIGRATIONS_DATABASE_URL` |

### `check_production_config.py`

```bash
python scripts/smoke/check_production_config.py
```

Thirty seconds, no dependencies on a running anything. Run it **first** on
a new box: it proves the guard protecting every other secret in your
`.env` actually fires, across all ten violation classes.

Its first assertion is the one usually left out — that a *correct*
production config still boots. A validator that refused everything would
pass all ten rejection cases and be worthless.

### `run_checklist.py`

```bash
# dev stack (the one-shot `migrate` service creates the schema)
docker compose up -d
python scripts/smoke/run_checklist.py

# production stack, from the box
SMOKE_BASE_URL=http://localhost:8000 python scripts/smoke/run_checklist.py
```

40 assertions covering: readiness and the `/health` split, the full
product flow (register → lead through its mandatory stage spine → won →
auto-drafted project → estimate → calculate → **PDF via the worker** →
document upload), the 413 upload cap, `X-Forwarded-For` reaching
`esignatures.ip_address`, forged Stripe webhook rejection, and the
`/metrics` labelling invariants.

**It writes real rows.** A company, a lead, a project, an estimate, a
signed document. Point it at a stack whose database you are willing to
dirty — a dev stack, or production *before* it holds customer data.

## What still needs a human

These are the items no script can honestly cover, and the reason §4 exists
as a checklist rather than a test suite:

- **item 3** — TLS and HSTS, needs a real certificate
- **item 7** — Redis fail-open, needs Redis stopped
- **item 9** — reboot persistence, needs the box rebooted
- **item 10** — backup and restore drill → `deploy/backup/restore-drill.sh`
- **item 11** — Prometheus targets and Grafana panels, needs the
  monitoring containers and an SSH tunnel

## Why these are scripts and not pytest tests

They talk to a *deployed* stack over the network, so they need something
running before they mean anything — which is the opposite of the test
suite's contract, where `conftest.py` builds its own database and nothing
external is assumed. Putting them under `tests/` would either make the
suite fail without a stack, or make them skip silently and prove nothing.

The distinction is the same one the runbook draws: CI proves the code is
right, this proves the *deployment* is.
