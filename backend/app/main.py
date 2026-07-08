from fastapi import FastAPI

from app.core.middleware import TenantMiddleware
from app.routers import auth

app = FastAPI(title="Builders Stream API", version="0.1.0")
app.add_middleware(TenantMiddleware)
app.include_router(auth.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
