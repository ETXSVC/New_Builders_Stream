#!/usr/bin/env bash
# Full restore from a backup pair produced by backup.sh. Run from the
# compose project directory ON THE HOST:
#
#   ./deploy/backup/restore.sh /opt/builders-stream-backups/db-<ts>.dump \
#                              [/opt/builders-stream-backups/documents-<ts>.tar.gz]
#
# Paths may be absolute or relative (BACKUP_DIR is typically outside the
# project directory, so absolute is the common case — they are resolved to
# absolute before being handed to docker).
#
# Stops the app services (leaves postgres/redis up), restores the database
# with --clean, replaces the documents volume contents, restarts, and
# verifies /ready. DESTRUCTIVE by design — it replaces current data with
# the backup.
set -euo pipefail

COMPOSE=(docker compose -f docker-compose.prod.yml)
DB_DUMP="${1:?usage: restore.sh <db-dump> [documents-tarball]}"
DOCS_TARBALL="${2:-}"

# Resolve to absolute host paths — a bare "$(pwd)/$arg" breaks the moment
# the caller passes an absolute path (BACKUP_DIR usually is one), silently
# creating an empty directory mount instead of the dump file.
DB_DUMP="$(cd "$(dirname "${DB_DUMP}")" && pwd)/$(basename "${DB_DUMP}")"
[ -f "${DB_DUMP}" ] || { echo "[restore] no such dump: ${DB_DUMP}" >&2; exit 1; }
if [ -n "${DOCS_TARBALL}" ]; then
  DOCS_TARBALL="$(cd "$(dirname "${DOCS_TARBALL}")" && pwd)/$(basename "${DOCS_TARBALL}")"
  [ -f "${DOCS_TARBALL}" ] || { echo "[restore] no such tarball: ${DOCS_TARBALL}" >&2; exit 1; }
fi

echo "[restore] stopping application services ..."
"${COMPOSE[@]}" stop backend worker scheduler frontend

echo "[restore] restoring database from ${DB_DUMP} ..."
"${COMPOSE[@]}" run --rm --no-deps \
  -v "${DB_DUMP}:/restore/db.dump:ro" \
  --entrypoint bash db-backup -c \
  'pg_restore --clean --if-exists --no-owner --dbname="$PGDATABASE" /restore/db.dump'

if [ -n "${DOCS_TARBALL}" ]; then
  echo "[restore] restoring documents from ${DOCS_TARBALL} ..."
  # NOT `compose run db-backup` here: that service already mounts the
  # documents volume READ-ONLY, and re-mounting the same target would
  # either collide or silently keep the :ro mount. A plain `docker run`
  # against the same named volume is unambiguous. The volume name is
  # <project>_documents_data; the project name defaults to the directory
  # name, so it is derived here rather than hardcoded.
  PROJECT="$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')"
  docker run --rm \
    -v "${PROJECT}_documents_data:/data/documents" \
    -v "${DOCS_TARBALL}:/restore/documents.tar.gz:ro" \
    postgres:16 bash -c \
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
