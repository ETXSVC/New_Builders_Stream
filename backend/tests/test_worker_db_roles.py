"""No application module may connect as the Postgres table owner.

Migration 0020 moved the four background jobs off `migrations_database_url`
— the `postgres` role that owns every table, is exempt from RLS, and can
drop tables or rewrite the policies protecting them. Until then the entire
async job layer held the highest privilege in the database to do work that
only ever reads and writes rows, and its tenant isolation rested on
hand-written `WHERE company_id = ...` clauses with nothing behind them.

`CLAUDE.md` already said "never reach for an owner-role connection in
application code." These tests are what make that checkable, in the same
catalog-driven spirit as `test_rls_policy_coverage.py`: a future module
that quietly opens an owner engine fails here rather than in review.

Alembic itself is the one legitimate consumer of that URL, and it is not
application code — `migrations/env.py` is deliberately out of scope below.
"""
import ast
import pathlib
import uuid

import asyncpg
import pytest

from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"

# The one place the owner URL may still be *named*: scanner_db.py falls
# back to it when SCANNER_DATABASE_URL is unset, so a deployment upgrading
# through 0020 keeps working instead of failing to start its worker. That
# fallback is a migration aid with a documented end state, not a licence
# for new modules to do the same.
_ALLOWED_FALLBACK = {"tasks/scanner_db.py"}


def _modules_reading_the_owner_url() -> list[str]:
    """Every app module that reads `settings.migrations_database_url`.

    An AST walk rather than a text search, so the docstrings in
    `app/tasks/*.py` — which discuss this URL at length, and should — do
    not register as usage.
    """
    offenders = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative = path.relative_to(APP_ROOT).as_posix()
        if relative in _ALLOWED_FALLBACK:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "migrations_database_url":
                offenders.append(relative)
                break
    return offenders


def test_no_application_module_connects_as_the_table_owner():
    offenders = _modules_reading_the_owner_url()
    assert offenders == [], (
        "these modules read settings.migrations_database_url, which authenticates "
        "as the Postgres role that OWNS every table: exempt from RLS, and able to "
        "drop tables and rewrite the policies that enforce tenant isolation. A "
        "single-tenant job should resolve its tenant and use the ordinary app_user "
        "session (see app/tasks/accounting_sync.py); a genuinely cross-tenant scan "
        "should use the `scanner` connection in app/tasks/scanner_db.py: "
        f"{offenders!r}"
    )


async def _role_attributes(rolname: str):
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        return await conn.fetchrow(
            "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = $1",
            rolname,
        )
    finally:
        await conn.close()


async def test_scanner_role_can_see_every_tenant_but_owns_nothing():
    """`scanner` needs BYPASSRLS — a cross-tenant sweep is the job — but
    that is the ONLY elevated attribute it may hold."""
    row = await _role_attributes("scanner")
    assert row is not None, "the scanner role is missing — migration 0020 creates it"

    assert row["rolbypassrls"], "scanner cannot do a cross-tenant scan without BYPASSRLS"
    assert not row["rolsuper"], "scanner must not be a superuser"
    assert not row["rolcreatedb"], "scanner has no reason to create databases"
    assert not row["rolcreaterole"], "scanner has no reason to create roles"

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        owned = await conn.fetchval(
            "SELECT count(*) FROM pg_class c "
            "JOIN pg_roles r ON r.oid = c.relowner "
            "WHERE r.rolname = 'scanner'"
        )
    finally:
        await conn.close()
    assert owned == 0, (
        "scanner owns database objects, which would let it ALTER or DROP them and "
        "disable row security on the tables it scans — the whole point of the role "
        "is that it can read across tenants without being able to change the rules"
    )


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE projects DISABLE ROW LEVEL SECURITY",
        "DROP POLICY tenant_isolation ON projects",
        "DROP TABLE projects",
    ],
)
async def test_scanner_cannot_weaken_the_tenant_boundary(statement):
    """The properties above stated as behaviour: each of these is exactly
    what an attacker who reached the worker's credentials would try."""
    conn = await asyncpg.connect(OWNER_DSN.replace("postgres:devpassword", "scanner:scanner_password"))
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(statement)
    finally:
        await conn.close()


# =============================================================================
# companies.parent_id is immutable (migration 0021)
# =============================================================================


async def _company_ids(client):
    """A parent and a child, created through the real routes."""
    register = await client.post(
        "/auth/register",
        json={
            "company_name": "Reparent Co",
            "admin_full_name": "Admin",
            "admin_email": "reparent-admin@acme.test",
            "admin_password": "supersecret123",
        },
    )
    assert register.status_code == 201, register.text
    parent_id = register.json()["company_id"]

    from tests.conftest import set_subscription_tier

    await set_subscription_tier(parent_id, "enterprise")
    login = await client.post(
        "/auth/login", json={"email": "reparent-admin@acme.test", "password": "supersecret123"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    child = await client.post(
        f"/companies/{parent_id}/children", json={"name": "Branch"}, headers=headers
    )
    assert child.status_code == 201, child.text
    return parent_id, child.json()["id"]


async def test_detaching_a_child_company_to_a_new_root_is_refused(client):
    """The gap this closes: `tenant_update`'s WITH CHECK allows
    `parent_id IS NULL`, which on UPDATE means a child can be detached into
    a NEW ROOT — leaving its parent's descendant tree (so the parent
    silently loses data it owns) and arriving with no subscriptions row,
    which both block_if_read_only and tier_allows treat as fail-OPEN.

    Asserted through a direct write as `app_user`, because no route exposes
    parent_id — the point is that the DATABASE refuses it, so a future
    route cannot reintroduce the hole by accident.

    The tenant context is set to the PARENT first, which is what makes this
    a real test rather than a vacuous one: without it, `tenant_update`'s
    USING clause matches zero rows, the UPDATE is a silent no-op, and the
    trigger never fires — the assertion would pass while proving nothing.
    Scoped to the parent, RLS genuinely permits this row to be updated
    (a parent may act on its descendants), so the trigger is the only thing
    standing between that session and a detached branch.
    """
    parent_id, child_id = await _company_ids(client)

    conn = await asyncpg.connect(
        OWNER_DSN.replace("postgres:devpassword", "app_user:app_password")
    )
    try:
        await conn.execute("SELECT set_config('app.current_tenant', $1, false)", parent_id)

        # Sanity: this session really can update that row, so the failure
        # below is the trigger and not RLS quietly matching nothing.
        renamed = await conn.execute(
            "UPDATE companies SET name = 'Still Editable' WHERE id = $1", uuid.UUID(child_id)
        )
        assert renamed == "UPDATE 1", renamed

        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="immutable"):
            await conn.execute(
                "UPDATE companies SET parent_id = NULL WHERE id = $1", uuid.UUID(child_id)
            )
    finally:
        await conn.close()


async def test_renaming_a_company_still_works(client):
    """The obvious fix — dropping the `IS NULL` branch from the UPDATE
    policy — would break this: a root's parent_id IS NULL, so
    `NULL IN (SELECT ...)` is NULL and every root update would be denied.
    A trigger states "parent_id may not CHANGE" instead, which leaves
    ordinary updates alone."""
    parent_id, _child_id = await _company_ids(client)

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        await conn.execute(
            "UPDATE companies SET name = 'Renamed' WHERE id = $1", uuid.UUID(parent_id)
        )
        name = await conn.fetchval(
            "SELECT name FROM companies WHERE id = $1", uuid.UUID(parent_id)
        )
    finally:
        await conn.close()
    assert name == "Renamed"


async def test_even_the_table_owner_cannot_reparent(client):
    """A trigger applies to every role, unlike an RLS policy — so a
    migration or an operator with the owner connection also has to disable
    it deliberately rather than re-parent by accident."""
    parent_id, child_id = await _company_ids(client)

    conn = await asyncpg.connect(OWNER_DSN)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="immutable"):
            await conn.execute(
                "UPDATE companies SET parent_id = $1 WHERE id = $2",
                uuid.UUID(child_id),
                uuid.UUID(parent_id),
            )
    finally:
        await conn.close()
