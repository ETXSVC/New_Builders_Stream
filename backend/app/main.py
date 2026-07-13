from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.event_handlers import register_event_handlers
from app.core.middleware import TenantMiddleware
from app.core.pagination import InvalidCursorError
from app.routers import (
    auth,
    catalogs,
    change_orders,
    companies,
    esignatures,
    estimates,
    invitations,
    leads,
    projects,
    subcontractors,
    tasks,
)

app = FastAPI(title="Builders Stream API", version="0.1.0")
app.add_middleware(TenantMiddleware)
app.include_router(auth.router)
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
