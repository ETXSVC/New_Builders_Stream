import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UpdatedAtMixin, UUIDPKMixin


class CostCatalogItem(Base, UUIDPKMixin, UpdatedAtMixin):
    __tablename__ = "cost_catalog_items"

    # No TimestampMixin: docs/04-database-schema.md Section 5's
    # `cost_catalog_items` table has no created_at column, only updated_at —
    # match the schema doc exactly (UpdatedAtMixin only, same "opt in only
    # where a matching column exists" discipline as every other Phase 0/1
    # model).

    # No ondelete here, matching the schema doc's
    # `company_id UUID NOT NULL REFERENCES companies(id)` (no ON DELETE clause) —
    # same convention as Lead.company_id / Project.company_id / MarkupProfile.company_id.
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    # Self-referencing FK: "links a branch override to its parent's item"
    # (docs/04-database-schema.md Section 5's inline comment). The schema
    # doc's literal SQL has no explicit ON DELETE clause here, so a judgment
    # call was made: ON DELETE SET NULL, not the Postgres default of NO
    # ACTION/RESTRICT. Rationale — if the ancestor catalog item this row
    # overrides is itself deleted, the override shouldn't block that
    # deletion (RESTRICT would), and the override row becoming a standalone
    # item (parent_catalog_item_id -> NULL) is a safer, more sensible
    # end-state than either blocking the delete or cascading it into
    # deleting every dependent override transitively (CASCADE would be
    # destructive and surprising here, unlike CASCADE's use elsewhere in
    # this schema for genuinely dependent child rows like Task/Phase).
    parent_catalog_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cost_catalog_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    unit_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # updated_at comes from UpdatedAtMixin (app/models/base.py) — a Cost
    # Catalog item has a mutable post-create lifecycle (unit_rate edits),
    # same rationale as Lead/Project's use of this mixin.
