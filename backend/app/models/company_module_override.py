import uuid

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedAtMixin, UUIDPKMixin

# Mirrors app/core/tier_gating.py's MODULE_MIN_TIER keys. That module is the
# authority on what a module *costs*; this is the same set of names, kept
# here so the CHECK constraint below can be built from it the way every
# other enum-like column in this codebase is built (see subscription.py's
# VALID_TIERS).
VALID_MODULES = ("estimation", "compliance", "accounting", "integrations", "child_branches")
_MODULE_CHECK_SQL = "module IN (" + ",".join(f"'{m}'" for m in VALID_MODULES) + ")"


class CompanyModuleOverride(Base, UUIDPKMixin, TimestampMixin, UpdatedAtMixin):
    """A per-tenant feature grant that overrides the subscription tier
    (migration 0023).

    Three states, and the difference between the last two matters:

      * no row              -> defer to the tier (`MODULE_MIN_TIER`)
      * row, enabled=True   -> granted regardless of tier
      * row, enabled=False  -> withheld even though the tier would allow it

    So deleting a row is "go back to what the plan says", which is a
    different act from switching the module off, and the console exposes
    both.

    `company_id` is always a ROOT company, for the same reason
    `Subscription.company_id` is: both gates resolve through
    `get_root_company_id()`, so an override attached to a child branch would
    never be consulted. Enforced at the application layer, following the
    precedent that model's own docstring sets.

    Writable only by the `platform_admin` database role. The runtime
    `app_user` role holds SELECT and nothing else, which is what keeps this
    an operator-controlled override rather than a self-service upgrade.
    """

    __tablename__ = "company_module_overrides"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    module: Mapped[str] = mapped_column(String(50), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Why this override exists — read by whoever finds it in six months and
    # wonders whether it is still meant to be here.
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    set_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (
        CheckConstraint(_MODULE_CHECK_SQL, name="ck_company_module_overrides_module"),
        UniqueConstraint(
            "company_id", "module", name="uq_company_module_overrides_company_module"
        ),
    )
