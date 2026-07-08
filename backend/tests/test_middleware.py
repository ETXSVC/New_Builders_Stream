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
