import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin

VALID_TIERS = ("starter", "pro", "enterprise")
_TIER_CHECK_SQL = "tier IN (" + ",".join(f"'{t}'" for t in VALID_TIERS) + ")"


class Subscription(Base, UUIDPKMixin):
    __tablename__ = "subscriptions"

    # No TimestampMixin: docs/04-database-schema.md Section 7's own DDL for
    # this table has no created_at/updated_at column — same "don't add a
    # column the schema doc doesn't list" convention Subcontractor's own
    # model docstring establishes. This row is a live mirror of Stripe's own
    # subscription state, not a historical record with its own lifecycle to
    # timestamp.
    #
    # ROOT-ONLY OWNERSHIP (design spec Section 1): a row here may only
    # belong to a company with parent_id IS NULL. Not expressible as a
    # plain CHECK constraint (would require a trigger to inspect another
    # table) — enforced at the application layer instead, at the single
    # point subscriptions get created (Task 3.19, POST /auth/register).
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    stripe_customer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stripe_subscription_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    included_seats: Mapped[int] = mapped_column(Integer, nullable=False)
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Manual join, not f"tier IN {VALID_TIERS!r}": repr() of a 1-element
        # tuple has a trailing comma ("('x',)"), which is invalid inside a
        # SQL IN (...) list — same _X_CHECK_SQL convention every other
        # status/enum-like column in this codebase already uses (lead.py,
        # user.py, project.py, task.py, ...).
        CheckConstraint(_TIER_CHECK_SQL, name="ck_subscriptions_tier"),
        UniqueConstraint("company_id", name="uq_subscriptions_company_id"),
        UniqueConstraint("stripe_subscription_id", name="uq_subscriptions_stripe_subscription_id"),
    )
