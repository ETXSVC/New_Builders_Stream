import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedAtMixin, UUIDPKMixin

VALID_STATUSES = ("draft", "pre_construction", "active", "suspended", "completed", "archived")

_STATUS_CHECK_SQL = "status IN (" + ",".join(f"'{status}'" for status in VALID_STATUSES) + ")"


class Project(Base, UUIDPKMixin, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "projects"

    # No ondelete here, matching docs/04-database-schema.md Section 4's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Lead.company_id / CommunicationLog.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # Nullable: a Project can be manually initialized without a Lead
    # (POST /projects's documented inputs), not only via LEAD_WON auto-draft.
    # No ondelete either, matching the schema doc's
    # `lead_id UUID REFERENCES leads(id)` (no ON DELETE clause).
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    site_address: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    projected_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # updated_at comes from UpdatedAtMixin (app/models/base.py) — Project has a
    # mutable post-create lifecycle (status transitions via PATCH
    # /projects/{id}/status, general field edits via PATCH /projects/{id}),
    # same rationale as Lead's use of this mixin.

    # CHECK constraint mirrors the migration's DB-level constraint (Task 1.10),
    # same belt-and-suspenders pattern as Lead.ck_leads_status.
    __table_args__ = (CheckConstraint(_STATUS_CHECK_SQL, name="ck_projects_status"),)
