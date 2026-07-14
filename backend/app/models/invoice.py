import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin

VALID_INVOICE_STATUSES = ("draft", "sent", "paid", "overdue", "void")

_STATUS_CHECK_SQL = (
    "status IN (" + ",".join(f"'{status}'" for status in VALID_INVOICE_STATUSES) + ")"
)


class Invoice(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "invoices"

    # No ondelete, matching docs/04-database-schema.md Section 7's
    # `project_id UUID NOT NULL REFERENCES projects(id)` (no ON DELETE
    # clause) — same convention as Estimate.project_id (when not NULL).
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # Nullable: NULL for invoices created directly via POST /projects/{id}/invoices,
    # not auto-generated from an approved Estimate.
    estimate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("estimates.id"), nullable=True
    )
    invoice_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    due_date: Mapped[date | None] = mapped_column(nullable=True)

    __table_args__ = (CheckConstraint(_STATUS_CHECK_SQL, name="ck_invoices_status"),)
