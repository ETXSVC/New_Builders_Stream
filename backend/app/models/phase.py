import uuid

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin


class Phase(Base, UUIDPKMixin):
    __tablename__ = "phases"

    # ON DELETE CASCADE per docs/04-database-schema.md Section 4:
    # `project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE`.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Project.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # No created_at/updated_at: docs/04-database-schema.md Section 4's `phases`
    # table has no timestamp columns at all — intentionally not adding
    # TimestampMixin/UpdatedAtMixin here to match the schema doc exactly.
