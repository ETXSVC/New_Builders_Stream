import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedAtMixin, UUIDPKMixin

VALID_STATUSES = ("draft", "sent", "approved", "rejected")
VALID_PDF_STATUSES = ("not_requested", "pending", "ready", "failed")

_STATUS_CHECK_SQL = "status IN (" + ",".join(f"'{status}'" for status in VALID_STATUSES) + ")"
_PDF_STATUS_CHECK_SQL = (
    "pdf_status IN (" + ",".join(f"'{status}'" for status in VALID_PDF_STATUSES) + ")"
)


class Estimate(Base, UUIDPKMixin, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "estimates"

    # No ondelete here, matching docs/04-database-schema.md Section 5's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Project.company_id / MarkupProfile.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # Nullable: an Estimate can be created against a bare Lead with no
    # Project yet (US-4.1: "against a Lead or Project"). No ondelete,
    # matching the schema doc's `project_id UUID REFERENCES projects(id)`.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True
    )
    # Nullable for the mirror-image reason — an Estimate against an
    # already-drafted Project has no lead_id. No ondelete, matching the
    # schema doc's `lead_id UUID REFERENCES leads(id)`.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    # No ondelete here, matching the schema doc's
    # `markup_profile_id UUID NOT NULL REFERENCES markup_profiles(id)`.
    markup_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("markup_profiles.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    # Nullable, no default: subtotal/total stay NULL (not 0) until the first
    # POST /estimates/{id}/calculate run actually computes them (Task 2.12).
    # NULL means "never calculated"; a Decimal("0") default would falsely
    # read as "calculated as free," so unlike MarkupProfile's
    # overhead_pct/profit_pct (always meaningfully zero-or-more from
    # creation), no default is given here at all.
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # True once approved; line items (and subtotal/total) become immutable —
    # enforced at the application layer, not the DB-grant layer (design
    # decision #4: immutability conditional on this column's runtime value
    # can't be expressed as a blanket REVOKE the way Phase 1's always-
    # immutable tables — communication_logs, daily_logs, documents — were).
    is_snapshotted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Nullable: unset until the Estimate is sent for signature. References a
    # table (`esignatures`) that does not exist yet as either a model or a
    # migration — it's built in Task 2.17. ForeignKey() targets are resolved
    # lazily by string, only when a migration/DB actually enforces them, so
    # declaring this now is safe; getting the CREATE TABLE ordering right
    # (esignatures before estimates) is Task 2.8's job, not this one's.
    esignature_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("esignatures.id"), nullable=True
    )

    # The three columns below are NOT in docs/04-database-schema.md Section
    # 5's `estimates` table — a deliberate, documented extension (design
    # decision #5), same category as Phase 1's `PATCH /projects/{id}`
    # decision: a necessary extension beyond the documented schema, not a
    # deviation from intent. No dedicated export-history table: only the
    # most recent export matters practically (re-exporting after a
    # line-item edit produces a new PDF from current state; previous
    # exports aren't a retained artifact the way Document versions are).
    pdf_status: Mapped[str] = mapped_column(String(20), nullable=False, default="not_requested")
    # Nullable, STORAGE_ROOT-relative path — same convention
    # app/services/document_storage.py established in Phase 1, reused
    # directly (Task 2.13). NULL until a PDF has actually been generated.
    pdf_storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # created_at comes from TimestampMixin, updated_at from UpdatedAtMixin
    # (app/models/base.py) — an Estimate has a mutable post-create lifecycle
    # (line-item edits, recalculation, status transitions, PDF export
    # tracking), same rationale as Lead/Project/CostCatalogItem's use of
    # UpdatedAtMixin.

    # Two separate CHECK constraints, mirroring the two independent DB-level
    # CHECK constraints Task 2.8's migration will add — same
    # belt-and-suspenders pattern as Project.ck_projects_status /
    # Lead.ck_leads_status. `status` and `pdf_status` are independent
    # lifecycles (an Estimate can be status='sent' while pdf_status='ready'
    # from an earlier export), hence two constraints rather than one
    # combined check.
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK_SQL, name="ck_estimates_status"),
        CheckConstraint(_PDF_STATUS_CHECK_SQL, name="ck_estimates_pdf_status"),
    )
