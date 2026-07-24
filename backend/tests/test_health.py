from httpx import AsyncClient, ASGITransport

from app.main import app


async def test_health_returns_ok():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_returns_ok_when_dependencies_are_up():
    from app.core.readiness import _reset_redis_client_for_tests

    # Fresh client bound to this test's own event loop — same hazard the
    # rate limiter's reset helper documents.
    _reset_redis_client_for_tests()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ok", "redis": "ok"}


async def test_ready_reports_the_failing_dependency(monkeypatch):
    """A degraded /ready must say WHICH dependency is down — that string is
    what docker logs/curl gives the operator during an incident."""
    from app.core import readiness

    async def failing_probe():
        return False

    monkeypatch.setattr(readiness, "probe_redis", failing_probe)
    # main.py imported the names at module load — patch the app module's
    # references too.
    import app.main as main_module

    monkeypatch.setattr(main_module, "probe_redis", failing_probe)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["redis"] == "unavailable"
    assert body["database"] == "ok"
