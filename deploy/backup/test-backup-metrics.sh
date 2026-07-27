#!/usr/bin/env bash
# Exercises backup.sh's textfile-metrics paths with a stubbed pg_dump, so
# the backup-failure alert is verified rather than assumed.
#
# It is worth having a test for this specific thing because the failure
# mode is invisible: if the EXIT trap or the atomic rename is wrong, the
# backup itself still works perfectly every night, and the only symptom is
# that BackupFailed never fires on the one night it should. Nothing else
# in the stack would notice.
#
# Run by `deploy-config` in .github/workflows/backend-ci.yml. No Docker,
# no Postgres — `pg_dump` is a shell function on PATH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

mkdir -p "${WORK}/backups" "${WORK}/documents" "${WORK}/metrics" "${WORK}/bin"
echo "a document" >"${WORK}/documents/hello.txt"

METRICS="${WORK}/metrics/builders_stream_backup.prom"
fail() { echo "FAIL: $*" >&2; exit 1; }

metric_value() {
  awk -v k="$1" '$1 == k {print $2}' "${METRICS}"
}

run_backup() {
  BACKUP_ROOT="${WORK}/backups" \
  DOCUMENTS_DIR="${WORK}/documents" \
  TEXTFILE_COLLECTOR_DIR="${WORK}/metrics" \
  PGDATABASE=stub \
  PATH="${WORK}/bin:${PATH}" \
    bash "${HERE}/backup.sh"
}

# --- a dump that passes the size floor -------------------------------- #
# backup.sh calls `pg_dump --format=custom --file=<path>`; the stub only
# has to honour --file and produce something over the 10 KiB floor.
cat >"${WORK}/bin/pg_dump" <<'STUB'
#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in --file=*) out="${arg#--file=}" ;; esac
done
head -c 65536 /dev/zero >"$out"
STUB
chmod +x "${WORK}/bin/pg_dump"

echo "[test] successful run publishes success + last-success timestamp"
run_backup >/dev/null
[ -f "${METRICS}" ] || fail "no metrics file written"
[ "$(metric_value buildersstream_backup_last_run_success)" = "1" ] \
  || fail "expected last_run_success=1"
FIRST_SUCCESS="$(metric_value buildersstream_backup_last_success_timestamp_seconds)"
[ -n "${FIRST_SUCCESS}" ] || fail "no last_success_timestamp after a successful run"
# node-exporter parses this file on every scrape; a stray temp file left
# beside it would be parsed too.
[ "$(find "${WORK}/metrics" -name '*.tmp' | wc -l)" = "0" ] \
  || fail "a .tmp file survived the atomic rename"

# --- a dump that silently produces almost nothing --------------------- #
# The size floor is what catches a pg_dump pointed at an empty or wrong
# database: it exits 0, so only the floor makes it a failure.
cat >"${WORK}/bin/pg_dump" <<'STUB'
#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in --file=*) out="${arg#--file=}" ;; esac
done
echo "too small" >"$out"
STUB

echo "[test] failed run flips the flag but PRESERVES the last-success age"
if run_backup >/dev/null 2>&1; then
  fail "backup.sh exited 0 on an undersized dump"
fi
[ "$(metric_value buildersstream_backup_last_run_success)" = "0" ] \
  || fail "expected last_run_success=0 after a failure"
# The whole point: BackupStale measures age since the last SUCCESS. If a
# failed run reset or dropped this, one bad night would hide the outage.
[ "$(metric_value buildersstream_backup_last_success_timestamp_seconds)" = "${FIRST_SUCCESS}" ] \
  || fail "a failed run clobbered the last-success timestamp"

# --- the trap must cover crashes, not just clean `exit 1` ------------- #
cat >"${WORK}/bin/pg_dump" <<'STUB'
#!/usr/bin/env bash
kill -9 $$
STUB

echo "[test] a killed pg_dump still reports failure"
run_backup >/dev/null 2>&1 && fail "backup.sh exited 0 after pg_dump was killed"
[ "$(metric_value buildersstream_backup_last_run_success)" = "0" ] \
  || fail "expected last_run_success=0 after a killed subprocess"

# --- no textfile directory: metrics skipped, backup still works ------- #
echo "[test] runs without a textfile directory (restore drill, no monitoring)"
cat >"${WORK}/bin/pg_dump" <<'STUB'
#!/usr/bin/env bash
for arg in "$@"; do
  case "$arg" in --file=*) out="${arg#--file=}" ;; esac
done
head -c 65536 /dev/zero >"$out"
STUB
BACKUP_ROOT="${WORK}/backups" DOCUMENTS_DIR="${WORK}/documents" PGDATABASE=stub \
  PATH="${WORK}/bin:${PATH}" bash "${HERE}/backup.sh" >/dev/null \
  || fail "backup.sh requires TEXTFILE_COLLECTOR_DIR"

echo "[test] all backup-metrics assertions passed"
