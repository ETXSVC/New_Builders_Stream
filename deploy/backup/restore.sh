#!/usr/bin/env bash
# Full restore from a backup pair produced by backup.sh. Run from the
# compose project directory ON THE HOST:
#
#   ./deploy/backup/restore.sh backups/db-YYYYmmdd-HHMMSS.dump [backups/documents-YYYYmmdd-HHMMSS.tar.gz]
#
# Stops the app services (leaves postgres/redis up), restores the database
# with --clean, untars documents into the volume, restarts, and verifies
# /ready. DESTRUCTIVE by design — it replaces current data with the backup.
set -euo pipefail

COMPOSE=(docker compose -f docker-compose.prod.yml)
DB_DUMP="${1:?usage: restore.sh <db-dump> [documents-tarball]}"
DOCS_TARBALL="${2:-}"

echo "[restore] stopping application services ..."
"${COMPOSE[@]}" stop backend worker scheduler frontend

echo "[restore] restoring database from ${DB_DUMP} ..."
"${COMPOSE[@]}" run --rm --no-deps \
  -v "$(pwd)/${DB_DUMP}:/restore/db.dump:ro" \
  --entrypoint bash db-backup -c \
  'pg_restore --clean --if-exists --no-owner --dbname="$PGDATABASE" /restore/db.dump'

if [ -n "${DOCS_TARBALL}" ]; then
  echo "[restore] restoring documents from ${DOCS_TARBALL} ..."
  "${COMPOSE[@]}" run --rm --no-deps \
    -v "$(pwd)/${DOCS_TARBALL}:/restore/documents.tar.gz:ro" \
    -v documents_data:/data/documents \
    --entrypoint bash db-backup -c \
    'rm -rf /data/documents/* && tar xzf /restore/documents.tar.gz -C /data/documents'
fi

echo "[restore] starting application services ..."
"${COMPOSE[@]}" up -d backend worker scheduler frontend

echo "[restore] waiting for readiness ..."
for _ in $(seq 1 30); do
  if "${COMPOSE[@]}" exec backend python -c \
    "import urllib.request; urllib.request.urlopen('http://localhost:8000/ready', timeout=3)" 2>/dev/null; then
    echo "[restore] done — backend is ready."
    exit 0
  fi
  sleep 2
done
echo "[restore] backend did not become ready — check: docker compose -f docker-compose.prod.yml logs backend" >&2
exit 1
