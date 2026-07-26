import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UpdatedAtMixin, UUIDPKMixin


class BomLine(Base, UUIDPKMixin, TimestampMixin, UpdatedAtMixin):
    """One row per material line on a project's Bill of Materials. There is
    no separate "Bom" header record — lines hang directly off `project_id`
    (design spec Decision 2: one BOM per project, add-only).

    `cost_catalog_item_id` is nullable: NULL for a manually added line
    (`source="manual"`), set for a line generated from an approved estimate
    (`source="estimate"`). `description`/`unit` are copied at creation time
    from the CostCatalogItem (or typed directly for a manual line) rather
    than joined live, so a later catalog edit doesn't retroactively change
    what an existing BOM line says — same snapshot rationale
    `EstimateLineItem.unit_rate_snapshot` already uses.

    No `status` column: status is always computed from `ordered` +
    the SUM of this line's `BomLineReceipt` rows (design spec Decision 3),
    never stored, so it can never drift out of sync with the ledger.
    """

    __tablename__ = "bom_lines"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    cost_catalog_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cost_catalog_items.id"), nullable=True
    )
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    ordered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "estimate" | "manual" — never client-supplied on any request; each
    # route that creates a BomLine hardcodes the value for its own case, so
    # this is not validated against an enum at the schema layer.
    source: Mapped[str] = mapped_column(String(20), nullable=False)
