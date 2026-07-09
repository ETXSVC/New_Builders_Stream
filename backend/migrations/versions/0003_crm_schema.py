"""CRM schema: leads, communication_logs, and their RLS policies.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-08

Per docs/04-database-schema.md Section 3. Both tables are flat,
company-scoped tables with no hierarchy concern of their own (unlike
`companies`), so their `tenant_isolation` policy follows the exact pattern
used for `invitations`/`audit_log` in migration 0001: a single FOR ALL
policy with both USING and WITH CHECK, each using the guarded
NULLIF(current_setting('app.current_tenant', true), '')::uuid cast (see
0001's long comment for why the guard is required — a bare cast raises an
unhandled 500 once a pooled connection has ever seen app.current_tenant set,
because current_setting(name, true) returns '' rather than NULL on a
connection that previously had the GUC set and later rolled back to no
value). WITH CHECK is not optional either (design decision #6 / 0001's
`tenant_update` comment): USING alone only gates which existing row a
caller may target, not what values may be written into it.

communication_logs is immutable by design (docs/04-database-schema.md:
"Immutable by convention: no updated_at, no UPDATE grants at the
application layer") — this migration hardens that at the grant level too
(design decision #6 in the Phase 1 plan), so a future route added by
mistake still can't mutate history: `REVOKE UPDATE, DELETE ON
communication_logs FROM app_user`. Verified empirically against the live
Postgres container (see task notes) that app_user already has SELECT,
INSERT, UPDATE, DELETE on both new tables without any explicit GRANT here
— migrations run as the `postgres` owner role (design decision #1;
MIGRATIONS_DATABASE_URL connects as postgres), and 0001's `ALTER DEFAULT
PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES
TO app_user` was issued by that same role, so it applies automatically to
every table `postgres` creates afterward in this schema, including these
two. No explicit GRANT statement is needed before the REVOKE.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("contact_name", sa.String(255), nullable=False),
        sa.Column("project_name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("estimated_value", sa.Numeric(12, 2), nullable=True),
        sa.Column("project_type", sa.String(100), nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('new','contacted','estimating','qualified','won','lost')",
            name="ck_leads_status",
        ),
    )
    op.create_index("idx_leads_company_status", "leads", ["company_id", "status"])

    op.create_table(
        "communication_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_id", UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("author_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # No updated_at column: immutable by design (design decision #6).
        sa.CheckConstraint(
            "channel IN ('call','email','note','sms')",
            name="ck_communication_logs_channel",
        ),
    )

    # --- Row-Level Security -------------------------------------------------
    #
    # Guarded NULLIF(...)::uuid cast on every current_setting() call below —
    # see 0001's long comment for the exact connection-pooling failure mode
    # this prevents (a bare cast raises "invalid input syntax for type uuid"
    # instead of evaluating to false once a pooled connection has ever seen
    # app.current_tenant set and later returns '' rather than NULL for it).
    for table in ("leads", "communication_logs"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table} FOR ALL
            USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            """
        )

    # --- Immutability hardening (design decision #6) ------------------------
    # app_user already has SELECT, INSERT, UPDATE, DELETE on both new tables
    # via 0001's ALTER DEFAULT PRIVILEGES (verified empirically — see module
    # docstring). Revoke the two mutation privileges communication_logs must
    # never allow, so DB-level enforcement backs up the fact that no
    # update/delete route for communication logs will ever exist.
    op.execute("REVOKE UPDATE, DELETE ON communication_logs FROM app_user")


def downgrade() -> None:
    op.execute("GRANT UPDATE, DELETE ON communication_logs TO app_user")
    for table in ("leads", "communication_logs"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("communication_logs")
    op.drop_index("idx_leads_company_status", table_name="leads")
    op.drop_table("leads")
