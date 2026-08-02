"""Migration 0033: a tenant's own deposit percentage and tax rate.

Both are FRACTIONS (0.08875, not 8.875) — every consumer multiplies
directly — and both are nullable so a tenant can set one without being
forced to state the other. `app/services/financial_settings.py` resolves
each independently: the company's own value, else its root's, else the code
default.
"""
import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class CompanyFinancialSettings(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "company_financial_settings"

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id"), nullable=False, unique=True
    )
    # NUMERIC(6,5): real US sales-tax rates carry three decimals as a
    # percentage (8.875% → 0.08875), which two would round away. Bounded to
    # [0, 1] by CHECK constraints in the migration — a "10% deposit" entered
    # as 10 would otherwise bill ten times the contract value.
    deposit_percentage: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    tax_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
