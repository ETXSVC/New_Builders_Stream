"""Task 3.24: block_if_read_only (design spec Section 6). Unit-level tests
against the dependency function directly — no HTTP route needed yet, since
no route is wired to it until later tasks. A later task adds the
completeness introspection test and an end-to-end behavior test once
routes exist."""
import uuid
from dataclasses import dataclass

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.deps import CurrentUser, block_if_read_only
from tests.conftest import TEST_APP_DATABASE_URL, TEST_DATABASE_URL


@dataclass
class _FakeRequest:
    method: str


async def _make_current_user_for_status(status_value: str) -> tuple[CurrentUser, callable]:
    """Builds a real Subscription row with the given status, then a real
    app_user-role session scoped to that company — not a mock session — so
    this test exercises the actual RLS-backed get_root_company_id query
    path, not a stubbed one."""
    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    company_id = uuid.uuid4()
    try:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO companies (id, parent_id, name) VALUES (:id, NULL, 'RO Test Co')"),
                {"id": company_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO subscriptions "
                    "(id, company_id, stripe_customer_id, stripe_subscription_id, tier, status, included_seats) "
                    "VALUES (:id, :company_id, 'cus_x', 'sub_x', 'pro', :status, 10)"
                ),
                {"id": uuid.uuid4(), "company_id": company_id, "status": status_value},
            )
    finally:
        # `async with owner_engine.begin()` already rolls back and releases
        # the connection on an exception, but dispose() itself sat outside
        # that block — an INSERT failure would have skipped it entirely,
        # leaving the engine's pool undisposed. Same "always release what
        # you open" discipline as `cleanup()` below, just for setup instead
        # of teardown.
        await owner_engine.dispose()

    app_engine = create_async_engine(TEST_APP_DATABASE_URL, pool_pre_ping=True)
    session_factory = async_sessionmaker(app_engine, expire_on_commit=False, class_=AsyncSession)
    session = session_factory()
    await session.execute(
        text("SELECT set_config('app.current_tenant', :tenant, true)"), {"tenant": str(company_id)}
    )
    current = CurrentUser(user=None, company_id=company_id, role="admin", session=session)

    async def cleanup() -> None:
        # session.execute() above implicitly opens a transaction that is
        # never committed/rolled back by this helper's own callers — engine
        # .dispose() alone does NOT roll back or close an in-flight session
        # first, so without this, the underlying connection sits
        # "idle in transaction" in Postgres indefinitely, holding locks
        # that block a LATER test's TRUNCATE-based cleanup fixture
        # (conftest.py's _clean_tables) forever. Reproduced directly: this
        # exact bug hung a full pytest run for over an hour with Postgres
        # itself perfectly healthy — pg_stat_activity showed two
        # "idle in transaction" app_user connections from this helper
        # blocking a TRUNCATE. Roll back (not commit — nothing here should
        # persist past the test) before disposing the engine.
        await session.rollback()
        await session.close()
        await app_engine.dispose()

    return current, cleanup


async def test_get_requests_always_pass_through():
    current, cleanup = await _make_current_user_for_status("canceled")
    try:
        await block_if_read_only(request=_FakeRequest(method="GET"), current=current)
    finally:
        await cleanup()


async def test_post_passes_when_trialing():
    current, cleanup = await _make_current_user_for_status("trialing")
    try:
        await block_if_read_only(request=_FakeRequest(method="POST"), current=current)
    finally:
        await cleanup()


async def test_post_passes_when_active():
    current, cleanup = await _make_current_user_for_status("active")
    try:
        await block_if_read_only(request=_FakeRequest(method="POST"), current=current)
    finally:
        await cleanup()


async def test_post_blocked_when_past_due():
    current, cleanup = await _make_current_user_for_status("past_due")
    try:
        with pytest.raises(HTTPException) as exc_info:
            await block_if_read_only(request=_FakeRequest(method="POST"), current=current)
        assert exc_info.value.status_code == 403
    finally:
        await cleanup()


async def test_post_blocked_when_canceled():
    current, cleanup = await _make_current_user_for_status("canceled")
    try:
        with pytest.raises(HTTPException) as exc_info:
            await block_if_read_only(request=_FakeRequest(method="POST"), current=current)
        assert exc_info.value.status_code == 403
    finally:
        await cleanup()


def test_every_write_route_has_block_if_read_only_except_deliberate_exclusions():
    """Task 3.28: rather than hand-maintaining a list of every write route
    to check (which silently rots as new routers get added after this
    feature ships), this introspects the LIVE FastAPI app's own route
    table. Deliberate exclusions, each with its own reason documented at
    the retrofit site: /auth/register (creates the subscription itself),
    /auth/login (must work even when read-only, so an admin can find out
    and go fix it), /webhooks/stripe (how a lapsed subscription's status
    even gets updated back after a real payment succeeds — Task 3.21),
    /subscriptions/portal-session (an Admin must be able to reach Stripe's
    own portal to FIX a lapsed subscription, which is itself a POST),
    /invitations/{id}/accept (structurally has no CurrentUser — Task
    3.25's own note), /integrations/{provider}/callback (same reason —
    Task 4.9's own note), and the three token-lifecycle routes
    /auth/refresh, /auth/logout, /auth/change-password (Task 6.x, spec
    docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md:
    session management must keep working for a read-only company for the
    same reason /auth/login does — and change-password especially so,
    since rotating a compromised password must never be blocked by a
    lapsed subscription; refresh and logout also carry no CurrentUser at
    all, the refresh token itself is the credential), and the three MFA
    routes /auth/mfa/enroll, /auth/mfa/activate, /auth/mfa/disable (Task
    7.x, spec docs/superpowers/specs/2026-07-16-mfa-totp-design.md:
    credential/session management must keep working for a read-only
    company — the same clause as the token-lifecycle routes; a lapsed
    subscription must never stop a user from strengthening, proving, or
    recovering control of their own second factor).

    Coverage caveats, for whoever extends this codebase later: (1) this
    walks `app.routes` and checks each route's own top-level
    `route.dependant.dependencies` — a route mounted as a separate ASGI
    sub-app (`app.mount(...)`, not used anywhere in this codebase today)
    has no `.dependant`/`.methods` in the shape this loop expects and
    would be silently skipped, not flagged. (2) the check is one level
    deep — `block_if_read_only` nested inside some OTHER composed
    dependency (rather than declared directly via `Depends()` on the
    route, which is how every retrofit in this codebase does it today)
    would not appear in this flat list even though FastAPI still executes
    it at request time, producing a false "missing" report. Both fail
    SAFE (a false positive you have to investigate, not a silent gap) —
    but if either pattern gets introduced, this test will need updating
    to match.
    """
    from app.core.deps import block_if_read_only
    from app.main import app

    excluded_paths = {
        "/auth/register",
        "/auth/login",
        "/auth/refresh",
        "/auth/logout",
        "/auth/change-password",
        # All three MFA routes at once, disable included even though it
        # lands in a later task — the completeness loop below only ever
        # tests route.path membership for routes that EXIST in app.routes,
        # so an exclusion for a not-yet-registered path is inert until the
        # route appears.
        "/auth/mfa/enroll",
        "/auth/mfa/activate",
        "/auth/mfa/disable",
        "/webhooks/stripe",
        "/subscriptions/portal-session",
        "/invitations/{invitation_id}/accept",
        "/integrations/{provider}/callback",
    }

    missing = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods or methods.isdisjoint({"POST", "PUT", "PATCH", "DELETE"}):
            continue
        if route.path in excluded_paths:
            continue

        dependant_calls = [dep.call for dep in route.dependant.dependencies]
        # block_if_read_only may be a direct dependency OR nested under
        # another dependency's own sub-dependencies (FastAPI flattens these
        # at request time, but `route.dependant.dependencies` only lists
        # the route's own top-level Depends(...) list) — every retrofit in
        # Tasks 3.25-3.27 added it as a top-level Depends(...), so a direct
        # membership check is sufficient and precise for this codebase's
        # actual usage.
        if block_if_read_only not in dependant_calls:
            missing.append(f"{sorted(methods)} {route.path}")

    assert missing == [], f"Write routes missing block_if_read_only: {missing}"


async def test_write_route_returns_403_when_subscription_is_past_due(client):
    """One representative end-to-end proof (via leads.create_lead) that a
    lapsed subscription actually blocks a real HTTP write, on top of the
    unit-level dependency tests (Task 3.24) and the completeness
    introspection test above."""
    register = await client.post(
        "/auth/register",
        json={
            "company_name": "Lapsed Co",
            "admin_email": "lapsed-admin@ro.test",
            "admin_password": "correct horse battery staple",
            "admin_full_name": "Lapsed Admin",
        },
    )
    assert register.status_code == 201, register.text
    company_id = register.json()["company_id"]
    login = await client.post(
        "/auth/login", json={"email": "lapsed-admin@ro.test", "password": "correct horse battery staple"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    owner_engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as conn:
            await conn.execute(
                text("UPDATE subscriptions SET status = 'past_due' WHERE company_id = :cid"),
                {"cid": company_id},
            )
    finally:
        await owner_engine.dispose()

    response = await client.post(
        "/leads",
        json={
            "contact_name": "Someone",
            "project_name": "A Project",
            "email": "lead@ro.test",
            "project_type": "residential",
        },
        headers=headers,
    )

    # Not just the status code: create_lead's require_role(*_LEAD_ROLES)
    # dependency ALSO raises 403 (for a role not in _LEAD_ROLES), and runs
    # BEFORE block_if_read_only on this same route. Asserting on the detail
    # message distinguishes "blocked because read-only" from "blocked
    # because RBAC" — without this, a future regression that silently
    # dropped block_if_read_only from create_lead while some other 403 path
    # coincidentally fired would leave this test passing for the wrong
    # reason, defeating its purpose as a completeness safety net.
    assert response.status_code == 403
    assert "subscription" in response.json()["detail"].lower()
