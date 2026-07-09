"""Initial schema: companies, users, company_users, invitations, audit_log,
app_user role, and Row-Level Security policies.

Revision ID: 0001
Revises:
Create Date: 2026-07-07

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # --- Tables -----------------------------------------------------------
    op.create_table(
        "companies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("parent_id", UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_companies_parent_id", "companies", ["parent_id"])

    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String, nullable=False),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "company_users",
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "role IN ('admin','project_manager','field_crew','accountant','client')",
            name="ck_company_users_role",
        ),
    )

    op.create_table(
        "invitations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(50), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "role IN ('admin','project_manager','field_crew','accountant','client')",
            name="ck_invitations_role",
        ),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # No ondelete (defaults to RESTRICT) — see the matching comment on the
        # AuditLog ORM model (Task 4): CASCADE here would violate the audit log's
        # documented 7-year, never-deleted retention policy.
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("actor_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("log_metadata", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_audit_log_company_created", "audit_log", ["company_id", "created_at"])

    # --- Recursive descendant lookup (design decision #2/#3 depend on this) -
    #
    # SECURITY DEFINER + a pinned search_path are required here, not optional
    # hardening: this function queries `companies` internally, and `companies`
    # has RLS enabled with a SELECT policy that itself calls this function.
    # For any caller who is not the table owner (i.e. the real runtime
    # `app_user` role — see design decision #1), a plain (non-SECURITY
    # DEFINER) version of this function recurses infinitely: the function's
    # internal SELECT on `companies` re-triggers the `tenant_select` policy,
    # which calls this function again, forever, until Postgres raises
    # "stack depth limit exceeded". This was verified by direct testing as
    # app_user during Task 5 implementation — every single query against any
    # RLS-protected table failed until this fix was applied.
    #
    # Marking the function SECURITY DEFINER makes its body execute with the
    # privileges of its owner (postgres, who also owns `companies` and was
    # never granted FORCE ROW LEVEL SECURITY), so the internal traversal
    # bypasses RLS entirely and terminates normally. This does NOT weaken
    # tenant isolation: the outer RLS policies still gate what `app_user`
    # can ultimately see/write via `... IN (SELECT id FROM
    # get_all_descendant_ids(...))` — only this function's own internal scan
    # is exempted, and EXECUTE on it remains restricted to app_user below.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION get_all_descendant_ids(root_id UUID)
        RETURNS TABLE (id UUID)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            WITH RECURSIVE company_tree AS (
                SELECT c.id FROM companies c WHERE c.id = root_id
                UNION ALL
                SELECT c.id FROM companies c INNER JOIN company_tree ct ON c.parent_id = ct.id
            )
            SELECT id FROM company_tree;
        $$;
        """
    )

    # --- Restricted application role (design decision #1) -------------------
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
                CREATE ROLE app_user WITH LOGIN PASSWORD 'app_password';
            END IF;
        END
        $$;
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO app_user")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user"
    )
    # Postgres grants EXECUTE on new functions to PUBLIC by default. Since
    # get_all_descendant_ids is SECURITY DEFINER (see the comment above its
    # definition), ANY SQL running as app_user — not just the RLS policy
    # engine's internal use of it — can call it directly with an arbitrary
    # root_id and get back that root's descendant company ids, regardless of
    # the caller's own tenant context. This is an accepted, narrow residual:
    # it only leaks UUID parent/child relationships (no other columns), and
    # app_user is the backend's single trusted connection role, not something
    # end users get raw access to. Revoking PUBLIC and granting only to
    # app_user keeps the surface as narrow as it can be while the function
    # still does its job — it's not a full fix, since app_user itself must
    # retain EXECUTE for the policies above to work.
    op.execute("REVOKE EXECUTE ON FUNCTION get_all_descendant_ids(UUID) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION get_all_descendant_ids(UUID) TO app_user")

    # --- Row-Level Security ---------------------------------------------
    #
    # Every current_setting('app.x', true) cast below is wrapped in
    # NULLIF(..., '') before ::uuid — this is not defensive styling, it fixes
    # a real bug found empirically via connection pooling. A custom
    # ("placeholder") GUC like app.current_tenant, once set even once via
    # SET LOCAL / set_config(..., is_local=true) on a given physical
    # connection, does NOT revert to NULL when that transaction ends —
    # current_setting(name, true) instead returns '' (empty string) for the
    # rest of that connection's life, even in later, unrelated transactions
    # that never set it themselves. Casting '' directly to ::uuid raises
    # invalid input syntax for type uuid: "", which is NOT the same as the
    # policy evaluating to false — it's an unhandled error that surfaces as a
    # 500. Because connection pools reuse physical connections across
    # unrelated logical requests, this bites intentionally: any request that
    # never sets app.current_tenant (e.g. login(), which only sets
    # app.current_user_id) can be served a connection previously "poisoned"
    # by an earlier request that did set it (e.g. register()) and commit.
    # NULLIF(x, '') turns the poisoned '' back into a real NULL before the
    # cast, so the policy correctly evaluates to false (access denied)
    # instead of raising. Verified against live Postgres: the un-guarded cast
    # reproduces the error deterministically once a connection has ever seen
    # a SET on that GUC; the guarded version returns NULL and
    # get_all_descendant_ids(NULL) correctly returns zero rows.
    for table in ("companies", "company_users", "invitations", "audit_log"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    # companies: split SELECT/UPDATE from INSERT so a brand-new top-level
    # company (parent_id IS NULL) can be created before any tenant context
    # exists (design decision #2).
    op.execute(
        """
        CREATE POLICY tenant_select ON companies FOR SELECT
        USING (id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )
    # WITH CHECK here is not optional. Without it, Postgres would fall back to
    # reusing USING for the check — but USING only constrains which existing row
    # a caller may target, never the NEW values being written to it. That leaves
    # parent_id completely unvalidated on UPDATE: a session scoped to tenant A
    # could UPDATE one of A's own companies to set parent_id to an unrelated
    # tenant B's id, re-parenting it out of A's tree and into B's — a full
    # tenant-boundary bypass via UPDATE, even though INSERT and SELECT on this
    # same table are correctly locked down. Empirically confirmed exploitable
    # (a bare `UPDATE companies SET parent_id = '<other-tenant>' WHERE id =
    # '<own-company>'` succeeds) before this WITH CHECK was added. The
    # condition mirrors tenant_insert's: a same-tenant update (parent_id
    # unchanged, or still within the caller's own tree) is unaffected, since
    # the row's pre-update parent_id must already satisfy this same predicate
    # for the row to have been visible/targetable via USING in the first place.
    op.execute(
        """
        CREATE POLICY tenant_update ON companies FOR UPDATE
        USING (id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (
            parent_id IS NULL
            OR parent_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid))
        )
        """
    )
    op.execute(
        """
        CREATE POLICY tenant_insert ON companies FOR INSERT
        WITH CHECK (
            parent_id IS NULL
            OR parent_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid))
        )
        """
    )

    # company_users: the ordinary tenant policy, PLUS a self-membership
    # policy so a user can always discover their own memberships even
    # before app.current_tenant is set (design decision #3). Postgres ORs
    # permissive policies of the same command together.
    op.execute(
        """
        CREATE POLICY tenant_isolation ON company_users FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )
    op.execute(
        """
        CREATE POLICY self_membership ON company_users FOR SELECT
        USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        """
    )

    for table in ("invitations", "audit_log"):
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table} FOR ALL
            USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            """
        )


def downgrade() -> None:
    for table in ("invitations", "audit_log"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute("DROP POLICY IF EXISTS self_membership ON company_users")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON company_users")
    op.execute("DROP POLICY IF EXISTS tenant_insert ON companies")
    op.execute("DROP POLICY IF EXISTS tenant_update ON companies")
    op.execute("DROP POLICY IF EXISTS tenant_select ON companies")
    op.execute("DROP FUNCTION IF EXISTS get_all_descendant_ids(UUID)")
    op.drop_table("audit_log")
    op.drop_table("invitations")
    op.drop_table("company_users")
    op.drop_table("users")
    op.drop_table("companies")
