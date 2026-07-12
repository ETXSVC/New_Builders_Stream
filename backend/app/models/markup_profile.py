import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin


class MarkupProfile(Base, UUIDPKMixin):
    __tablename__ = "markup_profiles"

    # No TimestampMixin/UpdatedAtMixin: docs/04-database-schema.md Section 5's
    # `markup_profiles` table has no created_at/updated_at columns at all —
    # match the schema doc exactly, don't add timestamps it doesn't specify.

    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Lead.company_id / Project.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # default=Decimal("0"), not the bare int 0 — SQLAlchemy applies a scalar
    # Python-side default to the in-memory attribute at flush time without
    # coercing it through the column's type, so a bare `default=0` would let
    # a freshly-flushed instance expose `.overhead_pct == 0` as a plain int
    # before the next refresh, contradicting both the Mapped[Decimal]
    # annotation and this codebase's "monetary/percentage values are always
    # Decimal, never float/int" invariant (Phase 2 plan, Inherited Invariant
    # #9). Found during this task's code-quality review.
    overhead_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    profit_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )

    # markup_profiles is a plain, flat, per-company resource — no
    # parent_profile_id / inheritance concept (New Design Decision #1's
    # closing note, docs/superpowers/plans/2026-07-09-phase-2-estimation-esignature.md).
    # Do not add inheritance logic here; that would be scope creep beyond
    # what US-4.6 and the schema doc actually specify.
