import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin, utcnow


class RefreshToken(Base, UUIDPKMixin):
    """One issued refresh token (docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md).

    token_hash is the SHA-256 hex of the opaque secret — the presentable
    secret is never stored anywhere. family_id groups a rotation chain
    (minted at login, inherited by every rotation successor) so that reuse
    of an already-rotated token can revoke the whole chain at once.
    replaced_by_id points at the rotation SUCCESSOR and must be set in the
    same UPDATE that sets revoked_at (the CHECK below enforces the
    direction that matters: a token with a successor can never still be
    redeemable — that would be exactly the double-spend rotation exists to
    prevent). "Valid" means revoked_at IS NULL AND expires_at > now().
    User-scoped, NO RLS (like users itself): a refresh token belongs to a
    person, not a tenant, and token rows/hashes are never serialized into
    any API response. issued_at replaces TimestampMixin's created_at — the
    domain-meaningful name, same reasoning as esignatures' signed_at.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("refresh_tokens.id"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "replaced_by_id IS NULL OR revoked_at IS NOT NULL",
            name="ck_refresh_tokens_replaced_implies_revoked",
        ),
    )
