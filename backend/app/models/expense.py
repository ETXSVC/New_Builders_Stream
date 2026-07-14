import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPKMixin


class Expense(Base, UUIDPKMixin):
    __tablename__ = "expenses"

    # No TimestampMixin: docs/04-database-schema.md Section 7's expenses
    # table has no created_at column — incurred_on is the meaningful date
    # for this row, same "the domain date plays the timestamp role" pattern
    # ComplianceNotification.fired_at already establishes.
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    incurred_on: Mapped[date] = mapped_column(nullable=False)
