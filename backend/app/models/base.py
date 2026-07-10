import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UUIDPKMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UpdatedAtMixin:
    """Opt in alongside TimestampMixin for models with a mutable post-create
    lifecycle (e.g. status transitions) — not every model needs this; models
    that are immutable-after-create (CommunicationLog, AuditLog, ...) should
    not use it. onupdate=utcnow bumps this on every UPDATE issued via the ORM
    (attribute-set-then-flush, Core-style update(), and 2.0 bulk-update-by-PK
    all trigger it); it will NOT fire for raw SQL that bypasses the ORM."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
