import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin, utcnow


class BomLineReceipt(Base, UUIDPKMixin):
    """An append-only ledger of delivery events against a BomLine — never a
    single mutable "quantity received so far" field. Mirrors the
    InvoicePayment/BillPayment pattern already established in this
    codebase (a running total is always derived by summing discrete
    recorded events), preserving a real audit trail of when materials
    arrived and who logged it (design spec Decision 2).

    `company_id` is carried directly on this table, not resolved through
    `bom_line_id`'s join — every RLS-protected table in this codebase
    carries its own `company_id` even when it also has a parent FK
    (InvoicePayment has both `invoice_id` and `company_id`), since the
    tenant-isolation policy filters on that column directly.

    No TimestampMixin: `received_at` (defaulting to `utcnow` at insert
    time, same as this app's other timestamp columns) already IS this
    row's creation timestamp — a receipt is immutable after creation, so a
    separate `created_at` would always equal `received_at` and add nothing.
    """

    __tablename__ = "bom_line_receipts"

    bom_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bom_lines.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    recorded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
