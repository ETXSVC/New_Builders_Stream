"""Prometheus metrics, and the two ways this could have gone badly wrong.

docs/06 §5 asks for Prometheus + Grafana covering container health, host
CPU/memory/disk, Postgres pool saturation and Dramatiq queue depth, with
alerts on service-down, backup failure, disk >85% and queue depth. This
module is the application's contribution: everything the app itself knows
that the host and container layers cannot see.

## Never label a metric with a tenant

There is no `company_id` label here and there must never be one. Two
independent reasons, either sufficient:

  * **Cardinality.** Prometheus creates one time series per label
    combination. A `company_id` label multiplies every series by the
    customer count forever — including for companies that churned, since
    the series persist for the retention window. This is the classic way
    a monitoring stack becomes the biggest process on the box.
  * **Disclosure.** Grafana is a different trust boundary from the API.
    Per-tenant request counts and error rates would let anyone with
    dashboard access infer which customers are large, which are failing,
    and when one stopped using the product. RLS spends real effort keeping
    tenants apart; exporting a per-tenant activity feed to a dashboard
    would undo that at the observability layer.

Sentry tags events with `company_id` (app/core/observability.py) and that
is deliberately different: an error event is one bounded record a human
reads during an incident, not an unbounded time series.

## Label with the ROUTE, never the raw path

`/projects/{project_id}` is one series. The raw paths behind it are one
series per project, forever. The middleware below resolves the matched
route template and falls back to a single "unmatched" bucket, so a 404
scan cannot mint series either.

That fallback matters more than it looks: without it, anyone hitting
random URLs can grow Prometheus's memory from the outside.
"""
import time
from typing import Any, Awaitable, Callable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response

# A private registry rather than the global default. The default registry
# is process-wide and picks up anything any imported library decides to
# register, which makes the exposed surface depend on the import graph.
REGISTRY = CollectorRegistry()

http_requests = Counter(
    "buildersstream_http_requests_total",
    "HTTP requests by route template, method and status class.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)

http_duration = Histogram(
    "buildersstream_http_request_duration_seconds",
    "Request duration by route template.",
    labelnames=("method", "route"),
    # Tuned for this app rather than the library default: the interesting
    # range is 25ms-2s, and the PDF export and bulk catalog import are the
    # only routes expected past that.
    buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

db_pool_in_use = Gauge(
    "buildersstream_db_pool_connections_in_use",
    "Checked-out connections in the runtime engine's pool.",
    registry=REGISTRY,
)

db_pool_size = Gauge(
    "buildersstream_db_pool_size",
    "Configured pool size, so saturation is a ratio rather than a guess.",
    registry=REGISTRY,
)

queue_depth = Gauge(
    "buildersstream_dramatiq_queue_depth",
    "Messages waiting in a Dramatiq queue.",
    labelnames=("queue",),
    registry=REGISTRY,
)

dead_letter_depth = Gauge(
    "buildersstream_dramatiq_dead_letter_depth",
    "Messages Dramatiq gave up on after exhausting retries (the .XQ "
    "sorted set). The counterpart to broker.py's DeadLetterLogging: that "
    "writes a log line when one dies, this makes the accumulated total "
    "alertable.",
    labelnames=("queue",),
    registry=REGISTRY,
)

queue_depth_scrape_failures = Counter(
    "buildersstream_dramatiq_queue_scrape_failures_total",
    "Times the queue-depth probe could not reach Redis. A rising count "
    "means the queue_depth gauge is stale, which otherwise looks like a "
    "healthy empty queue.",
    registry=REGISTRY,
)


def _route_label(request: Request) -> str:
    """The matched route TEMPLATE, or a single bucket for everything else.

    `request.scope["route"]` is set by Starlette once routing has matched.
    Using `request.url.path` instead would create a series per project id,
    invoice id and so on — see the module docstring.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    # One bucket for everything unmatched. Deliberately not the real path:
    # a 404 scan must not be able to grow the series count from outside.
    return "<unmatched>"


async def metrics_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # An unhandled exception still becomes a 500 downstream, so record
        # it rather than losing the observation entirely.
        http_requests.labels(request.method, _route_label(request), "5xx").inc()
        http_duration.labels(request.method, _route_label(request)).observe(
            time.perf_counter() - started
        )
        raise

    route = _route_label(request)
    # Status CLASS, not the exact code: 2xx/4xx/5xx is what an alert or a
    # dashboard actually asks about, at a fifth of the series.
    http_requests.labels(request.method, route, f"{response.status_code // 100}xx").inc()
    http_duration.labels(request.method, route).observe(time.perf_counter() - started)
    return response


def _refresh_pool_gauges() -> None:
    """Sampled at scrape time rather than tracked on every checkout.

    The pool already knows these numbers; mirroring them continuously
    would add work to the hot path to reproduce state that is one call
    away.
    """
    from app.db import engine

    pool = engine.pool
    try:
        db_pool_in_use.set(pool.checkedout())  # type: ignore[attr-defined]
        db_pool_size.set(pool.size())  # type: ignore[attr-defined]
    except AttributeError:
        # NullPool and friends expose neither; leave the gauges untouched
        # rather than publishing a zero that reads as "idle".
        pass


# Dramatiq's Redis broker key layout (brokers/redis/dispatch.lua):
#
#   <ns>:<queue>            list   — message ids waiting to be fetched
#   <ns>:<queue>.DQ         list   — the delay queue for retries/scheduled sends
#   <ns>:<queue>.XQ         zset   — dead-lettered ids, scored by death time
#   <ns>:<queue>.msgs       hash   — id -> payload, for any of the above
#   <ns>:__acks__.*         set    — fetched-but-unacked, per (worker, queue)
#   <ns>:__heartbeats__     zset   — worker liveness
#
# Only the first three are backlogs. The rest are bookkeeping and would
# either double-count (`.msgs` holds a row per queued AND per in-flight
# message) or report something that is not a queue at all.
_QUEUE_BOOKKEEPING_SUFFIX = ".msgs"
_QUEUE_INTERNAL_PREFIXES = ("__acks__", "__heartbeats__")


def _refresh_queue_depth() -> None:
    """Dramatiq queue depth, read straight from Redis.

    Deliberately not a separate redis_exporter service: that would report
    Redis's own health, and what docs/06 §5 asks for is *queue depth*,
    which means knowing Dramatiq's key naming. The app already does.

    Queues are discovered by scanning the namespace rather than read from
    `broker.get_declared_queues()`, and that is the whole point. A broker
    only declares the queues of actors the *importing process* loaded, so
    the API — which imports four of the eight actor modules — would report
    depth for those four and silently omit the rest. The failure mode is
    the same one the compose files' actor-module discipline guards against
    (an unwatched queue backing up while the dashboard reads zero), so the
    probe is made independent of this process's import graph.

    A failure here increments a counter instead of raising. A scrape that
    500s loses every other metric in this file, and an unreachable Redis
    would otherwise present as a queue that is reassuringly empty.
    """
    try:
        from app.tasks.broker import redis_broker

        client = redis_broker.client
        namespace = redis_broker.namespace
        for raw_key in client.scan_iter(match=f"{namespace}:*", count=100):
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            name = key[len(namespace) + 1 :]
            if name.endswith(_QUEUE_BOOKKEEPING_SUFFIX) or name.startswith(
                _QUEUE_INTERNAL_PREFIXES
            ):
                continue
            if name.endswith(".XQ"):
                dead_letter_depth.labels(name[: -len(".XQ")]).set(client.zcard(key))
            else:
                # Both `<queue>` and `<queue>.DQ` are lists. The delay
                # queue keeps its own series rather than being folded into
                # the main one: a growing .DQ means retries are piling up,
                # which is a different incident from "workers are behind".
                queue_depth.labels(name).set(client.llen(key))
    except Exception:
        queue_depth_scrape_failures.inc()


def render_metrics() -> bytes:
    """The exposition payload. Gauges are refreshed here so their values
    are current as of the scrape rather than as of the last request."""
    _refresh_pool_gauges()
    _refresh_queue_depth()
    return generate_latest(REGISTRY)


def metrics_content_type() -> str:
    from prometheus_client import CONTENT_TYPE_LATEST

    return str(CONTENT_TYPE_LATEST)


def registry_snapshot() -> dict[str, Any]:
    """Every currently-exported series name and its label keys. For the
    tests that assert no tenant label ever appears."""
    snapshot: dict[str, Any] = {}
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            snapshot.setdefault(sample.name, set()).update(sample.labels.keys())
    return snapshot
