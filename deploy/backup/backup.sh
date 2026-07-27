#!/usr/bin/env bash
# Nightly backup: Postgres custom-format dump + documents tarball into
# /backups (a HOST bind mount — see docker-compose.prod.yml's db-backup
# service), pruning both at 30 days (docs/06 §4 retention). Driven by host
# cron:
#   30 1 * * * cd /opt/builders-stream && docker compose -f docker-compose.prod.yml run --rm db-backup
# A nonzero exit surfaces in cron mail / the systemd journal. That path is
# kept deliberately: it is independent of the monitoring stack, so a
# Prometheus that is itself down does not take backup alerting with it.
#
# The second path is the textfile block at the bottom of this script,
# scraped by node-exporter and alerted on by deploy/prometheus/alerts.yml
# (BackupFailed / BackupStale / BackupMetricsMissing).
#
# Off-host sync + at-rest encryption of the synced copy are the host's
# job (rclone crypt / restic / rsync — examples in
# docs/11-production-deployment.md); RPO <= 24h per docs/06 comes from the
# nightly cadence.
set -euo pipefail

STAMP="$(date -u +%Y%m%d-%H%M%S)"
# Overridable only so the metrics paths below can be exercised outside a
# container — the compose service mounts exactly these defaults, and
# nothing in production sets either variable.
BACKUP_ROOT="${BACKUP_ROOT:-/backups}"
DOCUMENTS_DIR="${DOCUMENTS_DIR:-/data/documents}"
RETENTION_DAYS=30
# node-exporter's --collector.textfile.directory, shared with this
# container as the `metrics_textfile` volume. Unset when this script is
# run outside the compose stack (the restore drill, a manual dump on a box
# with no monitoring), in which case every metrics write is skipped rather
# than failing the backup — the dump is the deliverable, the metric is
# commentary on it.
TEXTFILE_DIR="${TEXTFILE_COLLECTOR_DIR:-}"
METRICS_FILE="${TEXTFILE_DIR:+${TEXTFILE_DIR}/builders_stream_backup.prom}"

# node-exporter reads this directory on every scrape, so a partially
# written file is a parse error at exactly the wrong moment. Writing to a
# temporary file and renaming makes the swap atomic — the collector either
# sees the whole old file or the whole new one. This is the documented
# requirement for the textfile collector, not a precaution.
write_metrics() {
  local success="$1" last_success_ts="$2"
  [ -n "${METRICS_FILE}" ] || return 0
  local tmp="${METRICS_FILE}.$$.tmp"
  {
    echo "# HELP buildersstream_backup_last_run_success Whether the most recent backup run exited cleanly (1) or failed (0)."
    echo "# TYPE buildersstream_backup_last_run_success gauge"
    echo "buildersstream_backup_last_run_success ${success}"
    echo "# HELP buildersstream_backup_last_run_timestamp_seconds Unix time this backup script last finished, success or not."
    echo "# TYPE buildersstream_backup_last_run_timestamp_seconds gauge"
    echo "buildersstream_backup_last_run_timestamp_seconds $(date -u +%s)"
    if [ -n "${last_success_ts}" ]; then
      echo "# HELP buildersstream_backup_last_success_timestamp_seconds Unix time of the last backup that completed successfully."
      echo "# TYPE buildersstream_backup_last_success_timestamp_seconds gauge"
      echo "buildersstream_backup_last_success_timestamp_seconds ${last_success_ts}"
    fi
  } >"${tmp}"
  mv -f "${tmp}" "${METRICS_FILE}"
}

# The age of the last SUCCESS has to survive a failed run, or a single bad
# night would reset the clock BackupStale measures and hide the outage it
# exists to catch. So a failure rewrites the success/failure flag while
# carrying the previous success timestamp through unchanged.
# Written as `if`, not `[ ... ] && [ ... ] || return`: under `set -e` a
# failing `&&` list is itself a failing command, which would abort the
# script (or, worse, the EXIT trap) instead of just skipping the read.
previous_success_ts() {
  if [ -n "${METRICS_FILE}" ] && [ -f "${METRICS_FILE}" ]; then
    awk '/^buildersstream_backup_last_success_timestamp_seconds /{print $2}' "${METRICS_FILE}"
  fi
}

# An EXIT trap rather than an `|| write_metrics 0` after each command: with
# `set -e` in force, any failing command aborts the script immediately, so
# only a trap actually runs on the paths that matter. It also covers the
# ones no explicit handler would — a SIGKILLed pg_dump, a full disk during
# tar, the container being stopped mid-run.
report_failure_on_exit() {
  local code="$?"
  if [ "${code}" -ne 0 ]; then
    write_metrics 0 "$(previous_success_ts)"
    echo "[backup] FAILED (exit ${code})" >&2
  fi
}
trap report_failure_on_exit EXIT

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
tar czf "${BACKUP_ROOT}/documents-${STAMP}.tar.gz" -C "${DOCUMENTS_DIR}" .

echo "[backup] pruning backups older than ${RETENTION_DAYS} days ..."
find "${BACKUP_ROOT}" -maxdepth 1 -name 'db-*.dump' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_ROOT}" -maxdepth 1 -name 'documents-*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete

write_metrics 1 "$(date -u +%s)"

echo "[backup] done: db-${STAMP}.dump + documents-${STAMP}.tar.gz"
