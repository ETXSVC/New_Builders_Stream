"""Dependency probes behind GET /ready.

Deliberately separate from /health: /health stays a static liveness check
(is the process serving requests at all — the thing a restart policy
should act on), while /ready answers "can this process do useful work
right now" (the thing a load balancer / compose healthcheck should gate
on). Conflating them makes a Postgres outage restart-loop the backend for
no benefit.

Owns its own lazy Redis client rather than reusing
app/services/rate_limit.py's — that module's client is private and its
test-reset machinery (_reset_redis_client_for_tests, event-loop binding)
is deliberately scoped to the rate limiter; sharing it would couple
readiness to that lifecycle for no gain.
"""
import asyncio

import redis.asyncio as redis
from sqlalchemy import text

from app.config import settings
from app.db import engine

_PROBE_TIMEOUT_SECONDS = 2.0

_redis_client: redis.Redis | None = None


def _get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url)
    return _redis_client


def _reset_redis_client_for_tests() -> None:
    """Test-only — same event-loop-binding hazard (and the same remedy) as
    app/services/rate_limit.py's helper of the same name; see that
    docstring for the full diagnosis."""
    global _redis_client
    _redis_client = None


async def probe_database() -> bool:
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def probe_redis() -> bool:
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            await _get_redis_client().ping()
        return True
    except Exception:
        return False
