import uuid
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
    due_date: str | None
    bucket: str


class ProfitabilityReportResponse(BaseModel):
    projects: list[ProjectProfitability]
    ar_aging: list[AgingEntry]
    ap_aging: list[AgingEntry]
    tax_liability_estimate: Decimal
