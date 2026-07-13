"""Billing schema: subscriptions table, get_root_company_id() function, and
its (deliberately non-standard) RLS policy.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-13

Per docs/superpowers/specs/2026-07-13-billing-design.md Sections 1 (Data
Model) and 3 (Registration & Trial Flow).

`get_root_company_id(company_id)` is the mirror-image of the existing
`get_all_descendant_ids()` (migration 0001): instead of walking DOWN the
parent_id tree to find every descendant, it walks UP to find the one
ancestor with parent_id IS NULL. Built the same way for the same reason:
SECURITY DEFINER (so it can read `companies` rows regardless of the
caller's own RLS-visible scope while resolving the chain), REVOKE EXECUTE
FROM PUBLIC + GRANT TO app_user only (same accepted-residual reasoning
0001's own comment gives for get_all_descendant_ids: this narrowly leaks
parent/child company-id relationships to any authenticated app_user
session, not arbitrary data).

`subscriptions` gets a DELIBERATELY NON-STANDARD RLS policy. Every other
company-scoped table in this codebase grants only DOWNWARD visibility (a
session scoped to a parent can see a descendant's rows, via
get_all_descendant_ids(current_tenant)). subscriptions.company_id is
always a root (enforced at the application layer, not the DB, per the
design spec), so a session scoped to a CHILD branch — exactly who needs to
resolve "my effective subscription" — needs the opposite, UPWARD,
direction: visibility into its own root's single row. No existing policy
shape grants that, so this table gets its own: a session can see the one
row whose company_id equals get_root_company_id(current_tenant) — which is
its own row if the session's tenant IS root (matching
get_all_descendant_ids()'s own base-case convention of including the
starting id).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION get_root_company_id(child_id UUID)
        RETURNS UUID
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            WITH RECURSIVE ancestry AS (
                SELECT c.id, c.parent_id FROM companies c WHERE c.id = child_id
                UNION ALL
                SELECT c.id, c.parent_id FROM companies c
                    INNER JOIN ancestry a ON c.id = a.parent_id
            )
            SELECT id FROM ancestry WHERE parent_id IS NULL;
        $$;
        """
    )
    op.execute("REVOKE EXECUTE ON FUNCTION get_root_company_id(UUID) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION get_root_company_id(UUID) TO app_user")

    op.create_table(
        "subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # No ondelete here, matching every other *.company_id FK convention
        # in this codebase (Project.company_id, Estimate.company_id, ...).
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("stripe_customer_id", sa.String(255), nullable=False),
        sa.Column("stripe_subscription_id", sa.String(255), nullable=False),
        sa.Column("tier", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("included_seats", sa.Integer, nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "tier IN ('starter','pro','enterprise')", name="ck_subscriptions_tier"
        ),
        sa.UniqueConstraint("company_id", name="uq_subscriptions_company_id"),
        sa.UniqueConstraint(
            "stripe_subscription_id", name="uq_subscriptions_stripe_subscription_id"
        ),
    )

    op.execute("ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON subscriptions FOR ALL
        USING (company_id = get_root_company_id(NULLIF(current_setting('app.current_tenant', true), '')::uuid))
        WITH CHECK (company_id = get_root_company_id(NULLIF(current_setting('app.current_tenant', true), '')::uuid))
        """
    )
    # No REVOKE: status/tier/current_period_end are updated in place by the
    # webhook handler (Task 3.21) as Stripe's own state changes — this row
    # is a live mirror, not an immutable record.


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON subscriptions")
    op.drop_table("subscriptions")
    op.execute("REVOKE EXECUTE ON FUNCTION get_root_company_id(UUID) FROM app_user")
    op.execute("DROP FUNCTION IF EXISTS get_root_company_id(UUID)")
