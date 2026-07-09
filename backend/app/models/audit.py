import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class AuditLog(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "audit_log"

    # No ondelete here (defaults to RESTRICT), unlike every other company_id FK in
    # this file. docs/07-security-compliance.md requires a minimum 7-year audit log
    # retention and states no code path ever deletes audit entries — CASCADE would
    # let deleting a company silently destroy its own audit trail, which directly
    # contradicts that policy. RESTRICT means a company can't be deleted while it
    # still has audit history, which is the correct failure mode until Phase 0 (which
    # has no company-delete endpoint at all) grows one with an explicit retention story.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    log_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
