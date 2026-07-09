import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin

VALID_CHANNELS = ("call", "email", "note", "sms")

_CHANNEL_CHECK_SQL = "channel IN (" + ",".join(f"'{channel}'" for channel in VALID_CHANNELS) + ")"


class CommunicationLog(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "communication_logs"

    # ON DELETE CASCADE per docs/04-database-schema.md Section 3:
    # `lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE`.
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Lead.company_id / AuditLog.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # No updated_at column: immutable by design (design decision #6). TimestampMixin's
    # created_at is the only timestamp. DB-level enforcement (REVOKE UPDATE, DELETE ON
    # communication_logs FROM app_user) lands in the Task 1.2 migration; this model
    # simply has no column and no route will ever exist to mutate one.
    __table_args__ = (
        CheckConstraint(_CHANNEL_CHECK_SQL, name="ck_communication_logs_channel"),
    )
