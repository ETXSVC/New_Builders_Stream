from contextvars import ContextVar
from typing import Optional

# Populated by TenantMiddleware from the request's JWT / X-Tenant-ID header.
# This is a *claim*, not a verified grant — see design decision #3.
claimed_tenant_id_ctx: ContextVar[Optional[str]] = ContextVar("claimed_tenant_id", default=None)
bearer_token_ctx: ContextVar[Optional[str]] = ContextVar("bearer_token", default=None)
