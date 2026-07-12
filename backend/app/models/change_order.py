import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin

VALID_STATUSES = ("pending", "approved", "rejected")

_STATUS_CHECK_SQL = "status IN (" + ",".join(f"'{status}'" for status in VALID_STATUSES) + ")"


class ChangeOrder(Base, UUIDPKMixin, TimestampMixin):
    """ChangeOrder: this is the table Phase 1 explicitly deferred (see that
    plan's top-of-doc "Explicitly Out of Scope" note) — now unblocked since
    `esignatures` exists as of Task 2.17. Per docs/04-database-schema.md
    Section 4 and Task 2.20.

    TimestampMixin only (no UpdatedAtMixin): the schema doc's `change_orders`
    DDL has `created_at TIMESTAMPTZ DEFAULT now()` but no `updated_at` column
    at all — matching Phase's own precedent (app/models/phase.py) of not
    adding a timestamp mixin/column the schema doc doesn't list. `status`
    being mutable post-create (via Task 2.21's future state machine) does NOT
    require `updated_at` here the way it did for Estimate: Estimate needed
    UpdatedAtMixin because its own schema/design decision explicitly added an
    `updated_at` column (design decision #5 in 0007's docstring); this
    table's schema DDL simply doesn't have one, so none is added.
    """

    __tablename__ = "change_orders"

    # ON DELETE CASCADE per docs/04-database-schema.md Section 4:
    # `project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE`
    # — copied from Phase.project_id's exact pattern (app/models/phase.py).
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE
    # clause) — same convention as Project.company_id / Estimate.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # TEXT, not String(N) — the schema doc says `description TEXT NOT NULL`,
    # not VARCHAR.
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Per US-3.6, a Change Order can legitimately be a credit (negative) or
    # an add (positive) to project cost — no CHECK constraint restricting
    # sign, unlike columns where only a non-negative value makes sense.
    cost_delta: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # NOT NULL with a Python-side default=0, matching how Phase.sequence
    # (app/models/phase.py) treats an analogous "has a default, effectively
    # always populated" integer column — even though the schema doc's raw
    # DDL sketch shows `INT DEFAULT 0` without an explicit NOT NULL, nothing
    # in the app ever intentionally sets this to NULL.
    schedule_impact_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Nullable: unset until the ChangeOrder is approved, mirroring
    # Estimate.esignature_id's own nullable-until-approved pattern
    # (app/models/estimate.py). No ondelete, matching the schema doc's
    # `esignature_id UUID REFERENCES esignatures(id)`.
    esignature_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("esignatures.id"), nullable=True
    )

    # CHECK constraint mirrors the migration's DB-level constraint, same
    # belt-and-suspenders pattern as Estimate.ck_estimates_status /
    # Project.ck_projects_status. Built from VALID_STATUSES the same way
    # Estimate builds _STATUS_CHECK_SQL (app/models/estimate.py).
    __table_args__ = (CheckConstraint(_STATUS_CHECK_SQL, name="ck_change_orders_status"),)
