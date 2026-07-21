"""BOM (Bill of Materials) schema: vendors, bom_lines, bom_line_receipts.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-21

Per docs/superpowers/specs/2026-07-20-bom-design.md Decision 2. All three
tables get the ordinary (non-bidirectional) tenant_isolation RLS policy
shape used by every plain company-scoped table in this codebase (e.g.
markup_profiles, migration 0005) — no inheritance/child-branch-visibility
concept, unlike cost_catalog_items' bidirectional policy.

No explicit GRANTs needed for the three new tables: migration 0001's
`ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE,
DELETE ON TABLES TO app_user` already covers every table created after it.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "vendors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("contact_email", sa.String(255), nullable=True),
        sa.Column("contact_phone", sa.String(50), nullable=True),
        sa.Column("notes", sa.String(2000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "bom_lines",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "cost_catalog_item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("cost_catalog_items.id"),
            nullable=True,
        ),
        sa.Column(
            "vendor_id", UUID(as_uuid=True), sa.ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("unit", sa.String(50), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False),
        sa.Column("ordered", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "bom_line_receipts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bom_line_id",
            UUID(as_uuid=True),
            sa.ForeignKey("bom_lines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 2), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("recorded_by_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
    )

    for table in ("vendors", "bom_lines", "bom_line_receipts"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table} FOR ALL
            USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
            """
        )


def downgrade() -> None:
    for table in ("bom_line_receipts", "bom_lines", "vendors"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_table("bom_line_receipts")
    op.drop_table("bom_lines")
    op.drop_table("vendors")
