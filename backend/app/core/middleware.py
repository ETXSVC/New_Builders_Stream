from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.context import bearer_token_ctx, claimed_tenant_id_ctx


class TenantMiddleware(BaseHTTPMiddleware):
    """Extracts the bearer token and the claimed tenant ID from the raw request
    and makes them available via contextvars for the duration of the request.
    Does NOT verify the claim — that happens in `get_current_user`
    (design decision #3), which has database access and this middleware does not.
    """

    async def dispatch(self, request: Request, call_next):
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else None
        tenant_header = request.headers.get("X-Tenant-ID")

        token_reset = bearer_token_ctx.set(token)
        tenant_reset = claimed_tenant_id_ctx.set(tenant_header)
        try:
            return await call_next(request)
        finally:
            bearer_token_ctx.reset(token_reset)
            claimed_tenant_id_ctx.reset(tenant_reset)
