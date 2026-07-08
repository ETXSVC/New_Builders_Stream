from fastapi import FastAPI

from app.core.middleware import TenantMiddleware

app = FastAPI(title="Builders Stream API", version="0.1.0")
app.add_middleware(TenantMiddleware)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
