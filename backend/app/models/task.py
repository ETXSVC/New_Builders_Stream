import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin

VALID_STATUSES = ("open", "in_progress", "done")

_STATUS_CHECK_SQL = "status IN (" + ",".join(f"'{status}'" for status in VALID_STATUSES) + ")"


class Task(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "tasks"

    # ON DELETE CASCADE per docs/04-database-schema.md Section 4:
    # `phase_id UUID NOT NULL REFERENCES phases(id) ON DELETE CASCADE`.
    phase_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("phases.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Project.company_id / Phase.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Nullable + no ondelete, matching the schema doc's
    # `assignee_id UUID REFERENCES users(id)` (no ON DELETE clause).
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    # No updated_at: docs/04-database-schema.md Section 4's `tasks` table has
    # only `created_at`, no `updated_at` column — status/assignee changes are
    # ordinary UPDATEs (tasks is not immutable), the schema doc simply doesn't
    # track a last-modified timestamp for this table.

    __table_args__ = (CheckConstraint(_STATUS_CHECK_SQL, name="ck_tasks_status"),)
