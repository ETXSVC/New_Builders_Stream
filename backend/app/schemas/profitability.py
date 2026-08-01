import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class ProjectProfitability(BaseModel):
    project_id: uuid.UUID
    # Resolved server-side rather than left to the caller. The alternative
    # is a client fetching /projects and joining by id, which cannot be done
    # correctly: that list is cursor-paginated, so a client would have to
    # walk every page to be sure of covering the ids in this report.
    #
    # Nullable because the join is a LEFT one — a project deleted after the
    # invoices that reference it still belongs in the report, and dropping
    # the row would quietly understate revenue.
    project_name: str | None
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
