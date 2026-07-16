import uuid
from datetime import datetime

from sqlalchemy import CHAR, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin, utcnow


class RefreshToken(Base, UUIDPKMixin):
    """One issued refresh token (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md).

    token_hash is the SHA-256 hex of the opaque secret — the presentable
    secret is never stored anywhere. family_id groups a rotation chain
    (minted at login, inherited by every rotation successor) so that reuse
    of an already-rotated token can revoke the whole chain at once.
    User-scoped, NO RLS (like users itself): a refresh token belongs to a
    person, not a tenant, and the table is never readable through any API.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(CHAR(64), unique=True, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id"), nullable=True
    )
