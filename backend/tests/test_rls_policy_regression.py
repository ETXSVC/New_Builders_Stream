import uuid

import asyncpg
import pytest

from tests.conftest import TEST_DATABASE_URL


async def _register(client, company_name, email):
    register = await client.post(
        "/auth/register",
        json={
            "company_name": company_name,
            "admin_full_name": "Test Admin",
            "admin_email": email,
            "admin_password": "supersecret123",
        },
    )
    return register.json()["company_id"]


async def test_rls_policy_itself_blocks_cross_tenant_row_visibility(client):
    """Connects as app_user directly (bypassing the FastAPI app entirely) to
    prove the POLICY blocks access, not some application-layer WHERE clause.
    Then disables RLS as the table owner and confirms the same query starts
    returning the row — showing the policy, not luck, was responsible."""
    company_a_id = await _register(client, "Company A", "rls-a@test.com")
    company_b_id = await _register(client, "Company B", "rls-b@test.com")

    app_conn = await asyncpg.connect(
        f"postgresql://app_user:app_password@localhost:5432/builders_stream_test"
    )
    try:
        # set_config(), not `SET app.current_tenant = $1` — see set_current_tenant's
        # docstring in app/db.py (Task 3) for why a bound parameter there is a syntax error.
        await app_conn.execute("SELECT set_config('app.current_tenant', $1, false)", company_a_id)
        visible_as_a = await app_conn.fetchrow(
            "SELECT id FROM companies WHERE id = $1", company_b_id
        )
        assert visible_as_a is None, "RLS should block Company A's session from seeing Company B"
    finally:
        await app_conn.close()

    owner_conn = await asyncpg.connect(TEST_DATABASE_URL.replace("+asyncpg", ""))
    try:
        await owner_conn.execute("ALTER TABLE companies DISABLE ROW LEVEL SECURITY")
        app_conn2 = await asyncpg.connect(
            f"postgresql://app_user:app_password@localhost:5432/builders_stream_test"
        )
        try:
            await app_conn2.execute("SELECT set_config('app.current_tenant', $1, false)", company_a_id)
            visible_with_rls_off = await app_conn2.fetchrow(
                "SELECT id FROM companies WHERE id = $1", company_b_id
            )
            assert visible_with_rls_off is not None, (
                "Sanity check failed: Company B's row should exist and be "
                "visible once RLS is off — if this fails, the row itself is "
                "missing, which means the test setup (not the policy) is broken."
            )
        finally:
            await app_conn2.close()
    finally:
        # ALWAYS restore RLS even if the assertion above fails, so this test
        # can't leave the database in an insecure state for other tests.
        await owner_conn.execute("ALTER TABLE companies ENABLE ROW LEVEL SECURITY")
        await owner_conn.close()


async def test_cannot_reparent_company_across_tenant_boundary(client):
    """Regression test for a real bug found in Task 5's code-quality review:
    companies.tenant_update originally had no WITH CHECK, which let a tenant
    UPDATE its own company's parent_id to point at an unrelated tenant's
    company, re-parenting itself out of its own tree and into theirs — a full
    tenant-boundary bypass via UPDATE that INSERT/SELECT policies didn't
    have. This connects as app_user directly, the same way the cross-tenant
    visibility test above does, so it exercises the real RLS policy rather
    than any application-layer guard."""
    company_a_id = await _register(client, "Company A", "reparent-a@test.com")
    company_b_id = await _register(client, "Company B", "reparent-b@test.com")

    app_conn = await asyncpg.connect(
        f"postgresql://app_user:app_password@localhost:5432/builders_stream_test"
    )
    try:
        await app_conn.execute("SELECT set_config('app.current_tenant', $1, false)", company_a_id)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await app_conn.execute(
                "UPDATE companies SET parent_id = $1 WHERE id = $2", company_b_id, company_a_id
            )
    finally:
        await app_conn.close()


async def test_current_tenant_guc_does_not_poison_later_queries_on_same_connection():
    """Regression test for design decision #7: a custom GUC like
    app.current_tenant, once set via SET LOCAL/set_config(is_local=true) on a
    connection and committed, reverts to '' (not NULL) for the rest of that
    connection's life — not just the one transaction that set it. Casting ''
    to ::uuid raises an unhandled error rather than the policy simply denying
    access. This bit login() in practice: a request that never sets
    app.current_tenant (only app.current_user_id) can be served a pooled
    connection previously used by a request that did (register()) and
    committed. Drives the mechanism directly on one raw connection so this
    keeps catching a regression even if pool configuration changes in a way
    that stops naturally reusing connections across register() and login()."""
    company_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    conn = await asyncpg.connect(
        f"postgresql://app_user:app_password@localhost:5432/builders_stream_test"
    )
    try:
        # First transaction: sets app.current_tenant (like register() does) and commits.
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_tenant', $1, true)", company_id)
            await conn.execute(
                "INSERT INTO companies (id, parent_id, name) VALUES ($1, NULL, 'Poison Test Co')",
                company_id,
            )
            await conn.execute(
                "INSERT INTO users (id, email, password_hash) VALUES ($1, 'poison-guc@test.com', 'x')",
                user_id,
            )
            await conn.execute(
                "INSERT INTO company_users (company_id, user_id, role, created_at) "
                "VALUES ($1, $2, 'admin', now())",
                company_id,
                user_id,
            )

        # Second transaction, same connection: only sets app.current_user_id
        # (like login() does). app.current_tenant is never touched here, but
        # this connection already saw it set once, in the transaction above.
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", user_id)
            # Without the NULLIF guard, this raises InvalidTextRepresentationError
            # instead of returning a row via the self_membership policy.
            row = await conn.fetchrow(
                "SELECT company_id FROM company_users WHERE user_id = $1", user_id
            )
            assert row is not None, (
                "self_membership should still allow a user to see their own "
                "membership row even though app.current_tenant was never set "
                "in this transaction and is 'poisoned' from the prior one"
            )
    finally:
        await conn.execute("DELETE FROM company_users WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        await conn.execute("DELETE FROM companies WHERE id = $1", company_id)
        await conn.close()
