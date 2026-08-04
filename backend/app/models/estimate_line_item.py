import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin

# Migration 0035. A line is EITHER catalogued (an item id, no description or
# unit of its own) OR free-form (a description and unit the estimator wrote,
# no item id) — never a mixture, and never neither. Kept as a module constant
# so the model and the migration state the rule once.
CATALOGUED_XOR_FREE_FORM = (
    "(cost_catalog_item_id IS NOT NULL AND description IS NULL AND unit IS NULL)"
    " OR "
    "(cost_catalog_item_id IS NULL AND description IS NOT NULL AND unit IS NOT NULL)"
)


class EstimateLineItem(Base, UUIDPKMixin):
    __tablename__ = "estimate_line_items"

    # No created_at/updated_at: docs/04-database-schema.md Section 5's
    # `estimate_line_items` table has no timestamp columns at all —
    # intentionally not adding TimestampMixin/UpdatedAtMixin here to match
    # the schema doc exactly, same discipline as Phase/MarkupProfile.

    # ON DELETE CASCADE per the schema doc:
    # `estimate_id UUID NOT NULL REFERENCES estimates(id) ON DELETE CASCADE`
    # — deleting an Estimate deletes its line items, same shape as
    # Phase.project_id's cascade.
    estimate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("estimates.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Estimate.company_id / Project.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # No ondelete here, matching the schema doc's original
    # `cost_catalog_item_id UUID NOT NULL REFERENCES cost_catalog_items(id)`
    # (no ON DELETE clause).
    #
    # NULLABLE since migration 0035, and that is load-bearing for two queries
    # that used to lean on it: `app/services/estimate_calculation.py` and
    # `app/tasks/estimate_pdf.py` both joined through this column to reach the
    # catalog item's category, each with a comment explaining that an INNER
    # JOIN was safe because this could never be NULL. Both are outer joins
    # now. Anything else that joins here must do the same, or free-form lines
    # silently disappear from whatever it computes.
    cost_catalog_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cost_catalog_items.id"), nullable=True
    )
    # Free-form lines only — what the estimator typed, standing in for the
    # catalog item's `name` and `unit`. NULL on a catalogued line, where both
    # live on the catalog item and would otherwise be a second copy free to
    # drift from it.
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Copied from CostCatalogItem.unit_rate at add-time rather than joined/
    # looked-up live — intentionally a separate column, per the schema doc's
    # own Section 9 note: this is what implements the historical-
    # immutability rule. A later edit to the catalog's unit_rate must NOT
    # retroactively change what an already-built Estimate shows or totals.
    #
    # On a FREE-FORM line there is no catalog item to copy from, so this is
    # the rate the estimator supplied. That is the single place in this
    # codebase where a caller's price reaches a stored column, and it is
    # narrow on purpose: the historical-immutability rule above exists to stop
    # a catalogued line disagreeing with its catalog item, and a line with no
    # catalog item has nothing to disagree with. See migration 0035.
    unit_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    __table_args__ = (
        CheckConstraint(CATALOGUED_XOR_FREE_FORM, name="ck_estimate_line_items_catalogued_xor_free_form"),
    )
