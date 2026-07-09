import asyncio
import os

import asyncpg
import email_validator
import pytest
from alembic import command
from alembic.config import Config
from httpx import AsyncClient, ASGITransport

# pydantic's EmailStr calls email_validator.validate_email() with no way to pass
# test_environment=True (the flag the library documents for exactly this case), so
# by default it rejects RFC 2606 reserved test TLDs (.test, .example, .invalid,
# .localhost) as "special-use or reserved" domains — even though this project's own
# test fixtures (e.g. "ada@acme.test" below) intentionally use them. Removing "test"
# from the module-level denylist here, once, for the whole test session is the
# narrowest fix: it doesn't touch app/schemas/auth.py (Task 8, out of scope for this
# task) or the given test payloads, and it has no effect outside the test process.
if "test" in email_validator.SPECIAL_USE_DOMAIN_NAMES:
    email_validator.SPECIAL_USE_DOMAIN_NAMES.remove("test")


TEST_DB_NAME = "builders_stream_test"
ADMIN_DSN = "postgresql://postgres:devpassword@localhost:5432/postgres"
TEST_DATABASE_URL = f"postgresql+asyncpg://postgres:devpassword@localhost:5432/{TEST_DB_NAME}"
TEST_APP_DATABASE_URL = f"postgresql+asyncpg://app_user:app_password@localhost:5432/{TEST_DB_NAME}"

# Point the app at the test database BEFORE app.config is imported anywhere else.
# This must run at conftest.py *module* import time, not inside a fixture body:
# pytest imports every test module in this directory during collection — which
# happens before any fixture (even a session-scoped autouse one) executes — and
# test_health.py/test_middleware.py import `app.main` at their own module level.
# That transitively imports app.config, whose `settings = Settings()` singleton
# is built once, from these env vars, the first time it's imported. If that first
# import happens during collection (before a fixture could set these), settings
# ends up holding the real .env values — notably DATABASE_URL's `postgres` Docker
# hostname, which doesn't resolve on the host running pytest. Setting env vars
# here, at conftest.py import time, guarantees they're in place before pytest
# imports any test module in this directory (pytest always imports conftest.py
# first), so every subsequent `Settings()` construction sees the test values
# regardless of which test file happens to trigger it first.
os.environ["DATABASE_URL"] = TEST_APP_DATABASE_URL
os.environ["MIGRATIONS_DATABASE_URL"] = TEST_DATABASE_URL.replace("+asyncpg", "+asyncpg")
os.environ["TEST_DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("JWT_EXPIRE_MINUTES", "60")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


async def _recreate_test_database() -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def _setup_test_database():
    asyncio.run(_recreate_test_database())

    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    alembic_cfg.set_main_option(
        "sqlalchemy.url", TEST_DATABASE_URL.replace("postgresql+asyncpg", "postgresql")
    )
    command.upgrade(alembic_cfg, "head")

    yield


@pytest.fixture
async def client():
    from app.main import app  # imported after env vars are set by the fixture above

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _clean_event_registry():
    """app.core.events._handlers is process-lifetime module state (Task 1.5),
    not per-test state — nothing else resets it between tests. Clearing it
    both before and after every test means a test that registers a handler
    and then fails before its own cleanup can't leak that handler into
    every later test's LEAD_WON (or other event) dispatches."""
    from app.core import events

    events.clear()
    yield
    events.clear()


@pytest.fixture(autouse=True)
async def _clean_tables():
    """Truncates all tenant tables before every test using the Postgres owner
    connection, which bypasses RLS (table owners are exempt by default) — this
    is test cleanup, not a runtime code path, so bypassing RLS here is correct."""
    yield
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        await conn.execute(
            "TRUNCATE audit_log, invitations, company_users, users, companies RESTART IDENTITY CASCADE"
        )
    finally:
        await conn.close()

    # app.db's engine is a module-level singleton with pool_pre_ping=True, so
    # SQLAlchemy reuses pooled asyncpg connections across tests and pings them
    # before reuse. On Windows' ProactorEventLoop, a connection checked back
    # into the pool at the end of one test's run_until_complete() call can have
    # its overlapped-I/O transport torn down by the time the next test's
    # pre-ping tries to write to it (AttributeError: 'NoneType' object has no
    # attribute 'send', because the transport's proactor reference is gone) —
    # empirically reproduced running this file's two tests back to back.
    # Disposing the pool after every test forces a fresh connection next time,
    # which sidesteps the stale-transport reuse entirely.
    from app.db import engine

    await engine.dispose()
