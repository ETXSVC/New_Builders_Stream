"""The monitoring stack's configuration, checked against reality.

Prometheus, Alertmanager and Grafana all fail the same quiet way. An alert
naming a metric that does not exist never fires and never errors. A scrape
job pointed at a renamed service reports "no data", which on a dashboard
looks a lot like "nothing is wrong". A Grafana panel querying a typo shows
an empty graph. None of it turns anything red; it just means that on the
night something breaks, nobody is told.

So the config files are not left to be verified by deploying them. This
module cross-checks them against the two things that can contradict them:
the metric names the application actually exports (imported live from
app/core/metrics.py, not transcribed) and the service names
docker-compose.prod.yml actually defines.

What it deliberately does NOT do is re-check syntax. `promtool check
config`, `promtool check rules` and `amtool check-config` do that in the
`deploy-config` CI job, where the real binaries are available in their
own images. The division is the point: syntax errors are loud (the service
refuses to start), and this module covers the mistakes that stay silent
forever.
"""
import json
import pathlib
import re

import pytest
import yaml

from app.core.metrics import REGISTRY

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
PROMETHEUS_YML = REPO_ROOT / "deploy" / "prometheus" / "prometheus.yml"
ALERTS_YML = REPO_ROOT / "deploy" / "prometheus" / "alerts.yml"
ALERTMANAGER_YML = REPO_ROOT / "deploy" / "alertmanager" / "alertmanager.yml"
DASHBOARD_JSON = REPO_ROOT / "deploy" / "grafana" / "dashboards" / "builders-stream.json"
DATASOURCES_YML = (
    REPO_ROOT / "deploy" / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
)
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
BACKUP_SCRIPT = REPO_ROOT / "deploy" / "backup" / "backup.sh"

# Metrics this application does not export from its own registry: they are
# written into node-exporter's textfile collector by deploy/backup/backup.sh.
# Listed explicitly rather than pattern-matched, and each one is separately
# asserted to appear in that script below — the pair is what makes the
# backup-failure alert (docs/06 §5) verifiable without a running stack.
TEXTFILE_METRICS = {
    "buildersstream_backup_last_success_timestamp_seconds",
    "buildersstream_backup_last_run_success",
    "buildersstream_backup_last_run_timestamp_seconds",
}

# Suffixes the Prometheus client appends to a metric family's base name.
# An expression says `..._total`; the registry reports the family as
# `...`. Stripping these is how the two are compared.
_SAMPLE_SUFFIXES = ("_bucket", "_count", "_sum", "_total", "_created")

# docs/06 §5, verbatim: "service down, backup failure, disk usage above
# 85%, queue depth exceeding a defined threshold". Each key is matched
# against the alert names carrying the `nfr` label, so renaming an alert is
# fine and deleting the coverage is not.
REQUIRED_ALERT_SUBJECTS = {
    "service down": ("ServiceDown",),
    "backup failure": ("BackupFailed", "BackupStale"),
    "disk above 85%": ("DiskUsageAbove85Percent",),
    "queue depth": ("DramatiqQueueDepthHigh",),
}


def _load(path: pathlib.Path):
    return yaml.safe_load(path.read_text())


def _exported_metric_names() -> set[str]:
    """Every metric family the app's registry can produce, base names only."""
    return {metric.name for metric in REGISTRY.collect()}


def _referenced_metrics(text: str) -> set[str]:
    """Every `buildersstream_*` identifier appearing in a query expression."""
    return set(re.findall(r"\bbuildersstream_[a-z0-9_]+", text))


def _normalise(metric: str) -> str:
    for suffix in _SAMPLE_SUFFIXES:
        if metric.endswith(suffix):
            return metric[: -len(suffix)]
    return metric


def _alert_rules() -> list[dict]:
    return [rule for group in _load(ALERTS_YML)["groups"] for rule in group["rules"]]


def _compose_services() -> set[str]:
    return set(_load(PROD_COMPOSE)["services"])


# ------------------------------------------------------------------ #
# The four alerts docs/06 §5 requires
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("subject,alert_names", sorted(REQUIRED_ALERT_SUBJECTS.items()))
def test_every_alert_docs_06_requires_still_exists(subject, alert_names):
    defined = {rule["alert"] for rule in _alert_rules() if "alert" in rule}
    missing = [name for name in alert_names if name not in defined]
    assert not missing, (
        f"docs/06 §5 requires alerting on {subject}; {missing} is gone. "
        "Deleting an alert is a decision about what wakes someone up — if "
        "it is genuinely the right call, change docs/06 in the same commit."
    )


def test_required_alerts_are_tagged_so_the_check_above_cannot_rot():
    """The four required alerts carry an `nfr` label naming their source.

    Without this, the list in REQUIRED_ALERT_SUBJECTS is just a second
    transcription that can drift from the file it describes.
    """
    tagged = {
        rule["alert"]
        for rule in _alert_rules()
        if rule.get("labels", {}).get("nfr", "").startswith("docs/06")
    }
    expected = {name for names in REQUIRED_ALERT_SUBJECTS.values() for name in names}
    assert expected <= tagged, f"untagged required alerts: {sorted(expected - tagged)}"


def test_every_alert_has_a_severity_and_a_description():
    """Alertmanager routes on `severity` and humans read `description`.

    An alert missing severity falls through the critical route and gets the
    slow batching path; one missing a description pages someone at 3am with
    nothing but a metric name.
    """
    for rule in _alert_rules():
        if "alert" not in rule:
            continue
        name = rule["alert"]
        assert rule.get("labels", {}).get("severity") in {"critical", "warning"}, (
            f"{name} has no usable severity label"
        )
        annotations = rule.get("annotations", {})
        assert annotations.get("summary"), f"{name} has no summary"
        assert annotations.get("description"), f"{name} has no description"


# ------------------------------------------------------------------ #
# Metric names, cross-checked against what the app really exports
# ------------------------------------------------------------------ #


def test_alert_expressions_only_reference_metrics_that_exist():
    exported = _exported_metric_names()
    assert exported, "the app registry is empty — this check would pass vacuously"

    referenced: set[str] = set()
    for rule in _alert_rules():
        referenced |= _referenced_metrics(rule["expr"])
    assert referenced, "no buildersstream_* metric referenced by any alert"

    unknown = {
        metric
        for metric in referenced
        if _normalise(metric) not in exported and metric not in TEXTFILE_METRICS
    }
    assert not unknown, (
        f"these alert expressions name metrics nothing exports: {sorted(unknown)}. "
        "An alert on a nonexistent metric is indistinguishable from an alert "
        "that never needs to fire."
    )


def test_dashboard_panels_only_reference_metrics_that_exist():
    exported = _exported_metric_names()
    dashboard = json.loads(DASHBOARD_JSON.read_text())

    referenced: set[str] = set()
    panel_count = 0
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            panel_count += 1
            referenced |= _referenced_metrics(target["expr"])
    assert panel_count >= 10, f"only {panel_count} panel queries found — check the parse"

    unknown = {
        metric
        for metric in referenced
        if _normalise(metric) not in exported and metric not in TEXTFILE_METRICS
    }
    assert not unknown, f"dashboard panels query metrics nothing exports: {sorted(unknown)}"


def test_backup_script_writes_every_textfile_metric_alerted_on():
    """The exemption above is only safe if the script really writes them.

    TEXTFILE_METRICS is what lets the two tests above accept a metric that
    is not in the app's registry. If backup.sh stopped emitting one, those
    tests would keep passing while BackupFailed silently stopped working —
    so the exemption has to be earned, here.
    """
    script = BACKUP_SCRIPT.read_text()
    for metric in sorted(TEXTFILE_METRICS):
        assert metric in script, (
            f"{metric} is exempted from the registry check as a textfile metric, "
            f"but {BACKUP_SCRIPT.name} does not write it"
        )
    # The collector only picks up a file in the directory it was told about;
    # the compose service passes it as TEXTFILE_COLLECTOR_DIR.
    assert "TEXTFILE_COLLECTOR_DIR" in script


# ------------------------------------------------------------------ #
# Targets, receivers and the compose file they all live in
# ------------------------------------------------------------------ #


def test_every_scrape_target_is_a_service_in_the_production_compose_file():
    services = _compose_services()
    config = _load(PROMETHEUS_YML)

    targets = [
        target
        for job in config["scrape_configs"]
        for static in job["static_configs"]
        for target in static["targets"]
    ]
    assert len(targets) >= 5, f"only {len(targets)} scrape targets — check the parse"

    for target in targets:
        host = target.split(":")[0]
        if host == "localhost":
            # Prometheus scraping itself. Deliberate — see prometheus.yml.
            continue
        assert host in services, (
            f"scrape target {target!r} names no service in docker-compose.prod.yml. "
            f"Known services: {sorted(services)}"
        )


def test_prometheus_and_the_compose_file_agree_on_the_mounted_config_paths():
    """A rules file Prometheus is told to load but nothing mounts is not an
    error at boot — Prometheus starts, `rule_files` matches nothing, and
    every alert in this repository is simply absent."""
    compose = _load(PROD_COMPOSE)
    mounted = {
        volume.split(":")[1]
        for volume in compose["services"]["prometheus"]["volumes"]
        if ":" in volume
    }
    for declared in _load(PROMETHEUS_YML)["rule_files"]:
        assert declared in mounted, (
            f"prometheus.yml loads {declared}, which the compose service does not mount"
        )


def test_alertmanager_routes_reference_receivers_that_exist():
    config = _load(ALERTMANAGER_YML)
    defined = {receiver["name"] for receiver in config["receivers"]}

    route = config["route"]
    referenced = {route["receiver"]}
    referenced |= {child["receiver"] for child in route.get("routes", []) if "receiver" in child}

    assert referenced <= defined, (
        f"routes point at undefined receivers: {sorted(referenced - defined)}. "
        "Alertmanager refuses to start on this, which takes the alerting path "
        "down at exactly the moment it is needed."
    )


def test_prometheus_points_at_the_alertmanager_service():
    services = _compose_services()
    targets = [
        target
        for entry in _load(PROMETHEUS_YML)["alerting"]["alertmanagers"]
        for static in entry["static_configs"]
        for target in static["targets"]
    ]
    assert targets, "no alertmanager configured — every alert would evaluate and go nowhere"
    for target in targets:
        assert target.split(":")[0] in services


def test_grafana_dashboard_uses_the_provisioned_datasource_uid():
    """Provisioning pins the datasource uid; the dashboard hardcodes it.

    A mismatch is invisible in review and unmistakable in the browser —
    every panel comes up "Datasource not found" on a fresh volume.
    """
    provisioned = {ds["uid"] for ds in _load(DATASOURCES_YML)["datasources"]}
    dashboard = json.loads(DASHBOARD_JSON.read_text())

    used = set()
    for panel in dashboard["panels"]:
        if "datasource" in panel:
            used.add(panel["datasource"]["uid"])
        for target in panel.get("targets", []):
            if "datasource" in target:
                used.add(target["datasource"]["uid"])

    assert used, "no panel declares a datasource"
    assert used <= provisioned, f"unprovisioned datasource uids: {sorted(used - provisioned)}"


def test_monitoring_ui_ports_are_bound_to_loopback_only():
    """Grafana, Prometheus and Alertmanager are reachable over an SSH
    tunnel, never from the internet.

    docs/06 §6 restricts the internal services to the Docker network, and
    Caddy is the only intended front door (docker-compose.prod.yml's own
    header says so). A published `9090:9090` would put an unauthenticated
    Prometheus — which can read every metric in this file — on the public
    interface, and the mistake looks like a one-character diff.
    """
    compose = _load(PROD_COMPOSE)
    for name in ("prometheus", "grafana", "alertmanager"):
        for published in compose["services"][name].get("ports", []):
            assert str(published).startswith("127.0.0.1:"), (
                f"{name} publishes {published!r} on all interfaces; bind it to "
                "127.0.0.1 and reach it through an SSH tunnel "
                "(docs/11-production-deployment.md §10)"
            )


def test_node_exporter_and_the_backup_job_share_one_textfile_directory():
    """The backup writes metrics in a container that exits; node-exporter
    reads them from one that does not. If the volume or the path stops
    matching, backups keep succeeding and BackupStale starts paging."""
    compose = _load(PROD_COMPOSE)
    exporter = compose["services"]["node-exporter"]
    backup = compose["services"]["db-backup"]

    exporter_dir = next(
        arg.split("=", 1)[1]
        for arg in exporter["command"]
        if arg.startswith("--collector.textfile.directory=")
    )
    assert backup["environment"]["TEXTFILE_COLLECTOR_DIR"] == exporter_dir

    def volume_for(service, container_path):
        return {
            volume.split(":")[0]
            for volume in service["volumes"]
            if volume.split(":")[1] == container_path
        }

    shared = volume_for(exporter, exporter_dir) & volume_for(backup, exporter_dir)
    assert shared, (
        "node-exporter and db-backup mount different volumes at "
        f"{exporter_dir} — the metrics the backup writes are never read"
    )
