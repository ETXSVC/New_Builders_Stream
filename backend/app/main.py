import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.event_handlers import register_event_handlers
from app.core.logging import configure_logging
from app.core.middleware import TenantMiddleware
from app.core.pagination import InvalidCursorError
from app.core.readiness import probe_database, probe_redis
from app.routers import (
    auth,
    bills,
    branding,
    catalogs,
    change_orders,
    companies,
    compliance,
    dashboard,
    esignatures,
    estimates,
    expenses,
    integrations,
    invitations,
    invoices,
    leads,
    projects,
    reports,
    subcontractor_assignments,
    subcontractors,
    subscriptions,
    tasks,
    webhooks,
)

configure_logging()
logger = logging.getLogger("app")

# In production the interactive docs/schema endpoints are disabled — the
# backend isn't publicly routed there (the reverse proxy only fronts the
# Next.js BFF), so this is free defense in depth, not the boundary itself.
# scripts/export_openapi.py runs under the development default, unaffected.
_in_production = settings.app_env == "production"
app = FastAPI(
    title="Builders Stream API",
    version="0.1.0",
    docs_url=None if _in_production else "/docs",
    redoc_url=None if _in_production else "/redoc",
    openapi_url=None if _in_production else "/openapi.json",
)
app.add_middleware(TenantMiddleware)
app.include_router(auth.router)
# branding.router is registered BEFORE companies.router deliberately:
# companies.router declares `GET/PUT /companies/{company_id}` (a generic,
# single-path-segment pattern), which would otherwise shadow this router's
# literal `/companies/branding` and `/companies/branding/logo` paths —
# Starlette tries included routes in registration order and stops at the
# first structural match, regardless of a path parameter's declared Python
# type, so `{company_id}` matching the literal string "branding" would win
# and 422 (failed UUID parse) before branding.router's own routes ever got a
# chance. Confirmed by the same "declare the specific literal before the
# generic parameter" precedent companies.py itself already uses internally
# for its own `/companies/members` vs `/companies/{company_id}` ordering.
app.include_router(branding.router)
app.include_router(companies.router)
app.include_router(invitations.router)
app.include_router(leads.router)
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(catalogs.router)
app.include_router(estimates.router)
app.include_router(esignatures.router)
app.include_router(change_orders.router)
app.include_router(subcontractors.router)
app.include_router(subcontractor_assignments.router)
app.include_router(compliance.router)
app.include_router(subscriptions.router)
app.include_router(invoices.router)
app.include_router(bills.router)
app.include_router(expenses.router)
app.include_router(reports.router)
app.include_router(webhooks.router)
app.include_router(integrations.router)
app.include_router(dashboard.router)

# Task 1.18: wires the real LEAD_WON -> draft-Project handler into
# app.core.events for actual requests served by this app instance. Called
# once here, at module import time (i.e. once per process) — see
# app/core/event_handlers.py's docstring for why tests can't rely on this
# same call and must invoke register_event_handlers() themselves instead.
register_event_handlers()


@app.exception_handler(InvalidCursorError)
async def invalid_cursor_handler(request: Request, exc: InvalidCursorError) -> JSONResponse:
    # Centralized so every paginate() call site (leads.py today, more list
    # endpoints in later Phase 1 tasks) gets a clean 400 for free, instead of
    # each router needing its own try/except around paginate().
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Turns silent 500s into greppable log lines (docker logs is the
    # observability surface on the single-box deployment) without leaking
    # internals to the client. Starlette re-raises through this handler's
    # response, so the traceback is captured here, not swallowed.
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health() -> dict:
    """Static liveness: is the process serving requests at all. Dependency
    state deliberately excluded — that's /ready's job (see
    app/core/readiness.py for the split's rationale)."""
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness: can this process do useful work right now. Probes report
    per-dependency status so a failing healthcheck names the dependency."""
    database_ok = await probe_database()
    redis_ok = await probe_redis()
    all_ok = database_ok and redis_ok
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={
            "status": "ready" if all_ok else "degraded",
            "database": "ok" if database_ok else "unavailable",
            "redis": "ok" if redis_ok else "unavailable",
        },
    )
