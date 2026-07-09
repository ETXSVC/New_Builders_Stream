import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class DailyLog(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "daily_logs"

    # ON DELETE CASCADE per docs/04-database-schema.md Section 4:
    # `project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE`.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Project.company_id / Phase.company_id / Task.company_id /
    # Document.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `author_id UUID NOT NULL REFERENCES users(id)` (no ON DELETE clause).
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    log_date: Mapped[date] = mapped_column(Date, nullable=False)
    weather: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # No updated_at column: immutable once submitted (application-layer
    # enforced per the schema doc's comment, DB-level REVOKE UPDATE, DELETE
    # hardening lands in Task 1.10) — same pattern as CommunicationLog.
