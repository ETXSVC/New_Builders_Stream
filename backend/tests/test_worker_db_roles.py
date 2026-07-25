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
