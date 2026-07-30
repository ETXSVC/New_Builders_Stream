import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class Company(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "companies"

    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Vestigial since 0001: defaulted true, exposed in CompanyResponse, read
    # by nothing. `deleted_at` below is what actually gates access; the
    # platform console writes both together so this one stops lying. See
    # migration 0024's docstring for why it was not simply repurposed.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Soft delete, set by the platform console (migration 0024). NULL means
    # live. Enforced in app/core/deps.py at the membership chokepoint, so a
    # token already issued stops working too, not just new logins.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
