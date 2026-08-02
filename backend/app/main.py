import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.event_handlers import register_event_handlers
from app.core.logging import configure_logging
from app.core.observability import init_error_reporting
from app.core.metrics import metrics_content_type, metrics_middleware, render_metrics
from app.core.middleware import TenantMiddleware
from app.core.pagination import InvalidCursorError
from app.core.readiness import probe_database, probe_redis
from app.services.stripe_client import StripeUnavailableError
from app.routers import (
    auth,
    bills,
    bom_lines,
    branding,
    company_email_settings,
    company_financial_settings,
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
    platform_auth,
    platform_tenants,
    projects,
    reports,
    subcontractor_assignments,
    subcontractors,
    subscriptions,
    tasks,
    team,
    vendors,
    webhooks,
)

configure_logging()
# Before the app object exists, so an error during router import or
# startup is reported rather than only printed.
init_error_reporting("api")
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
# NO CORSMiddleware, deliberately — do not "fix" this by adding one.
#
# The browser never talks to this API. `frontend/lib/api/client.ts` is
# `server-only`, and every browser request goes to a Next.js Route Handler
# on the frontend's own origin, which then makes a server-to-server call
# here (the BFF pattern). Server-to-server requests are not subject to the
# same-origin policy, so there is no preflight to answer and nothing for
# CORS to permit.
#
# Adding a permissive CORS policy — `allow_origins=["*"]` especially, which
# is the usual reflex when a "CORS error" appears — would not fix any real
# problem and WOULD turn this into an API any origin can call directly with
# a stolen bearer token, bypassing the BFF boundary entirely. If a genuine
# cross-origin consumer ever exists (a native mobile app, a partner
# integration), it needs an explicit allowlist and its own review, not a
# wildcard.
app.add_middleware(TenantMiddleware)
# Registered AFTER TenantMiddleware, which means it runs OUTSIDE it —
# Starlette applies middleware in reverse registration order — so the
# timing includes tenant resolution and the counter sees requests that
# TenantMiddleware rejects.
app.middleware("http")(metrics_middleware)
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
# Registered beside branding and BEFORE companies.router, for exactly the
# reason spelled out above: `/companies/{company_id}` would otherwise
# swallow the literal `/companies/email-settings` and answer 422 on a
# hostname that is not a UUID.
app.include_router(company_email_settings.router)
# Same reason again: `/companies/{company_id}` would swallow the literal
# `/companies/financial-settings`.
app.include_router(company_financial_settings.router)
app.include_router(companies.router)
app.include_router(invitations.router)
app.include_router(leads.router)
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(catalogs.router)
app.include_router(vendors.router)
app.include_router(bom_lines.router)
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
app.include_router(team.router)
# Platform console (migration 0023). Registered last because nothing else
# claims a `/platform` prefix, so ordering carries no meaning here — unlike
# the branding/companies pair above. These routes authenticate through
# `get_platform_admin`, NOT `get_current_user`, and a token minted by
# /auth/login can never reach them.
app.include_router(platform_auth.router)
app.include_router(platform_tenants.router)

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


@app.exception_handler(StripeUnavailableError)
async def stripe_unavailable_handler(
    request: Request, exc: StripeUnavailableError
) -> JSONResponse:
    # 502, not 500: the request failed because an upstream dependency did,
    # which is a different thing to tell an operator reading logs at 2am
    # than "our own code raised". The billing design spec asks for exactly
    # this on a failed registration — a trial-less root company is a state
    # that feature does not tolerate, so the whole registration fails rather
    # than half-completing.
    #
    # The message is logged, never returned: Stripe's error strings can name
    # Price ids and account details, and this response reaches an anonymous
    # caller on /auth/register.
    logger.exception("stripe call failed on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=502, content={"detail": "Billing provider unavailable, please retry"}
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Turns silent 500s into greppable log lines (docker logs is the
    # observability surface on the single-box deployment) without leaking
    # internals to the client. Starlette re-raises through this handler's
    # response, so the traceback is captured here, not swallowed.
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition. Deliberately NOT in the OpenAPI schema: it is
    an operational surface, not part of the product API, and including it
    would put it in the generated frontend types.

    Unauthenticated, and that is safe only because of where this runs. The
    production reverse proxy fronts the Next BFF alone and never routes the
    backend (deploy/Caddyfile), so nothing outside the compose network can
    reach this. In the split topology, deploy/Caddyfile.api additionally
    pins a remote_ip allowlist. If the backend is ever published directly,
    this route needs a guard before that happens — see
    docs/11-production-deployment.md.
    """
    return Response(content=render_metrics(), media_type=metrics_content_type())


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
