import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin, utcnow

VALID_THRESHOLDS = ("30_day", "14_day", "7_day")

_THRESHOLD_CHECK_SQL = (
    "threshold IN (" + ",".join(f"'{threshold}'" for threshold in VALID_THRESHOLDS) + ")"
)


class ComplianceNotification(Base, UUIDPKMixin):
    __tablename__ = "compliance_notifications"

    # No TimestampMixin: `fired_at` (below) is the meaningful timestamp for
    # this row, not a generic created_at — same rationale as
    # Esignature.signed_at (app/models/esignature.py).

    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
    # clause) — same convention as Project.company_id / Estimate.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # ON DELETE CASCADE per the schema doc's `ON DELETE CASCADE` on this FK —
    # same pattern as Phase.project_id / ChangeOrder.project_id.
    compliance_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("compliance_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    threshold: Mapped[str] = mapped_column(String(10), nullable=False)
    # Rolled-own column (not TimestampMixin) so the name matches its actual
    # semantic meaning, mirroring Esignature.signed_at. Reuses the same
    # `utcnow` helper from app/models/base.py that TimestampMixin uses.
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Nullable, no default: None until the notification is dismissed.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_THRESHOLD_CHECK_SQL, name="ck_compliance_notifications_threshold"),
        # The actual duplicate-notification guarantee, not application-layer
        # discipline alone.
        UniqueConstraint(
            "compliance_document_id", "threshold", name="uq_compliance_notifications_document_threshold"
        ),
    )
