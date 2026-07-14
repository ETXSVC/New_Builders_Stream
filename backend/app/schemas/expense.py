import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ExpenseCreateRequest(BaseModel):
    description: str
    amount: Decimal
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
    next_cursor: str | None
