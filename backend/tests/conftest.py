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
# The `platform_admin` role (migration 0023) against the TEST database.
# Assigned unconditionally like the two above, not via setdefault: a
# developer whose shell exports the dev value would otherwise have the
# platform-console tests read and WRITE the development database.
TEST_PLATFORM_DATABASE_URL = (
    f"postgresql+asyncpg://platform_admin:platform_password@localhost:5432/{TEST_DB_NAME}"
)
# The `scanner` role (migration 0020) against the TEST database, and
# assigned for exactly the reason described above — this one is not
# hypothetical. `app/tasks/scanner_db.py` resolves
# `scanner_database_url or migrations_database_url`, and only the second of
# those was ever pointed at the test database here. CI has no `.env`, so the
# fallback made it pass there; every developer machine has one (it ships in
# `.env.example`), which means the three cross-tenant sweeps under test —
# compliance expiry, seat usage, overdue financial records — connected to
# the DEVELOPMENT database and wrote to it.
TEST_SCANNER_DATABASE_URL = (
    f"postgresql+asyncpg://scanner:scanner_password@localhost:5432/{TEST_DB_NAME}"
)

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
os.environ["PLATFORM_DATABASE_URL"] = TEST_PLATFORM_DATABASE_URL
os.environ["SCANNER_DATABASE_URL"] = TEST_SCANNER_DATABASE_URL
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("JWT_EXPIRE_MINUTES", "60")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# /auth/register's per-IP rate limit (app/config.py, app/services/rate_limit.py)
# would otherwise trip almost immediately: httpx's ASGITransport reports the
# same synthetic client address for every request in the whole test session,
# and nearly every test file registers at least one company. Disabled here,
# same pattern as the other test-environment overrides in this file — the
# limiter itself is covered by its own dedicated test, not by leaving it live
# against the shared-IP test client.
os.environ.setdefault("REGISTER_RATE_LIMIT_ENABLED", "false")
# Same reasoning for /auth/login, and more acutely: every test client shares
# 127.0.0.1 under httpx's ASGITransport, and the suite logs in far more often
# than it registers, so a live per-IP login limiter would start failing tests
# purely on suite size. Both login limiters are covered by their own
# dedicated tests, which re-enable them scoped to themselves.
os.environ.setdefault("LOGIN_RATE_LIMIT_ENABLED", "false")
# Task 4.3: must be a real, valid Fernet key (not an arbitrary string like
# "test-secret" above) — app.services.token_encryption constructs
# Fernet(settings.integration_token_encryption_key.encode()) at import time,
# which raises immediately on a malformed key.
os.environ.setdefault("INTEGRATION_TOKEN_ENCRYPTION_KEY", "Rewy1h1FRZkZ2sxynenqVW39Vu1r573swS_UOr1uiUk=")
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
def _fresh_redis_client():
    """Rebind `app.services.rate_limit`'s module-level Redis client to each
    test's own event loop.

    A `redis.asyncio.Redis` is bound to the loop that was running when it
    was constructed; reused from a later test's loop it raises "Event loop
    is closed". That module's `_reset_redis_client_for_tests` docstring has
    the full diagnosis, and until now a couple of rate-limiter tests called
    it by hand — enough, because the only Redis-touching route was
    `/auth/register`, whose limiter conftest disables suite-wide, so the
    singleton was rarely constructed at all.

    Adding the login and TOTP limiters made every authenticated test path
    touch Redis, which turned that latent hazard into real failures in
    `test_mfa_totp.py`. Resetting here fixes the whole class at once rather
    than adding an opt-out flag per limiter: `redis.from_url` is lazy (no
    socket until the first command), so tests that never touch Redis pay
    nothing for this.

    Application code is unaffected — one uvicorn process has one event loop
    for its lifetime, which is exactly why the singleton is correct there.
    """
    from app.services import rate_limit

    rate_limit._reset_redis_client_for_tests()
    yield
    rate_limit._reset_redis_client_for_tests()


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
    RLS. Teardown order matters (rollback releases every Postgres-side lock
    this session could hold, which is what a LATER test's TRUNCATE-based
    _clean_tables cleanup needs) — see the inline comments below for why
    each teardown step is guarded and why loop_scope/poolclass are set.
    """
    # loop_scope="function" is required, not stylistic: pytest.ini's
    # asyncio_default_fixture_loop_scope=session governs FIXTURES only, not
    # test functions (which default to a fresh per-function loop). Without
    # this override, db_session ran on the session-scoped loop while its
    # calling test ran on its own per-function loop — reproduced as
    # `RuntimeError: Event loop is closed` raised from inside
    # session.rollback() once the test's own loop closed at test end
    # (confirmed via print-instrumented teardown: the "after rollback"
    # print never fired). Pinning to the test's own loop scope eliminates
    # the mismatch entirely.
    #
    # poolclass=NullPool: no connection pooling — every checkout opens a
    # fresh physical connection, every checkin closes it immediately. This
    # fixture is used rarely (a handful of tests, one session each), so
    # there's no performance reason to pool, and a pooled connection was a
    # separate proven cause of a hang: a connection returned to the pool
    # without its transaction fully unwound left a later test's TRUNCATE-
    # based _clean_tables cleanup blocked indefinitely — reproduced via
    # pg_locks/pg_stat_activity, showing _clean_tables' own TRUNCATE
    # waiting on exactly that connection's lock.
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = session_factory()
    try:
        yield session
    finally:
        # Nested try/finally, not three flat awaits: rollback is what
        # actually matters for the next test (it's what releases every
        # Postgres-side lock this session could hold) — if close() or
        # dispose() themselves raise (the same class of failure this
        # fixture was built to survive, see loop_scope comment above),
        # rollback must still have already run, and the later steps must
        # still each get attempted independently rather than one failure
        # skipping the rest.
        try:
            await session.rollback()
        finally:
            try:
                await session.close()
            finally:
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

    # The platform console's engine (migration 0023) is a second module-level
    # singleton with the same lifetime, and it hits the same hazard from the
    # other direction: a connection pooled during one test's event loop and
    # reused in the next raises "got Future ... attached to a different loop"
    # from inside the request, which surfaces as a 500 on a /platform route
    # rather than anything pointing at pooling. Disposed here for the same
    # reason and in the same place as app.db's engine above.
    from app.core.platform_db import platform_engine

    if platform_engine is not None:
        await platform_engine.dispose()


async def set_subscription_tier(company_id, tier) -> None:
    """Task 5.2 (tier-gating spec, Section 5): flips a registered company's
    subscription tier via the RLS-exempt owner connection. Registration can
    only ever produce trialing/pro (docs/08 Section 5), so any test that
    exercises an Enterprise-gated module (accounting, integrations,
    child-branch creation) — or a Starter-blocked scenario — sets the tier
    it needs explicitly with this. Same owner-connection test-setup
    rationale as _clean_tables above and the tenant-isolation files'
    _insert_*_directly helpers. Accepts company_id as str or UUID (asyncpg
    takes either for a uuid column, so no conversion happens here).

    The UPDATE-1 assertion matters (Task 5.2 code-quality review): a wrong
    or stale company_id would otherwise silently update zero rows, leaving
    the company at its registration default (pro) — and a "starter is
    blocked" test would still pass, because pro is blocked from the same
    Enterprise module too, silently never testing starter at all. Same
    guard scripts/e2e_smoke_test.py's _set_subscription_status already has."""
    conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        result = await conn.execute(
            "UPDATE subscriptions SET tier = $1 WHERE company_id = $2",
            tier,
            company_id,
        )
        assert result == "UPDATE 1", (
            f"expected exactly one subscriptions row for company_id={company_id!r}, got {result!r}"
        )
    finally:
        await conn.close()


async def grant_client_access(http_client, admin, *, project_id=None, lead_id=None, email):
    """Grant a client-role user access to a Project or a Lead.

    Migration 0019 gave the `client` role row-level scoping: a client sees
    nothing on a project or lead they aren't a member of. That makes this a
    setup step for every client-role test in the suite, so it lives here
    rather than being re-derived per file — including the `/companies/members`
    lookup that turns an email into the `user_id` the grant needs (test
    helpers commonly keep only the auth headers).

    Exactly one of `project_id`/`lead_id`.
    """
    assert (project_id is None) != (lead_id is None), "pass exactly one of project_id/lead_id"

    members = await http_client.get("/companies/members", headers=admin["headers"])
    assert members.status_code == 200, members.text
    user_id = next(m["user_id"] for m in members.json()["items"] if m["email"] == email)

    path = f"/projects/{project_id}/clients" if project_id else f"/leads/{lead_id}/clients"
    granted = await http_client.post(path, json={"user_id": user_id}, headers=admin["headers"])
    assert granted.status_code == 201, granted.text
    return granted.json()


async def register_and_login(client, company_name, email, *, tier=None) -> dict:
    """Register a company, log its admin in, hand back what tests need.

    53 of the 90 test files had grown their own copy of this, in **24
    distinct variants** — the drift is the finding, not the duplication.
    They differed along four axes:

      * whether `user_id` came back in the dict (some callers need it);
      * whether the register/login responses were status-asserted at all;
      * whether a subscription tier was set afterwards, and to what;
      * cosmetics (`body['access_token']` vs `login.json()[...]`).

    This is the union, deliberately, rather than the intersection:

      * `user_id` is ALWAYS returned. An extra key costs a caller nothing;
        omitting it is what forced a second variant to exist.
      * both responses are ALWAYS asserted. This is the only behavioural
        change, and it is strictly better — without it a 500 during setup
        surfaced as a confusing `KeyError: 'company_id'` several frames
        later, in a test that had nothing to do with the real failure.
      * `tier` is the ONE knob, because registration can only ever produce
        trialing/pro (docs/08 Section 5) and a test exercising an
        Enterprise-gated module genuinely has to say so. It delegates to
        `set_subscription_tier` above rather than reimplementing it.

    One optional keyword, not four booleans. Variants that need something
    genuinely different keep a two-line wrapper in their own file, which
    is honest about being a local exception rather than growing this
    signature to cover it.

    Lives in conftest because that is already where this suite's shared
    setup lives (`set_subscription_tier`, `grant_client_access`) and 65
    files already import from here. A pytest fixture would avoid the
    import entirely, but would mean touching the signature of every test
    that uses it — a far larger and more error-prone diff for a smaller
    gain.
    """
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    login = await client.post("/auth/login", json={"email": email, "password": "supersecret123"})
    assert login.status_code == 200, login.text

    if tier is not None:
        await set_subscription_tier(register.json()["company_id"], tier)

    return {
        "company_id": register.json()["company_id"],
        "user_id": register.json()["user_id"],
        "email": email,
        "headers": {"Authorization": f"Bearer {login.json()['access_token']}"},
    }
