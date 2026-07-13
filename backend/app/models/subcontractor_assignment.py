import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class SubcontractorAssignment(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "subcontractor_assignments"

    # ON DELETE CASCADE per the schema doc's `ON DELETE CASCADE` on this FK —
    # same pattern as Phase.project_id / ChangeOrder.project_id.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `subcontractor_id UUID NOT NULL REFERENCES subcontractors(id)` (no ON
    # DELETE clause).
    subcontractor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("subcontractors.id"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
    # clause) — same convention as Project.company_id / Estimate.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `assigned_by UUID NOT NULL REFERENCES users(id)` (no ON DELETE clause) —
    # same convention as Document.uploaded_by.
    assigned_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # Nullable: populated only when the assignment overrides an
    # expired-compliance block.
    override_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # TimestampMixin only (no UpdatedAtMixin): the schema doc's own DDL for
    # this table gives it no `updated_at` column — same precedent as Phase
    # (app/models/phase.py) of not adding a timestamp mixin/column the
    # schema doc doesn't list.
