"""Regression sweep asserting Row-Level Security actually covers every
tenant table — the companion gate to `test_company_id_index_coverage.py`.

That test catches a new tenant table that ships without a `company_id`
*index* (a performance gap). This one catches the far more dangerous
omission it cannot see: a new tenant table that ships with **no RLS policy
at all**, or with a policy that doesn't really scope by tenant. RLS is the
enforcement boundary in this codebase — `CLAUDE.md`'s "any new tenant-owned
table needs its own RLS policy in the same migration that creates it" is,
without this file, a convention enforced only by review. A table created
without `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` is readable and
writable across every tenant by the runtime `app_user` role, and nothing
else in the suite notices unless someone remembered to write a bespoke
isolation test for that particular table.

Like its sibling, this is deliberately catalog-driven and forward-looking:
it special-cases nothing by table name except through the two explicit
allowlists below, each of which forces a *conscious* decision (and a code
review) when a new table is added.

Four properties are asserted:

  1. every table with a `company_id` column has RLS enabled and at least
     one policy;
  2. each of those tables has a `FOR ALL` policy whose USING **and** check
     expressions both call a tenant-scoping function — this is what catches
     a policy that exists but reads `USING (true)`;
  3. any *additional* permissive policy (Postgres ORs permissive policies
     together, so each one only ever widens access) is on the allowlist;
  4. every table *without* a `company_id` column either has RLS enabled or
     is on the known-non-tenant allowlist.

All four read Postgres's own catalogs through the owner connection, the
same way `test_company_id_index_coverage.py` does.
"""
import asyncpg
import pytest

from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

# The SECURITY DEFINER functions that implement the tenant boundary
# (defined in migrations 0001 and 0010). A policy expression that mentions
# neither of these is not scoping by tenant, whatever else it does.
TENANT_SCOPING_FUNCTIONS = ("get_all_descendant_ids", "get_root_company_id")

# Tables with no `company_id` column that legitimately carry no RLS policy.
# Adding a table here is a deliberate assertion that it holds no
# tenant-owned data.
#
#   users / refresh_tokens — a user identity is global, not tenant-owned:
#       one person can be a member of several companies (that's what
#       `company_users` models), and login has to find the user *before*
#       any tenant context exists. Access is scoped by the queries
#       themselves (always by email or by the authenticated user's own id),
#       not by RLS.
#   alembic_version — Alembic's own bookkeeping, one row, no tenant data.
# `password_reset_tokens` joins users/refresh_tokens here for the same
# reason (migration 0028): a reset is requested with no session, no
# X-Tenant-ID and no membership resolved, so there is no tenant to scope
# the row to. The credential is protected by being a hash of a secret that
# lives in one email, not by a policy.
NON_TENANT_TABLES = frozenset(
    {"users", "refresh_tokens", "password_reset_tokens", "alembic_version"}
)

# Permissive policies that intentionally grant access on a basis *other*
# than the tenant tree. Each widens what the `tenant_isolation` policy on
# the same table allows (permissive policies are ORed), so each one is a
# deliberate, reviewed exception rather than an oversight.
#
#   company_users.self_membership — `get_current_user` must read the
#       caller's own memberships to decide whether the claimed
#       `X-Tenant-ID` is legitimate. That lookup necessarily happens
#       *before* `set_current_tenant`, so it cannot be tenant-scoped
#       without a chicken-and-egg failure; it is scoped to
#       `app.current_user_id` instead.
#   invitations.invitation_probe — accepting an invitation is an
#       unauthenticated flow: the recipient has no session and no tenant.
#       The policy is scoped to a single id the caller must already know
#       (`app.probing_invitation_id`), set from the opaque token.
NON_TENANT_SCOPED_POLICIES = frozenset(
    {
        ("company_users", "self_membership"),
        ("invitations", "invitation_probe"),
    }
)

_TENANT_TABLE_RLS_QUERY = """
    SELECT t.relname AS table_name,
           t.relrowsecurity AS rls_enabled,
           (SELECT count(*) FROM pg_policy p WHERE p.polrelid = t.oid) AS policy_count
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public'
    WHERE t.relkind = 'r'
      AND EXISTS (
        SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = t.oid AND a.attname = 'company_id' AND NOT a.attisdropped
      )
    ORDER BY t.relname
"""

# `cmd` is the human-readable command ('ALL', 'SELECT', ...); `qual` is the
# USING expression and `with_check` the WITH CHECK expression, both already
# deparsed by the pg_policies view. A NULL with_check on a policy that
# permits writes means Postgres reuses `qual` as the check — safe, and
# handled by `_effective_check` below.
_POLICY_QUERY = """
    SELECT tablename, policyname, cmd, permissive, qual, with_check
    FROM pg_policies
    WHERE schemaname = 'public'
    ORDER BY tablename, policyname
"""

_NON_TENANT_TABLE_QUERY = """
    SELECT t.relname AS table_name, t.relrowsecurity AS rls_enabled
    FROM pg_class t
    JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'public'
    WHERE t.relkind = 'r'
      AND NOT EXISTS (
        SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = t.oid AND a.attname = 'company_id' AND NOT a.attisdropped
      )
    ORDER BY t.relname
"""


async def _fetch(query):
    conn = await asyncpg.connect(OWNER_DSN)
    try:
        return await conn.fetch(query)
    finally:
        await conn.close()


@pytest.fixture(scope="module")
async def tenant_tables():
    return await _fetch(_TENANT_TABLE_RLS_QUERY)


@pytest.fixture(scope="module")
async def policies():
    return await _fetch(_POLICY_QUERY)


def _is_tenant_scoped(expression) -> bool:
    return expression is not None and any(
        fn in expression for fn in TENANT_SCOPING_FUNCTIONS
    )


def _effective_check(policy) -> str | None:
    """The expression Postgres applies to rows being written.

    A policy with no explicit WITH CHECK falls back to its USING clause,
    so the two are equivalent for our purposes — but only when USING is
    itself present.
    """
    return policy["with_check"] if policy["with_check"] is not None else policy["qual"]


def _every_defined_expression_is_tenant_scoped(policy) -> bool:
    """True when the policy constrains *only* by the tenant tree.

    Per-command policies define one side or the other — an INSERT policy has
    no USING, a SELECT policy no WITH CHECK — so this checks every
    expression that exists rather than demanding both, which would flag a
    perfectly good `FOR INSERT ... WITH CHECK (company_id IN ...)`.
    """
    defined = [e for e in (policy["qual"], policy["with_check"]) if e is not None]
    return bool(defined) and all(_is_tenant_scoped(e) for e in defined)


async def test_every_tenant_table_has_rls_enabled_with_at_least_one_policy(tenant_tables):
    unprotected = [
        row["table_name"]
        for row in tenant_tables
        if not row["rls_enabled"] or row["policy_count"] == 0
    ]
    assert unprotected == [], (
        "these tables hold tenant-owned rows (they have a company_id column) but "
        "RLS is not enabled on them, or it is enabled with no policy attached — "
        "either way the runtime app_user role can read and write every tenant's "
        "rows in them. Add `ALTER TABLE <t> ENABLE ROW LEVEL SECURITY` plus a "
        "tenant_isolation policy to the migration that creates the table: "
        f"{unprotected!r}"
    )


async def test_every_tenant_table_is_scoped_by_a_for_all_tenant_policy(
    tenant_tables, policies
):
    by_table: dict[str, list] = {}
    for policy in policies:
        by_table.setdefault(policy["tablename"], []).append(policy)

    unscoped = []
    for row in tenant_tables:
        table = row["table_name"]
        scoping = [
            p
            for p in by_table.get(table, [])
            if p["cmd"] == "ALL"
            and _is_tenant_scoped(p["qual"])
            and _is_tenant_scoped(_effective_check(p))
        ]
        if not scoping:
            unscoped.append(table)

    assert unscoped == [], (
        "these tables have RLS policies, but none is a FOR ALL policy whose "
        "USING *and* WITH CHECK expressions both call one of "
        f"{TENANT_SCOPING_FUNCTIONS} — so reads, writes, or both are not "
        "actually constrained to the caller's tenant tree (a policy of "
        "`USING (true)` would look protected to the test above and leak "
        f"everything): {unscoped!r}"
    )


async def test_additional_permissive_policies_are_deliberate(tenant_tables, policies):
    tenant_table_names = {row["table_name"] for row in tenant_tables}

    unexpected = [
        (p["tablename"], p["policyname"])
        for p in policies
        if p["tablename"] in tenant_table_names
        and p["permissive"] == "PERMISSIVE"
        and not _every_defined_expression_is_tenant_scoped(p)
        and (p["tablename"], p["policyname"]) not in NON_TENANT_SCOPED_POLICIES
    ]

    assert unexpected == [], (
        "these permissive policies on tenant tables are not scoped by the tenant "
        "tree, and are not on this module's reviewed allowlist. Postgres ORs "
        "permissive policies together, so each one can only *widen* what "
        "tenant_isolation allows — an accidental one is a cross-tenant read or "
        "write. Either scope it by tenant or add it to "
        f"NON_TENANT_SCOPED_POLICIES with the reason: {unexpected!r}"
    )


async def test_tables_without_a_company_id_column_are_accounted_for():
    rows = await _fetch(_NON_TENANT_TABLE_QUERY)

    unaccounted = [
        row["table_name"]
        for row in rows
        if not row["rls_enabled"] and row["table_name"] not in NON_TENANT_TABLES
    ]
    assert unaccounted == [], (
        "these tables have neither a company_id column nor RLS enabled, so the "
        "sweep above cannot see them at all. A tenant table that models "
        "ownership some other way (or simply forgot the column) would hide here. "
        "Give it a company_id column and a policy, or add it to "
        f"NON_TENANT_TABLES declaring it holds no tenant data: {unaccounted!r}"
    )


async def test_companies_policies_are_tenant_scoped_and_rls_is_not_forced(policies):
    """`companies` is the root of the tenant tree: it has no `company_id`
    column (it *is* the company), so the sweeps above skip it, and its
    policies are split per-command rather than FOR ALL — the split exists so
    a brand-new top-level company can be INSERTed before any tenant context
    exists.

    FORCE ROW LEVEL SECURITY must stay off. `get_all_descendant_ids` is
    SECURITY DEFINER and queries `companies` internally; every policy here
    calls that function, so forcing RLS on the owner would make the
    function's own scan re-trigger the policy that called it — infinite
    recursion until "stack depth limit exceeded", on every query against
    every RLS-protected table (see migration 0001's docstring, which
    records this being hit for real).
    """
    company_policies = [p for p in policies if p["tablename"] == "companies"]
    assert company_policies, "companies lost its RLS policies entirely"

    unscoped = [
        p["policyname"]
        for p in company_policies
        if not _every_defined_expression_is_tenant_scoped(p)
    ]
    assert unscoped == [], (
        "these policies on `companies` have an expression that does not call a "
        f"tenant-scoping function — the tenant tree's own root is unguarded: {unscoped!r}"
    )

    forced = await _fetch(
        "SELECT relforcerowsecurity FROM pg_class WHERE relname = 'companies'"
    )
    assert forced[0]["relforcerowsecurity"] is False, (
        "FORCE ROW LEVEL SECURITY is on for `companies`, which makes "
        "get_all_descendant_ids recurse into the policy that calls it — expect "
        "'stack depth limit exceeded' on every tenant query"
    )


async def test_app_user_cannot_bypass_rls():
    """None of the above means anything if the runtime role is exempt.

    `app_user` is the role every application connection uses (`DATABASE_URL`);
    granting it BYPASSRLS or SUPERUSER would silently turn every policy in
    this file into decoration. The tenant-isolation suites would start
    failing too, but with a confusing "the policy is wrong" signal — this
    names the real cause.
    """
    rows = await _fetch(
        "SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'app_user'"
    )
    assert rows, "the app_user role is missing — migration 0001 creates it"
    assert not rows[0]["rolbypassrls"] and not rows[0]["rolsuper"], (
        "app_user has BYPASSRLS or SUPERUSER: every RLS policy in the database "
        "is inert for the role the application actually connects as"
    )
