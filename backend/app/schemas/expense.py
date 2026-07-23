import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ExpenseCreateRequest(BaseModel):
    description: str
    # gt=0: a zero/negative expense represents no real cost incurred and
    # is never a legitimate state — same floor this codebase's other
    # money-recording create schemas (InvoiceCreateRequest,
    # BillCreateRequest) enforce.
    amount: Decimal = Field(gt=0)
    incurred_on: date


class ExpenseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    company_id: uuid.UUID
    description: str
    amount: Decimal
    incurred_on: date


class ExpenseListResponse(BaseModel):
    items: list[ExpenseResponse]
    next_cursor: str | None = None
