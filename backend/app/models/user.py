import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin, utcnow

VALID_ROLES = ("admin", "project_manager", "field_crew", "accountant", "client")

_ROLE_CHECK_SQL = "role IN (" + ",".join(f"'{role}'" for role in VALID_ROLES) + ")"


class User(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # MFA/TOTP (docs/superpowers/specs/2026-07-16-mfa-totp-design.md).
    # totp_secret_encrypted is Fernet ciphertext via app/services/
    # token_encryption.py — the base32 secret is presentable exactly once,
    # at enrollment. Secret present + mfa_activated_at NULL = enrollment
    # pending (NOT yet enforced at login); both set = active.
    # totp_last_used_step records the last successfully used 30s timestep
    # so an intercepted code cannot be replayed inside its window.
    # Text, not String — matches 0015's own DDL and the Fernet-ciphertext
    # convention integration_connection.py established.
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    mfa_activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    totp_last_used_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class CompanyUser(Base):
    __tablename__ = "company_users"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    # No TimestampMixin here (CompanyUser is a pure association row, not an
    # id-bearing entity), but the migration's DDL (0001) already declares
    # created_at NOT NULL with server_default=sa.func.now(). Without a
    # matching client-side default, SQLAlchemy's ORM sends an explicit NULL
    # for this column on INSERT (it doesn't know about the server default),
    # violating that NOT NULL constraint. default=utcnow mirrors the exact
    # pattern TimestampMixin uses for every other timestamped model.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (CheckConstraint(_ROLE_CHECK_SQL, name="ck_company_users_role"),)


class Invitation(Base, UUIDPKMixin):
    __tablename__ = "invitations"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (CheckConstraint(_ROLE_CHECK_SQL, name="ck_invitations_role"),)
