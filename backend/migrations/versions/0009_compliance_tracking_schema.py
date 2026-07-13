"""Compliance Tracking schema: subcontractors, compliance_documents,
subcontractor_assignments, compliance_notifications, and their RLS policies.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-12

Per docs/superpowers/specs/2026-07-13-compliance-tracking-design.md Section 1
(Data Model). This is genuinely the next migration in sequence after
`change_orders` (0008) — no re-sequencing note needed the way 0006's
esignatures-before-estimates correction required one, since none of these
four tables' FKs point at anything that lands in a later migration.

Four tables, created in FK-dependency order:

  subcontractors               -> companies
  compliance_documents         -> subcontractors, companies
  subcontractor_assignments    -> projects, subcontractors, companies, users
  compliance_notifications     -> companies, compliance_documents

Column/type/nullability/constraint-name choices mirror the already-committed
ORM models exactly (Task 3.1: app/models/subcontractor.py,
app/models/compliance_document.py, app/models/subcontractor_assignment.py,
app/models/compliance_notification.py) — same "migration and model both
declare the same DB-level constraint, belt-and-suspenders" pattern every
prior migration uses (see e.g. 0007/0008's own module docstrings).

All four tables are plain, flat, company-scoped resources — no hierarchy or
join-based-scoping concern of their own (unlike cost_catalog_items'
bidirectional policy, 0005) — so each gets its own ordinary, single,
non-inherited `tenant_isolation` policy, the exact shape 0008 gave
`change_orders`: a single FOR ALL policy with matching USING/WITH CHECK,
each using the guarded NULLIF(current_setting('app.current_tenant',
true), '')::uuid cast (see 0001's long comment for why the guard is
required) routed through get_all_descendant_ids() so a parent company's
session also sees its descendant branches' rows.

REVOKE treatment (design decision #4/#6 precedent, same rationale as
0006/0008):

  - subcontractors: no REVOKE. `name`/`trade`/`contact_email` may need
    correction after entry (e.g. a typo'd contact email) — ordinarily
    mutable, same as any plain CRM-shaped resource.
  - compliance_documents: REVOKE UPDATE, DELETE ON compliance_documents FROM
    app_user. No update or delete route is ever planned for this table —
    the design spec calls this out explicitly as "immutability by
    omission," and the ComplianceDocument ORM model itself (Task 3.1)
    already omits UpdatedAtMixin for the same reason, matching
    esignatures' own precedent (0006) exactly: an uploaded compliance
    document (insurance certificate / license) is immutable from the
    instant it's written, no "before approval" state to make the
    immutability conditional the way estimates/change_orders' status-gated
    mutability is (design decision #4 — REVOKE can't be conditioned on a
    column's runtime value, so genuinely-unconditional immutability is the
    only case where a blanket grant-level REVOKE applies).
  - subcontractor_assignments: no REVOKE. No update route is currently
    planned, but the design spec doesn't declare this table immutable
    either way, so it defaults to the ordinary app_user grants rather than
    being revoked pre-emptively.
  - compliance_notifications: no REVOKE. `read_at` is explicitly written by
    a future dismiss-notification route — an unconditionally-immutable
    REVOKE would break that route entirely.

app_user already has SELECT, INSERT, UPDATE, DELETE on every new table via
0001's `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT,
UPDATE, DELETE ON TABLES TO app_user`, issued by the same `postgres` owner
role that runs every migration (design decision #1; MIGRATIONS_DATABASE_URL
connects as postgres), so it applies automatically here too without any
explicit GRANT in this file — same as every prior migration.

Verified empirically against the live Postgres container (see task notes)
that the REVOKE on compliance_documents actually blocks UPDATE/DELETE as
app_user, same empirical check 0006's own task notes describe for
esignatures.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- subcontractors ------------------------------------------------
    op.create_table(
        "subcontractors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # No ondelete here, matching the schema doc's
        # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
        # clause) — same convention as Project.company_id / Estimate.company_id.
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("trade", sa.String(100), nullable=True),
        sa.Column("contact_email", sa.String(255), nullable=True),
        # TimestampMixin only (no updated_at) — matches the Subcontractor
        # ORM model's own mixin choice (module docstring above).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute("ALTER TABLE subcontractors ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON subcontractors FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )
    # No REVOKE: name/trade/contact_email may legitimately need correction
    # after entry — ordinarily mutable (module docstring above).

    # --- compliance_documents -------------------------------------------
    op.create_table(
        "compliance_documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # ON DELETE CASCADE per the schema doc's `ON DELETE CASCADE` on this
        # FK — same pattern as Phase.project_id / ChangeOrder.project_id.
        sa.Column(
            "subcontractor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subcontractors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # No ondelete here, matching the schema doc's
        # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
        # clause) — same convention as Project.company_id / Estimate.company_id.
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("doc_type", sa.String(30), nullable=False),
        # Text, not String(N) — a STORAGE_ROOT-relative path, same
        # convention as Document.storage_path / Esignature.signature_artifact_path.
        sa.Column("storage_path", sa.Text, nullable=False),
        # DATE, not TIMESTAMPTZ — a plain calendar date with no time-of-day
        # component, per the schema doc's own DATE type for this column.
        sa.Column("expires_on", sa.Date, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "doc_type IN ('insurance_certificate','license')",
            name="ck_compliance_documents_doc_type",
        ),
    )

    # Per docs/04-database-schema.md Section 6:
    # `CREATE INDEX idx_compliance_expiry ON compliance_documents(company_id, expires_on);`
    # — supports both the compliance dashboard (Task 3.6) and the daily
    # expiry-scan actor (Task 3.8), both of which filter/scan by exactly this
    # (company_id, expires_on) pair.
    op.create_index(
        "idx_compliance_expiry", "compliance_documents", ["company_id", "expires_on"]
    )

    op.execute("ALTER TABLE compliance_documents ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON compliance_documents FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )
    # --- Immutability hardening (design decision #6, same as esignatures,
    # 0006) --- No update or delete route is ever planned for this table —
    # "immutability by omission" per the design spec. app_user already has
    # SELECT, INSERT, UPDATE, DELETE via 0001's ALTER DEFAULT PRIVILEGES;
    # revoke the two mutation privileges this table must never allow.
    op.execute("REVOKE UPDATE, DELETE ON compliance_documents FROM app_user")

    # --- subcontractor_assignments ---------------------------------------
    op.create_table(
        "subcontractor_assignments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # ON DELETE CASCADE per the schema doc's `ON DELETE CASCADE` on this
        # FK — same pattern as Phase.project_id / ChangeOrder.project_id.
        sa.Column(
            "project_id",
            UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # No ondelete here, matching the schema doc's
        # `subcontractor_id UUID NOT NULL REFERENCES subcontractors(id)` (no
        # ON DELETE clause).
        sa.Column(
            "subcontractor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("subcontractors.id"),
            nullable=False,
        ),
        # No ondelete here, matching the schema doc's
        # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
        # clause) — same convention as Project.company_id / Estimate.company_id.
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        # No ondelete here, matching the schema doc's
        # `assigned_by UUID NOT NULL REFERENCES users(id)` (no ON DELETE
        # clause) — same convention as Document.uploaded_by.
        sa.Column(
            "assigned_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        # Nullable: populated only when the assignment overrides an
        # expired-compliance block.
        sa.Column("override_reason", sa.Text, nullable=True),
        # TimestampMixin only (no updated_at) — matches the
        # SubcontractorAssignment ORM model's own mixin choice (module
        # docstring above).
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.execute("ALTER TABLE subcontractor_assignments ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON subcontractor_assignments FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )
    # No REVOKE: no update route is currently planned, but the design spec
    # doesn't declare this table immutable either — defaults to the
    # ordinary app_user grants (module docstring above).

    # --- compliance_notifications ------------------------------------------
    op.create_table(
        "compliance_notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # No ondelete here, matching the schema doc's
        # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
        # clause) — same convention as Project.company_id / Estimate.company_id.
        sa.Column(
            "company_id", UUID(as_uuid=True), sa.ForeignKey("companies.id"), nullable=False
        ),
        # ON DELETE CASCADE per the schema doc's `ON DELETE CASCADE` on this
        # FK — same pattern as Phase.project_id / ChangeOrder.project_id.
        sa.Column(
            "compliance_document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("compliance_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("threshold", sa.String(10), nullable=False),
        # No created_at/updated_at: fired_at (below) is the meaningful
        # timestamp for this row, matching the ComplianceNotification ORM
        # model's own mixin-free choice (same rationale as
        # Esignature.signed_at).
        sa.Column(
            "fired_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Nullable, no server default: None until the notification is
        # dismissed.
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "threshold IN ('30_day','14_day','7_day')",
            name="ck_compliance_notifications_threshold",
        ),
        sa.UniqueConstraint(
            "compliance_document_id",
            "threshold",
            name="uq_compliance_notifications_document_threshold",
        ),
    )

    op.execute("ALTER TABLE compliance_notifications ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON compliance_notifications FOR ALL
        USING (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        WITH CHECK (company_id IN (SELECT id FROM get_all_descendant_ids(NULLIF(current_setting('app.current_tenant', true), '')::uuid)))
        """
    )
    # No REVOKE: read_at is explicitly written by a future
    # dismiss-notification route — must stay ordinarily mutable.


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON compliance_notifications")
    op.drop_table("compliance_notifications")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON subcontractor_assignments")
    op.drop_table("subcontractor_assignments")

    op.execute("GRANT UPDATE, DELETE ON compliance_documents TO app_user")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON compliance_documents")
    op.drop_table("compliance_documents")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON subcontractors")
    op.drop_table("subcontractors")
