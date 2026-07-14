"""Invoicing AR/AP schema: invoices, invoice_payments, bills, bill_payments,
expenses, and their RLS policies.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-14

Per docs/superpowers/specs/2026-07-14-invoicing-ar-ap-design.md Section 1.
Five tables, created in FK-dependency order:

  invoices          -> projects, companies, estimates
  invoice_payments  -> invoices, companies, users
  bills             -> companies, projects, subcontractors
  bill_payments     -> bills, companies, users
  expenses          -> projects, companies

All five are plain, flat, company-scoped resources (no hierarchy/bidirectional
concern of their own) — each gets the standard single, non-inherited
`tenant_isolation` policy, the same shape 0008/0009 give change_orders/
subcontractors: a single FOR ALL policy with matching USING/WITH CHECK, using
the guarded NULLIF(current_setting('app.current_tenant', true), '')::uuid
cast routed through get_all_descendant_ids() so a parent company's session
also sees its descendant branches' rows. None of these are root-only like
`subscriptions` (migration 0010) — no upward-visibility policy needed here.

No REVOKE on any of the five: every table has a real, planned mutation path
(payments append, void transitions status, and nothing here is
"immutable by omission" the way esignatures/compliance_documents are) —
defaults to the ordinary app_user grants from 0001's ALTER DEFAULT
PRIVILEGES, same as subcontractor_assignments (0009).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- invoices --------------------------------------------------------
    op.create_table(
        "invoices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column(
            "estimate_id", UUID(as_uuid=True), sa.ForeignKey("estimates.id"), nullable=True
        ),
        sa.Column("invoice_number", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('draft','sent','paid','overdue','void')", name="ck_invoices_status"
        ),
        # NOT globally unique — unique PER COMPANY. Numbering
        # (app/services/invoicing.py's next_invoice_number, Task 3.33) is a
        # per-company sequential counter, so two different companies both
        # generating "INV-2026-0001" as their own first invoice is expected,
        # not a collision.
        sa.UniqueConstraint("company_id", "invoice_number", name="uq_invoices_company_invoice_number"),
    )
    op.create_index("idx_invoices_company_status", "invoices", ["company_id", "status"])

    op.execute("ALTER TABLE invoices ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON invoices FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- invoice_payments --------------------------------------------------
    op.create_table(
        "invoice_payments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "invoice_id",
            UUID(as_uuid=True),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("paid_date", sa.Date, nullable=False),
        sa.Column(
            "recorded_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute("ALTER TABLE invoice_payments ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON invoice_payments FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- bills -------------------------------------------------------------
    op.create_table(
        "bills",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column(
            "project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True
        ),
        sa.Column(
            "subcontractor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subcontractors.id"),
            nullable=True,
        ),
        sa.Column("vendor_name", sa.String(255), nullable=True),
        sa.Column("bill_number", sa.String(50), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="unpaid"),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('unpaid','paid','overdue','void')", name="ck_bills_status"
        ),
        sa.CheckConstraint(
            "subcontractor_id IS NOT NULL OR vendor_name IS NOT NULL", name="ck_bills_vendor"
        ),
    )
    op.create_index("idx_bills_company_status", "bills", ["company_id", "status"])

    op.execute("ALTER TABLE bills ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON bills FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- bill_payments -------------------------------------------------------
    op.create_table(
        "bill_payments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bill_id",
            UUID(as_uuid=True),
            sa.ForeignKey("bills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("paid_date", sa.Date, nullable=False),
        sa.Column(
            "recorded_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute("ALTER TABLE bill_payments ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON bill_payments FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )

    # --- expenses ------------------------------------------------------------
    op.create_table(
        "expenses",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("incurred_on", sa.Date, nullable=False),
    )

    op.execute("ALTER TABLE expenses ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON expenses FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON expenses")
    op.drop_table("expenses")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON bill_payments")
    op.drop_table("bill_payments")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON bills")
    op.drop_table("bills")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON invoice_payments")
    op.drop_table("invoice_payments")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON invoices")
    op.drop_table("invoices")
