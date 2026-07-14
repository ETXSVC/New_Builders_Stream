import asyncio
import os
import tempfile

import asyncpg
import email_validator
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

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
# Same reasoning as DATABASE_URL above: app.config.Settings' default
# (`/data/documents`, a Docker-volume path — see Task 1.15) doesn't exist on
# the host running pytest. Point it at a host-writable temp directory
# instead, set at conftest.py import time for the same "before any test
# module's own import of app.config" ordering reason given above. Left in
# place (not cleaned up) after the session — it's under the OS temp
# directory, same as any other tempfile.mkdtemp() caller's convention of
# leaving cleanup to the OS/user, and test isolation doesn't require
# removing it (each test's uploaded files use fresh company/project UUIDs,
# so nothing collides across test runs).
os.environ.setdefault("STORAGE_ROOT", tempfile.mkdtemp(prefix="builders_stream_test_documents_"))


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


@pytest_asyncio.fixture(loop_scope="function")
async def db_session():
    """Real, owner-role AsyncSession (bypasses RLS — table owners are exempt
    by default, same reasoning as _clean_tables' asyncpg connection below).
    Needed by tests (e.g. tests/test_invoicing_service.py, Task 3.33) that
    must see rows across MULTIPLE companies in one test — a tenant-scoped
    app_user session can only ever see one company's rows at a time under
    RLS.

    `loop_scope="function"` is required, not stylistic. pytest.ini sets
    `asyncio_default_fixture_loop_scope = session`, which applies to
    FIXTURES only — it says nothing about the loop scope of TEST FUNCTIONS
    themselves, which pytest-asyncio defaults to "function" (a fresh loop
    per test) unless configured otherwise. Without this explicit override,
    `db_session` would run on the session-scoped loop while the test
    calling it runs on its own per-function loop — two different loops.
    That mismatch reproduced as `RuntimeError: Event loop is closed`,
    raised from inside `session.rollback()` itself (confirmed by adding
    print statements immediately before/after the rollback() call and
    observing the "after" print never fires): once the test's own
    per-function loop closes at the end of that test, this fixture's
    teardown — resuming on the OTHER (session) loop as far as pytest-
    asyncio's bookkeeping goes, but actually needing to drive I/O that got
    scheduled against the now-closed per-function loop — can no longer
    complete. Pinning this fixture to loop_scope="function" makes it use
    the SAME loop as the test that requested it, eliminating the mismatch
    entirely.

    poolclass=NullPool: no connection pooling — every checkout opens a
    brand-new physical connection, every checkin closes it immediately.
    This fixture is used rarely (a handful of tests, one session each), so
    there's no performance reason to pool, and a pooled connection was a
    SEPARATE proven cause of a hang while diagnosing this fixture: a
    connection returned to the pool without its transaction fully unwound
    left a later test's TRUNCATE-based _clean_tables cleanup blocked
    indefinitely ("idle in transaction", holding a lock pg_locks confirmed
    _clean_tables' TRUNCATE was waiting on).

    Teardown order matters: rollback, then close, then dispose — in that
    order. Skipping the rollback and going straight to close()/dispose()
    leaves the session's implicitly-opened transaction "idle in
    transaction" in Postgres, holding locks that can hang a LATER test's
    TRUNCATE-based _clean_tables cleanup. This is not hypothetical: this
    exact leak-cleanup-order bug previously hung a full pytest run for
    over an hour in this codebase (see test_read_only_enforcement.py's
    _make_current_user_for_status() docstring for the original incident).
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = session_factory()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


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
