import uuid

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Document(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "documents"

    # ON DELETE CASCADE per docs/04-database-schema.md Section 4:
    # `project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE`.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Project.company_id / Phase.company_id / Task.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # No ondelete here, matching the schema doc's
    # `uploaded_by UUID NOT NULL REFERENCES users(id)` (no ON DELETE clause).
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    # No updated_at column: new versions are new rows (design decision #6),
    # never an UPDATE of an existing row — same immutable-row rationale as
    # CommunicationLog, though documents is versioned-by-insert rather than
    # fully immutable (Task 1.10's REVOKE UPDATE, DELETE still applies).
