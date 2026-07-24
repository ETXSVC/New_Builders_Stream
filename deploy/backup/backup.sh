#!/usr/bin/env bash
# Nightly backup: Postgres custom-format dump + documents tarball into
# /backups (a HOST bind mount — see docker-compose.prod.yml's db-backup
# service), pruning both at 30 days (docs/06 §4 retention). Driven by host
# cron:
#   30 1 * * * cd /opt/builders-stream && docker compose -f docker-compose.prod.yml run --rm db-backup
# A nonzero exit surfaces in cron mail / the systemd journal — that IS the
# backup-failure alert until real monitoring lands.
#
# Off-host sync + at-rest encryption of the synced copy are the host's
# job (rclone crypt / restic / rsync — examples in
# docs/11-production-deployment.md); RPO <= 24h per docs/06 comes from the
# nightly cadence.
set -euo pipefail

STAMP="$(date -u +%Y%m%d-%H%M%S)"
BACKUP_ROOT="/backups"
RETENTION_DAYS=30

echo "[backup] dumping database ${PGDATABASE} ..."
pg_dump --format=custom --file="${BACKUP_ROOT}/db-${STAMP}.dump"

# A pg_dump that "succeeds" against the wrong/empty database still exits 0
# — assert a plausible floor so a silently broken backup fails the cron job
# tonight instead of surfacing at the next quarterly drill.
DUMP_BYTES="$(stat -c %s "${BACKUP_ROOT}/db-${STAMP}.dump")"
if [ "${DUMP_BYTES}" -lt 10240 ]; then
  echo "[backup] FAIL: dump is only ${DUMP_BYTES} bytes — refusing to treat as a valid backup" >&2
  exit 1
fi

echo "[backup] archiving document storage ..."
tar czf "${BACKUP_ROOT}/documents-${STAMP}.tar.gz" -C /data/documents .

echo "[backup] pruning backups older than ${RETENTION_DAYS} days ..."
find "${BACKUP_ROOT}" -maxdepth 1 -name 'db-*.dump' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_ROOT}" -maxdepth 1 -name 'documents-*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete

echo "[backup] done: db-${STAMP}.dump + documents-${STAMP}.tar.gz"
