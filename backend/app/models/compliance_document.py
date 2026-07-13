import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin

VALID_DOC_TYPES = ("insurance_certificate", "license")

_DOC_TYPE_CHECK_SQL = (
    "doc_type IN (" + ",".join(f"'{doc_type}'" for doc_type in VALID_DOC_TYPES) + ")"
)


class ComplianceDocument(Base, UUIDPKMixin, TimestampMixin):
    """No UpdatedAtMixin: no update route exists for this table at all —
    same "immutable from creation" precedent as Esignature
    (app/models/esignature.py)."""

    __tablename__ = "compliance_documents"

    # ON DELETE CASCADE per the schema doc's `ON DELETE CASCADE` on this FK —
    # same pattern as Phase.project_id / ChangeOrder.project_id.
    subcontractor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subcontractors.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
    # clause) — same convention as Project.company_id / Estimate.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    doc_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # Text, not String(N) — a STORAGE_ROOT-relative path, same convention as
    # Document.storage_path / Esignature.signature_artifact_path.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    # DATE, not TIMESTAMPTZ — a plain calendar date with no time-of-day
    # component, per the schema doc's own `DATE` type for this column.
    expires_on: Mapped[date] = mapped_column(Date, nullable=False)

    # CHECK constraint mirrors the migration's DB-level constraint, same
    # belt-and-suspenders pattern as Estimate.ck_estimates_status /
    # ChangeOrder.ck_change_orders_status.
    __table_args__ = (
        CheckConstraint(_DOC_TYPE_CHECK_SQL, name="ck_compliance_documents_doc_type"),
    )
