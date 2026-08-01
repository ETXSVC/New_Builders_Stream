import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedAtMixin, UUIDPKMixin


class CompanyEmailSettings(Base, UUIDPKMixin, TimestampMixin, UpdatedAtMixin):
    """One company's own SMTP server (migration 0029).

    `password_encrypted` is a Fernet ciphertext under the integrations
    key — somebody else's mail password, decrypted in exactly one place
    (`app/services/tenant_smtp.py`) at the moment of sending, and never
    returned by a route.

    `enabled=False` means "use the platform relay" without discarding the
    settings, which is what a company does while their provider is down.
    """

    __tablename__ = "company_email_settings"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False, unique=True
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=587)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    from_address: Mapped[str] = mapped_column(String(255), nullable=False)
    starttls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Null until a test message actually gets through, so a saved form is
    # never mistaken for a working one.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
