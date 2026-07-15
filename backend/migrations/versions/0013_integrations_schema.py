"""Integrations schema: integration_connections, integration_sync_records,
and their RLS policies.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-15

Per docs/superpowers/specs/2026-07-15-integrations-quickbooks-freshbooks-design.md
Section 1. Two tables:

  integration_connections   -> companies
  integration_sync_records  -> integration_connections, companies

Both are plain, flat, company-scoped resources (no hierarchy/bidirectional
concern of their own) — each gets the standard single, non-inherited
tenant_isolation policy, the same shape 0012 gives invoices/bills/expenses.

No REVOKE on either table: both have real, planned mutation paths
(callback upserts connections; the sync actor updates sync records in
place) — defaults to the ordinary app_user grants from 0001's ALTER
DEFAULT PRIVILEGES.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- integration_connections ------------------------------------------
    op.create_table(
        "integration_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("access_token_encrypted", sa.Text, nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text, nullable=False),
        sa.Column(
            "connected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("provider IN ('quickbooks','freshbooks')", name="ck_integration_connections_provider"),
        sa.UniqueConstraint(
            "company_id", "provider", name="uq_integration_connections_company_provider"
        ),
    )

    op.execute("ALTER TABLE integration_connections ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON integration_connections FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- integration_sync_records -------------------------------------------
    op.create_table(
        "integration_sync_records",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column(
            "connection_id", UUID(as_uuid=True), sa.ForeignKey("integration_connections.id"), nullable=False
        ),
        sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "entity_type IN ('invoice','expense','bill')", name="ck_integration_sync_records_entity_type"
        ),
        sa.CheckConstraint(
            "status IN ('pending','success','failed')", name="ck_integration_sync_records_status"
        ),
        sa.UniqueConstraint(
            "connection_id", "entity_type", "entity_id",
            name="uq_integration_sync_records_connection_entity",
        ),
    )
    op.create_index(
        "idx_integration_sync_records_connection_status",
        "integration_sync_records",
        ["connection_id", "status"],
    )

    op.execute("ALTER TABLE integration_sync_records ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON integration_sync_records FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON integration_sync_records")
    op.drop_table("integration_sync_records")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON integration_connections")
    op.drop_table("integration_connections")
