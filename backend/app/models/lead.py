import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedAtMixin, UUIDPKMixin

VALID_STATUSES = ("new", "contacted", "estimating", "qualified", "won", "lost")

_STATUS_CHECK_SQL = "status IN (" + ",".join(f"'{status}'" for status in VALID_STATUSES) + ")"


class Lead(Base, UUIDPKMixin, TimestampMixin, UpdatedAtMixin):
    __tablename__ = "leads"

    # No ondelete here, matching docs/04-database-schema.md Section 3's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # the same "no ondelete" convention app/models/audit.py's AuditLog.company_id
    # uses for a company_id FK the schema doc doesn't explicitly cascade.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    contact_name: Mapped[str] = mapped_column(String(255), nullable=False)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    estimated_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    project_type: Mapped[str] = mapped_column(String(100), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # updated_at comes from UpdatedAtMixin (app/models/base.py) — Lead is the
    # first model in this codebase with a mutable post-create lifecycle
    # (status transitions and field patches, Task 1.5); every Phase 0 model
    # was create-once/immutable, hence no prior need for this mixin.

    # CHECK constraint mirrors the migration's DB-level constraint (Task 1.2),
    # following the same belt-and-suspenders pattern as user.py's
    # ck_company_users_role / ck_invitations_role — the migration is the
    # authoritative enforcement point (raw op.create_table, not
    # metadata.create_all), this is ORM-level self-documentation.
    __table_args__ = (CheckConstraint(_STATUS_CHECK_SQL, name="ck_leads_status"),)
