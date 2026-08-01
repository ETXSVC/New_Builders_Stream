import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin, utcnow


class PasswordResetToken(Base, UUIDPKMixin):
    """One outstanding "forgot my password" link (migration 0028).

    `token_hash` is the SHA-256 hex of a secret that exists in exactly one
    email and nowhere else — the same rule `RefreshToken` follows, for the
    same reason: this row is a bearer credential to an account, and a
    database dump must not be one.

    "Redeemable" means `used_at IS NULL AND expires_at > now()`. Both halves
    matter and neither is enough alone: an hour-old link that was never
    clicked is dead by time, and a link clicked ten seconds ago is dead by
    use, because a reset link sitting in an inbox would otherwise be a
    standing key to the account.

    User-scoped and deliberately NOT tenant-scoped, like `refresh_tokens`
    and `users`: the request arrives with no session, no `X-Tenant-ID` and
    no membership yet resolved, so there is no tenant to scope it to.
    """

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set the moment the token is spent, in the same transaction that
    # rewrites the password — so a double-submit of the same link cannot
    # set two different passwords.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
