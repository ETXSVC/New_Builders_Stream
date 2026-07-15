"""Task 4.1 (design spec Section 1): OAuth connection state for a company's
QuickBooks/FreshBooks integration. access_token_encrypted/
refresh_token_encrypted are meant to hold Fernet ciphertext only (Task 4.3's
app/services/token_encryption.py) — no plaintext token, by design — but
that's a property of the write path this task doesn't build yet (Tasks
4.3/4.8/4.9), not something this model layer enforces on its own.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin


class IntegrationConnection(Base, UUIDPKMixin):
    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("company_id", "provider", name="uq_integration_connections_company_provider"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    # Text, not String — matches Task 4.2's own migration DDL and
    # IntegrationSyncRecord.last_error's own use of Text for the same
    # "unbounded free-form string" shape, in this same task.
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
