"""Fixed-window Redis rate limiter — the mechanism behind /auth/register's
anti-enumeration rate limit (see app/config.py's register_rate_limit_*
settings and the routers/auth.py call site). A generic, single-purpose
counter rather than anything register-specific, so a future caller with a
different rate-limit need can reuse it with its own key prefix and limits.
"""

import logging

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)

_redis_client: redis.Redis | None = None


def _get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url)
    return _redis_client


def _reset_redis_client_for_tests() -> None:
    """Test-only — never called from application code.

    pytest-asyncio gives most test functions their own fresh event loop
    (pytest.ini's asyncio_default_fixture_loop_scope=session governs
    FIXTURES only, not test functions — see tests/conftest.py's db_session
    fixture for the identical hazard already diagnosed once for the DB
    engine). A redis.asyncio.Redis client is bound to whichever loop is
    running when it's first constructed; reused from a different, later
    test's loop it raises "Event loop is closed". Harmless for the real app
    (one uvicorn process = one event loop for its whole lifetime, so the
    module-level singleton above is correct there) — this exists purely so
    a test that exercises the rate limiter can force a fresh, correctly-
    bound client before it runs, the same way db_session gets a fresh
    engine per test rather than reusing one across loop boundaries.
    """
    global _redis_client
    _redis_client = None


async def check_rate_limit(key: str, max_attempts: int, window_seconds: int) -> bool:
    """Returns True if the caller may proceed, False if the limit is exceeded.

    Fixed-window counter: the first call under a given `key` starts a window
    of `window_seconds` (via Redis EXPIRE); every call within that window
    increments the same counter. Simple and adequate for this use — an
    attacker gets at most one extra burst at each window boundary compared
    to a true sliding window, which doesn't matter for slowing down bulk
    email enumeration to impractical levels.

    Fails OPEN on a Redis outage (WARNING-logged): this limiter exists as
    anti-enumeration hardening, and failing closed would convert a Redis
    outage into a total signup outage — a strictly worse availability
    trade for a marginal, temporary loss of enumeration resistance during
    a window an attacker would have to know about to exploit. Same
    documented fail-open stance block_if_read_only/tier_allows already
    take for a missing subscription row.
    """
    try:
        client = _get_redis_client()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_seconds)
    except (redis.RedisError, OSError) as exc:
        logger.warning("rate limiter unavailable, failing open for %s: %s", key, exc)
        return True
    return count <= max_attempts
