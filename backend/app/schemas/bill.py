import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator


class BillCreateRequest(BaseModel):
    project_id: uuid.UUID | None = None
    subcontractor_id: uuid.UUID | None = None
    vendor_name: str | None = None
    amount: Decimal
    due_date: date | None = None
    bill_number: str | None = None

    @model_validator(mode="after")
    def _require_a_vendor(self) -> "BillCreateRequest":
        if self.subcontractor_id is None and not self.vendor_name:
            raise ValueError("Either subcontractor_id or vendor_name is required")
        return self


class BillPaymentCreateRequest(BaseModel):
    amount: Decimal
    paid_date: date


class BillPaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bill_id: uuid.UUID
    amount: Decimal
    paid_date: date
    recorded_by: uuid.UUID
    created_at: datetime


class BillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    project_id: uuid.UUID | None
    subcontractor_id: uuid.UUID | None
    vendor_name: str | None
    bill_number: str | None
    amount: Decimal
    status: str
    due_date: date | None
    created_at: datetime
    outstanding_balance: Decimal


class BillDetailResponse(BillResponse):
    payments: list[BillPaymentResponse]


class BillListResponse(BaseModel):
    items: list[BillResponse]
    next_cursor: str | None
