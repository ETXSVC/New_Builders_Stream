from httpx import AsyncClient, ASGITransport

from app.core.context import bearer_token_ctx, claimed_tenant_id_ctx
from app.main import app


async def test_middleware_populates_context_from_headers():
    captured = {}

    @app.get("/_debug_context")
    async def debug_context():
        captured["token"] = bearer_token_ctx.get()
        captured["tenant"] = claimed_tenant_id_ctx.get()
        return {}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get(
            "/_debug_context",
            headers={"Authorization": "Bearer abc123", "X-Tenant-ID": "11111111-1111-1111-1111-111111111111"},
        )

    assert captured["token"] == "abc123"
    assert captured["tenant"] == "11111111-1111-1111-1111-111111111111"


async def test_middleware_leaves_context_none_when_headers_absent():
    """Covers the branches the first test doesn't: no Authorization header at
    all, and no X-Tenant-ID header. Both should resolve to None, not an
    exception or a stale value — later code (Task 11's get_current_user)
    treats None as "no claim" and falls back appropriately; a regression here
    (e.g. someone breaks the `else None` ternary in middleware.py) would
    silently propagate a wrong value instead of failing loudly."""
    captured = {}

    @app.get("/_debug_context_absent")
    async def debug_context_absent():
        captured["token"] = bearer_token_ctx.get()
        captured["tenant"] = claimed_tenant_id_ctx.get()
        return {}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/_debug_context_absent")

    assert captured["token"] is None
    assert captured["tenant"] is None


async def test_middleware_leaves_token_none_for_non_bearer_scheme():
    """Covers the `else None` branch specifically: a non-Bearer Authorization
    scheme (e.g. Basic auth) must not be captured as a token."""
    captured = {}

    @app.get("/_debug_context_basic_auth")
    async def debug_context_basic_auth():
        captured["token"] = bearer_token_ctx.get()
        return {}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/_debug_context_basic_auth", headers={"Authorization": "Basic dXNlcjpwYXNz"})

    assert captured["token"] is None
