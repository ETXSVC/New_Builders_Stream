#!/usr/bin/env bash
# Restore DRILL (docs/06 §4: RTO must be documented AND tested): restores
# the newest dump into a THROWAWAY Postgres container and asserts the data
# is really there — safe to run against production at any time; it never
# touches the live database. Run quarterly (see
# docs/11-production-deployment.md).
#
#   ./deploy/backup/restore-drill.sh [backup-dir]   # default ./backups
set -euo pipefail

BACKUP_DIR="${1:-./backups}"
LATEST_DUMP="$(ls -1t "${BACKUP_DIR}"/db-*.dump 2>/dev/null | head -1 || true)"
[ -n "${LATEST_DUMP}" ] || { echo "[drill] no db-*.dump found in ${BACKUP_DIR}" >&2; exit 1; }
echo "[drill] using ${LATEST_DUMP}"

CONTAINER="restore-drill-$$"
cleanup() { docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true; }
trap cleanup EXIT

docker run -d --name "${CONTAINER}" \
  -e POSTGRES_USER=drill -e POSTGRES_PASSWORD=drill -e POSTGRES_DB=drill \
  -v "$(cd "$(dirname "${LATEST_DUMP}")" && pwd)/$(basename "${LATEST_DUMP}"):/drill/db.dump:ro" \
  postgres:16 >/dev/null

echo "[drill] waiting for throwaway postgres ..."
for _ in $(seq 1 30); do
  docker exec "${CONTAINER}" pg_isready -U drill >/dev/null 2>&1 && break
  sleep 2
done

echo "[drill] restoring ..."
docker exec "${CONTAINER}" pg_restore --no-owner --username=drill --dbname=drill /drill/db.dump

echo "[drill] asserting restored data ..."
COMPANIES="$(docker exec "${CONTAINER}" psql -U drill -d drill -tAc 'SELECT count(*) FROM companies')"
USERS="$(docker exec "${CONTAINER}" psql -U drill -d drill -tAc 'SELECT count(*) FROM users')"
VERSION="$(docker exec "${CONTAINER}" psql -U drill -d drill -tAc 'SELECT version_num FROM alembic_version')"
echo "[drill] companies=${COMPANIES} users=${USERS} alembic_version=${VERSION}"
[ "${COMPANIES}" -gt 0 ] || { echo "[drill] FAIL: zero companies in restored dump" >&2; exit 1; }
[ "${USERS}" -gt 0 ] || { echo "[drill] FAIL: zero users in restored dump" >&2; exit 1; }
[ -n "${VERSION}" ] || { echo "[drill] FAIL: no alembic_version row" >&2; exit 1; }

echo "[drill] PASS — backup ${LATEST_DUMP} restores cleanly."
