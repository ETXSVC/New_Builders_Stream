"""What `/metrics` may and may not expose.

Two of these are ordinary behavioural tests. The other two guard invariants
that nothing else in this codebase can: a Prometheus label mistake produces
no error, no failing request and no log line — it produces a metrics
endpoint that works perfectly while quietly growing without bound or
leaking who the customers are. The cost lands weeks later, on the person
holding the pager, so it has to be caught here.

See app/core/metrics.py's module docstring for the reasoning; this file is
the enforcement.
"""
import pytest

from app.core.metrics import (
    REGISTRY,
    _route_label,
    registry_snapshot,
)

# Label names that must never appear on a time series. `company_id` is the
# real one — the tenant identifier RLS spends the whole codebase keeping
# apart. The rest are the shapes the same mistake takes when someone
# reaches for "just a bit more detail" on a dashboard.
FORBIDDEN_LABELS = {
    "company_id",
    "tenant",
    "tenant_id",
    "user_id",
    "user",
    "email",
    # Not a tenant leak, an unbounded one: the raw request path is one
    # series per project/invoice/estimate id, forever.
    "path",
    "url",
}


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_prometheus_exposition(client):
    response = await client.get("/metrics")

    assert response.status_code == 200
    # Prometheus refuses a payload whose content type it does not
    # recognise, and the failure is on the scraper's side where nobody is
    # looking at this app's logs.
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "buildersstream_http_requests_total" in body
    # HELP lines are what makes a metric legible in Grafana's browser
    # rather than a name someone has to grep the source for.
    assert "# HELP buildersstream_dramatiq_queue_depth" in body


@pytest.mark.asyncio
async def test_no_series_is_labelled_with_a_tenant_or_a_raw_path(client):
    """The invariant from app/core/metrics.py's docstring, as a gate.

    Driven through a real request first so the http_* families have actual
    label sets to inspect — an unexercised Counter reports its declared
    labels but no samples, and this test would pass vacuously against a
    registry that had never seen traffic.
    """
    await client.get("/health")
    await client.get("/metrics")

    snapshot = registry_snapshot()
    assert snapshot, "no series exported at all — the sweep below would pass vacuously"

    offenders = {
        name: sorted(labels & FORBIDDEN_LABELS)
        for name, labels in snapshot.items()
        if labels & FORBIDDEN_LABELS
    }
    assert not offenders, (
        "these series carry a label that is either unbounded or "
        f"tenant-identifying: {offenders}. See app/core/metrics.py — the "
        "reasons are cardinality and disclosure, and neither is negotiable "
        "for a dashboard's convenience."
    )


@pytest.mark.asyncio
async def test_requests_are_counted_under_the_route_template(client):
    """A parameterised route contributes ONE series, whatever id is used.

    Asserted as a delta on the template's own counter rather than as a diff
    of the label SET: the registry is process-global, so by the time the
    full suite reaches this test, `/projects/{project_id}` has already been
    counted thousands of times and a set difference would be empty. That
    made the first version of this test pass alone and fail in the suite.
    """
    route = "/projects/{project_id}"
    before = _counter_value(route)

    # Two different ids on the same route. Unauthenticated, so both 401 —
    # irrelevant here, the middleware counts every request regardless of
    # outcome, which is the point (an auth failure is still traffic).
    await client.get("/projects/11111111-1111-1111-1111-111111111111")
    await client.get("/projects/22222222-2222-2222-2222-222222222222")

    assert _counter_value(route) - before == 2, (
        f"expected both requests under {route!r}; the counter moved by "
        f"{_counter_value(route) - before}"
    )
    # The failure this is really about: if _route_label ever fell back to
    # request.url.path, the two calls above would have minted two series.
    assert not any("1111-1111" in r or "2222-2222" in r for r in _counter_routes()), (
        "a raw path leaked into a label"
    )


@pytest.mark.asyncio
async def test_unmatched_paths_all_collapse_into_one_bucket(client):
    """A 404 scan must not be able to grow Prometheus's memory from outside."""
    before = _counter_value("<unmatched>")
    paths = [f"/no-such-endpoint-{i}" for i in range(5)]

    for path in paths:
        await client.get(path)

    assert _counter_value("<unmatched>") - before == 5, (
        "five unmatched requests did not all land in the shared bucket"
    )
    labelled = _counter_routes()
    assert not any(path in labelled for path in paths), (
        "a 404 path became its own series — anyone who can reach this app "
        "could then mint series at will"
    )


def test_route_label_falls_back_when_routing_never_matched():
    """The unit-level counterpart: the middleware also observes requests
    that raised before Starlette set `scope['route']` at all."""

    class _FakeRequest:
        scope: dict = {}

    assert _route_label(_FakeRequest()) == "<unmatched>"  # type: ignore[arg-type]


def _request_counter_samples():
    """Every sample of the request counter, excluding the `_created`
    timestamp series the client library emits alongside each counter."""
    for metric in REGISTRY.collect():
        if metric.name != "buildersstream_http_requests":
            continue
        for sample in metric.samples:
            if sample.name.endswith("_total") and "route" in sample.labels:
                yield sample


def _counter_routes() -> set[str]:
    """The `route` label values currently present on the request counter."""
    return {sample.labels["route"] for sample in _request_counter_samples()}


def _counter_value(route: str) -> float:
    """Total requests counted under one route label, across every method
    and status class. Zero when the route has never been seen — which is
    what makes a before/after delta work on a registry other tests have
    already written to."""
    return sum(
        sample.value for sample in _request_counter_samples() if sample.labels["route"] == route
    )
