import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class ProjectProfitability(BaseModel):
    project_id: uuid.UUID
    billed_revenue: Decimal
    actual_cost: Decimal
    profitability: Decimal


class AgingEntry(BaseModel):
    id: uuid.UUID
    outstanding_balance: Decimal
    # date, not str: Pydantic serializes this to ISO format on its own.
    # The router passes the row's own due_date (a date | None) straight
    # through — no .isoformat() call needed on the way in.
    due_date: date | None
    bucket: str


class ProfitabilityReportResponse(BaseModel):
    projects: list[ProjectProfitability]
    ar_aging: list[AgingEntry]
    ap_aging: list[AgingEntry]
    tax_liability_estimate: Decimal
