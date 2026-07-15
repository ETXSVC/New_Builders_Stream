"""Task 4.1 (design spec Section 1): per-record sync status against one
connected provider. Mutable current-state, not an append-only log — see
the design spec's own Section 1 for why (matches Dramatiq's own retry
re-running the SAME logical job, and answers "is this record synced right
now," not "show me every attempt ever made")."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class IntegrationSyncRecord(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "integration_sync_records"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "entity_type", "entity_id",
            name="uq_integration_sync_records_connection_entity",
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integration_connections.id"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
