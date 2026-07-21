"""Company branding: logo, accent color, footer text applied to exported
Estimate PDFs.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-20

Per docs/superpowers/specs/2026-07-20-estimation-esignature-frontend-design.md
Decision 8. One row per company (not root-scoped like subscriptions —
branding is a per-company-branch setting, no inheritance concept), created
lazily on first PUT rather than at company-creation time (spec's own
"missing row = defaults" framing) — same "no row yet" pattern
integration_connections (migration 0013) already establishes for an
optional per-company settings table.

Plain, flat, company-scoped resource, no hierarchy/bidirectional concern of
its own — same standard, non-inherited tenant_isolation policy shape
migration 0013 gives integration_connections.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_branding",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("logo_storage_path", sa.Text, nullable=True),
        sa.Column("accent_color", sa.String(7), nullable=False, server_default="#1e293b"),
        sa.Column("footer_text", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute("ALTER TABLE company_branding ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON company_branding FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON company_branding")
    op.drop_table("company_branding")
