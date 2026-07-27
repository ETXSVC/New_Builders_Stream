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
# Used only for the "is this revision one we know?" check below; the drill
# still works without it (run from somewhere other than the repo).
MIGRATIONS_DIR="${MIGRATIONS_DIR:-backend/migrations/versions}"
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

# The dump's GRANT/RLS statements reference app_user, but pg_dump -Fc
# carries no cluster-level roles — without this the restore errors on every
# grant and the drill fails on a perfectly good backup.
echo "[drill] creating referenced roles ..."
docker exec "${CONTAINER}" psql -U drill -d drill -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN CREATE ROLE app_user; END IF; END \$\$;"

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

# Row counts are the easy half. This is the half that matters.
#
# Every tenant table's isolation is a Postgres RLS policy, not application
# code — so a restore that brings back all 112 users but drops the policies
# is not a degraded restore, it is a total tenant-isolation failure in
# which every company reads every other company's data. It would also look
# perfect by every assertion above.
#
# A first drill run against a real dump confirmed policies DO survive
# `pg_dump -Fc` (39 of them, identical either side). The point of asserting
# it is the day that stops being true — a Postgres major upgrade, a
# `--section` flag added to backup.sh, someone switching to `--data-only`
# to make the dumps smaller. Each of those produces a restore that passes
# a row-count check and silently unifies every tenant.
echo "[drill] asserting RLS survived the round trip ..."
POLICIES="$(docker exec "${CONTAINER}" psql -U drill -d drill -tAc \
  "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")"
UNPROTECTED="$(docker exec "${CONTAINER}" psql -U drill -d drill -tAc \
  "SELECT count(*) FROM pg_class c
     JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND c.relrowsecurity = false
      AND EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public'
                     AND table_name = c.relname
                     AND column_name = 'company_id')")"
echo "[drill] rls_policies=${POLICIES} tenant_tables_without_rls=${UNPROTECTED}"
[ "${POLICIES}" -gt 0 ] || {
  echo "[drill] FAIL: the restored database has NO row-level security policies." >&2
  echo "[drill]       Rows came back; tenant isolation did not. Do not use this backup." >&2
  exit 1
}
[ "${UNPROTECTED}" -eq 0 ] || {
  echo "[drill] FAIL: ${UNPROTECTED} table(s) carry company_id but have RLS disabled after restore." >&2
  exit 1
}

# Is this dump's schema one this repository knows about?
#
# Deliberately NOT "must equal head": a backup is by definition from the
# past, so the night after a migration deploys, last night's dump is
# legitimately one revision behind and failing on that would be a false
# alarm that trains people to ignore the drill. What is worth catching is a
# dump whose revision this repo has never heard of — a backup from a
# different deployment, or from a branch that never merged, which cannot be
# migrated forward at all.
if [ -d "${MIGRATIONS_DIR}" ]; then
  if ls "${MIGRATIONS_DIR}"/"${VERSION}"_*.py >/dev/null 2>&1; then
    HEAD="$(ls -1 "${MIGRATIONS_DIR}"/*.py | sed 's#.*/##; s/_.*//' | sort -n | tail -1)"
    if [ "${VERSION}" = "${HEAD}" ]; then
      echo "[drill] schema is at head (${HEAD})."
    else
      echo "[drill] NOTE: dump is at ${VERSION}, repo head is ${HEAD} — restore then \`alembic upgrade head\`."
    fi
  else
    echo "[drill] FAIL: revision ${VERSION} is not a migration in ${MIGRATIONS_DIR}." >&2
    echo "[drill]       This dump did not come from this codebase." >&2
    exit 1
  fi
else
  echo "[drill] NOTE: ${MIGRATIONS_DIR} not present — skipping the revision check."
fi

echo "[drill] PASS — backup ${LATEST_DUMP} restores cleanly, with tenant isolation intact."
