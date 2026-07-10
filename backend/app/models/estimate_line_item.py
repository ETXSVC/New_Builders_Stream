import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin


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
    # No ondelete here, matching the schema doc's
    # `cost_catalog_item_id UUID NOT NULL REFERENCES cost_catalog_items(id)`
    # (no ON DELETE clause).
    cost_catalog_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cost_catalog_items.id"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Copied from CostCatalogItem.unit_rate at add-time rather than joined/
    # looked-up live — intentionally a separate column, per the schema doc's
    # own Section 9 note: this is what implements the historical-
    # immutability rule. A later edit to the catalog's unit_rate must NOT
    # retroactively change what an already-built Estimate shows or totals.
    unit_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
