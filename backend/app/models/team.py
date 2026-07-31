"""The team directory (migration 0026).

Company-scoped records of the people in a company. See the migration's
docstring for why these hang off the MEMBERSHIP rather than off `users` —
short version: `users` has no RLS, so a home address there would be
readable by every company that person belongs to.
"""
import uuid

from sqlalchemy import ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UpdatedAtMixin, UUIDPKMixin


class Profession(Base, UUIDPKMixin, TimestampMixin):
    """A trade a company recognises — company-managed, not a constant.

    Adding "Drywall Finisher" should be a row, not a migration. Uniqueness
    is enforced case-insensitively per company by a functional index in
    0026, not by this class.
    """

    __tablename__ = "professions"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)


class MemberProfile(Base, UUIDPKMixin, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "member_profiles"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    address_line1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    profession_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("professions.id", ondelete="SET NULL"), nullable=True
    )
    # Relative to STORAGE_ROOT, like company branding logos — never
    # absolute, so the volume can move.
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # `lazy="selectin"` because every read of a profile wants its phones and
    # a lazy load would be a synchronous IO attempt from asyncpg — the
    # `MissingGreenlet` that `companies.is_active` already taught this
    # codebase about (see app/models/company.py).
    phones: Mapped[list["MemberPhone"]] = relationship(
        back_populates="profile",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MemberPhone.created_at",
    )

    __table_args__ = (
        UniqueConstraint("company_id", "user_id", name="uq_member_profiles_company_user"),
        # Mirrors the migration: the profile describes a membership, so it
        # depends on one, and CASCADE takes it away with the person.
        ForeignKeyConstraint(
            ["company_id", "user_id"],
            ["company_users.company_id", "company_users.user_id"],
            name="fk_member_profiles_membership",
            ondelete="CASCADE",
        ),
    )


class MemberPhone(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "member_phones"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    member_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("member_profiles.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Stored as typed. Extensions, country codes and parentheses are all
    # things a builder writes down; a lossy normalisation would throw away
    # the only copy.
    number: Mapped[str] = mapped_column(String(40), nullable=False)

    profile: Mapped[MemberProfile] = relationship(back_populates="phones")
