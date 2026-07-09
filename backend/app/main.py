from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.middleware import TenantMiddleware
from app.core.pagination import InvalidCursorError
from app.routers import auth, companies, invitations, leads

app = FastAPI(title="Builders Stream API", version="0.1.0")
app.add_middleware(TenantMiddleware)
app.include_router(auth.router)
app.include_router(companies.router)
app.include_router(invitations.router)
app.include_router(leads.router)


@app.exception_handler(InvalidCursorError)
async def invalid_cursor_handler(request: Request, exc: InvalidCursorError) -> JSONResponse:
    # Centralized so every paginate() call site (leads.py today, more list
    # endpoints in later Phase 1 tasks) gets a clean 400 for free, instead of
    # each router needing its own try/except around paginate().
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
