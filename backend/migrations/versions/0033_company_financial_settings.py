"""A tenant's own deposit percentage and tax rate.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-01

`DEFAULT_DEPOSIT_PERCENTAGE = 0.10` and `DEFAULT_TAX_RATE = 0.00` have been
documented placeholders in `app/services/invoicing.py` since the invoicing
spec, which deferred the real numbers as "a pending business decision".
They were harmless while nothing depended on them. They are not any more:
the deposit percentage decides what a customer is actually billed when an
estimate is approved, and the tax rate feeds a figure the profitability
report puts on screen.

The answer was never one number. A deposit percentage is a commercial
policy that differs per builder, and a sales-tax rate differs by
jurisdiction — two branches of the same company in different states have
genuinely different rates. So this is per-tenant configuration rather than
a better constant.

**Stored as fractions, not percentages.** 0.08875, not 8.875. Both existing
consumers multiply directly, and the alternative means dividing by 100 at
every call site with one of them eventually forgotten. NUMERIC(6,5) because
real US sales-tax rates carry three decimal places as a percentage
(Chicago's 8.875% is 0.08875), which two decimal places would silently
round away. The CHECKs bound both to [0, 1] — a "10% deposit" entered as
`10` would otherwise bill ten times the contract value, and a NUMERIC
column would take it without complaint.

**Nullable columns, not a nullable row.** A tenant may want to set one and
leave the other alone, so each column resolves independently: the company's
own value, else its root's, else the code default. Root fallback means a
head office can set a policy once and branches inherit it, while a branch
in another state can still override — which is the whole reason this is
per-company rather than root-scoped like `subscriptions`.

A tenant table like any other: RLS scoped to the tenant tree, and a
`company_id` index because the policy puts `company_id IN (...)` on every
query against it.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_financial_settings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            UUID(as_uuid=True),
            sa.ForeignKey("companies.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("deposit_percentage", sa.Numeric(6, 5), nullable=True),
        sa.Column("tax_rate", sa.Numeric(6, 5), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # A fraction, always. Without these a "10% deposit" typed as 10
        # bills ten times the contract value and NUMERIC(6,5) accepts it.
        sa.CheckConstraint(
            "deposit_percentage IS NULL OR (deposit_percentage >= 0 AND deposit_percentage <= 1)",
            name="ck_company_financial_settings_deposit_fraction",
        ),
        sa.CheckConstraint(
            "tax_rate IS NULL OR (tax_rate >= 0 AND tax_rate <= 1)",
            name="ck_company_financial_settings_tax_fraction",
        ),
    )

    op.execute("ALTER TABLE company_financial_settings ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON company_financial_settings FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )
    op.create_index(
        "ix_company_financial_settings_company_id",
        "company_financial_settings",
        ["company_id"],
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON company_financial_settings")
    op.drop_index(
        "ix_company_financial_settings_company_id", table_name="company_financial_settings"
    )
    op.drop_table("company_financial_settings")
