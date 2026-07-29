"""Platform administration: a trust tier above every tenant.

Revision ID: 0023
Revises: 0022
Create Date: 2026-07-28

Until now the highest identity in this system was a company `admin`. There
was no way for the operator of the platform to see, let alone adjust, what
a customer's tenant is entitled to — tier changes could only come from
Stripe, and per-feature exceptions were not expressible at all.

This migration adds that tier. Three things, plus one column:

1. `platform_admins` — WHO may administer tenants. Deliberately NOT a new
   value in `company_users.role`: that column is scoped to one company by
   construction (and by a CHECK constraint), so a "role above all
   companies" does not fit it. This is a separate, global fact about a
   user, in the same way `users` itself is global.

2. `company_module_overrides` — per-tenant feature grants that override the
   subscription tier's bundle. `MODULE_MIN_TIER` (app/core/tier_gating.py)
   maps each module to the tier that unlocks it; a row here says "this
   tenant gets (or does not get) this module regardless of that mapping",
   which is how a single feature gets comped without moving a customer's
   whole plan.

3. `platform_admin` — the Postgres role the console's cross-tenant reads
   run as. Same reasoning as `scanner` in migration 0020: BYPASSRLS,
   because listing every tenant is the entire job, but owning nothing, so
   it cannot alter a policy or drop a table. It is a SEPARATE role from
   `scanner` and much narrower on writes — `scanner` holds blanket DML for
   the daily sweeps, whereas this role can write exactly two things
   (`company_module_overrides` and `subscriptions`) plus an audit row. A
   request-path role that physically cannot write a customer's project or
   invoice is worth one extra role.

4. `subscriptions.manual_status_override` — see its own comment below.

THE GRANT MODEL IS THE POINT. Migration 0001 ends with

    ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user

so every table created afterwards hands the runtime role full DML unless
that is explicitly taken back. Left alone, `platform_admins` would be
INSERTable by the ordinary request path — i.e. any code path reachable
from an HTTP request could promote its own user to platform admin. Both
new tables therefore REVOKE write from `app_user` immediately after
creation, and `platform_admins` is additionally readable only for the row
naming the current user. There is no route that can grant this privilege;
`backend/scripts/grant_platform_admin.py`, run by an operator as the
owner role, is the only way in. Privilege escalation is removed as a
category rather than defended against.
"""
import os

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

# Same shape as migration 0020's role passwords: an operator who sets
# nothing gets a working dev/test default, and `app/config.py`'s production
# validator is what makes shipping the default loud rather than silent.
_DEFAULT_PLATFORM_PASSWORD = "platform_password"

# Mirrors app/core/tier_gating.py's MODULE_MIN_TIER keys. Duplicated as a
# CHECK constraint rather than left to the application: a typo'd module name
# in an override row would otherwise sit in the table matching nothing,
# silently granting nothing, with no error anywhere.
_MODULES = ("estimation", "compliance", "accounting", "integrations", "child_branches")
_MODULE_CHECK_SQL = "module IN (" + ",".join(f"'{m}'" for m in _MODULES) + ")"


def _quote_literal(value: str) -> str:
    """Postgres string literal; see migration 0020's identical helper."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    # --- who may administer tenants -------------------------------------
    op.create_table(
        "platform_admins",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "granted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # Nullable because the FIRST platform admin has no granting admin
        # above them -- the bootstrap CLI creates it with granted_by NULL.
        sa.Column("granted_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        # Revocation is a soft delete: who held this privilege and when is
        # exactly the sort of thing an incident review asks about, and a
        # DELETE answers it with silence.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute("ALTER TABLE platform_admins ENABLE ROW LEVEL SECURITY")
    # No company_id, so `tests/test_rls_policy_coverage.py`'s tenant sweeps
    # do not apply -- but its fourth check (every table without a company_id
    # either has RLS on or is declared non-tenant) does, and enabling RLS
    # here is the honest answer rather than an allowlist entry.
    #
    # SELECT-only, and only the row naming the caller. That is the entire
    # read the request path legitimately needs: "am *I* a platform admin?"
    # It cannot enumerate the others.
    op.execute(
        """
        CREATE POLICY self_read ON platform_admins FOR SELECT
        USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        """
    )
    # TWO standing ALTER DEFAULT PRIVILEGES grants already handed this table
    # full DML the moment it was created -- migration 0001's to `app_user`
    # and migration 0020's to `scanner`. Both must be taken back, and the
    # second is the easier one to miss: `scanner` is BYPASSRLS and is what
    # the Dramatiq worker connects as, so leaving it would mean any of the
    # daily sweeps could insert a platform-admin row. Verified against
    # information_schema.role_table_grants rather than assumed --
    # `tests/test_platform_admin.py` now asserts it, because the next
    # migration that adds a default-privilege grant will re-open this hole
    # for whatever table comes after it.
    op.execute("REVOKE INSERT, UPDATE, DELETE ON platform_admins FROM app_user")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON platform_admins FROM scanner")

    # --- per-tenant feature overrides -----------------------------------
    op.create_table(
        "company_module_overrides",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # Always a ROOT company, for the same reason `subscriptions` is
        # (migration 0010): tier resolution runs through
        # get_root_company_id(), so an override attached to a child branch
        # would be invisible to the gate that consults it. Enforced at the
        # application layer, matching the precedent subscriptions set.
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("module", sa.String(50), nullable=False),
        # Three-state by design: a row with enabled=false REVOKES a module
        # the tier would otherwise allow, and NO row at all means "defer to
        # the tier". Deleting the row is how you go back to the plan's
        # default, which is a different act from switching it off.
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("set_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(_MODULE_CHECK_SQL, name="ck_company_module_overrides_module"),
        # Also the index `tests/test_company_id_index_coverage.py` requires:
        # company_id is the leading key column of the unique index this
        # constraint creates, so no separate index is needed.
        sa.UniqueConstraint("company_id", "module", name="uq_company_module_overrides_company_module"),
    )

    op.execute("ALTER TABLE company_module_overrides ENABLE ROW LEVEL SECURITY")
    # The SAME deliberately non-standard upward-visibility policy
    # `subscriptions` carries (migration 0010), and for the identical
    # reason: these rows hang off the root company, but the session asking
    # "is this module enabled for me?" is usually scoped to a CHILD branch.
    # Downward visibility (get_all_descendant_ids) is the wrong direction
    # here and would hide a branch's own entitlement from it.
    op.execute(
        """
        CREATE POLICY tenant_isolation ON company_module_overrides FOR ALL
        USING (company_id = get_root_company_id(NULLIF(current_setting('app.current_tenant', true), '')::uuid))
        WITH CHECK (company_id = get_root_company_id(NULLIF(current_setting('app.current_tenant', true), '')::uuid))
        """
    )
    # A tenant may READ its own entitlements (the gate does exactly that on
    # every mutating request) and may never write them. Only the platform
    # role below can, which is what makes this an override the OPERATOR
    # controls rather than a self-service upgrade button. `scanner` is
    # revoked for the same reason it is on platform_admins above -- the
    # daily sweeps have no business editing what a customer is entitled to.
    op.execute("REVOKE INSERT, UPDATE, DELETE ON company_module_overrides FROM app_user")
    op.execute("REVOKE INSERT, UPDATE, DELETE ON company_module_overrides FROM scanner")

    # --- keep a manual status from being undone by Stripe ----------------
    #
    # `POST /webhooks/stripe` (app/routers/webhooks.py) is last-write-wins on
    # `status`: customer.subscription.updated overwrites whatever is there.
    # An operator who sets a tenant read-only by hand would therefore have
    # that reverted by the next routine subscription event, with no error
    # and nothing in the logs -- the change simply stops being true. This
    # flag makes the manual value stick: the webhook still records
    # current_period_end (that is Stripe's fact to own, not ours) but leaves
    # status alone while it is set. Clearing the override hands control back.
    op.add_column(
        "subscriptions",
        sa.Column(
            "manual_status_override",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
    )

    # --- the console's database role ------------------------------------
    platform_password = os.environ.get("PLATFORM_DB_PASSWORD", _DEFAULT_PLATFORM_PASSWORD)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_admin') THEN
                CREATE ROLE platform_admin WITH LOGIN BYPASSRLS PASSWORD {_quote_literal(platform_password)};
            ELSE
                ALTER ROLE platform_admin WITH LOGIN BYPASSRLS PASSWORD {_quote_literal(platform_password)};
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO platform_admin")
    # Read everything: the console lists every tenant, and answering "what
    # is this customer actually using" spans most tables.
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO platform_admin")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO platform_admin")
    # Write almost nothing. These three are the whole mutable surface of the
    # console, so a bug in it cannot corrupt a customer's project, invoice
    # or estimate -- the privilege simply is not held.
    op.execute("GRANT INSERT, UPDATE, DELETE ON company_module_overrides TO platform_admin")
    op.execute("GRANT UPDATE ON subscriptions TO platform_admin")
    op.execute("GRANT INSERT ON audit_log TO platform_admin")
    op.execute("GRANT EXECUTE ON FUNCTION get_root_company_id(UUID) TO platform_admin")
    op.execute("GRANT EXECUTE ON FUNCTION get_all_descendant_ids(UUID) TO platform_admin")


def downgrade() -> None:
    # Same reasoning as migration 0020's downgrade: strip the role's
    # footprint from THIS database, but do not DROP the role -- roles are
    # cluster-level and another database may still grant it. DROP OWNED BY
    # covers every per-table ACL entry and the ALTER DEFAULT PRIVILEGES
    # entry together, which hand-written REVOKEs reliably miss.
    op.execute("DROP OWNED BY platform_admin")

    op.drop_column("subscriptions", "manual_status_override")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON company_module_overrides")
    op.drop_table("company_module_overrides")

    op.execute("DROP POLICY IF EXISTS self_read ON platform_admins")
    op.drop_table("platform_admins")
