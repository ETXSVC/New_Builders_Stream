import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class InvoiceCreateRequest(BaseModel):
    amount: Decimal
    due_date: date | None = None


class InvoiceSendRequest(BaseModel):
    due_date: date | None = None


class InvoicePaymentCreateRequest(BaseModel):
    # gt=0, unlike InvoiceCreateRequest.amount above (which just records
    # what's owed): this field represents actual money changing hands and
    # feeds directly into whether an invoice's cumulative payments cross
    # its amount. A zero/negative value here isn't just bad data — since
    # the status transition only ever moves forward (never un-marks
    # "paid"), a negative "payment" recorded before the real total is
    # reached could permanently suppress the sum from ever crossing the
    # threshold.
    amount: Decimal = Field(gt=0)
    paid_date: date


class InvoicePaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    amount: Decimal
    paid_date: date
    recorded_by: uuid.UUID
    created_at: datetime


class InvoiceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    company_id: uuid.UUID
    estimate_id: uuid.UUID | None
    invoice_number: str
    amount: Decimal
    status: str
    due_date: date | None
    created_at: datetime
    # Not an Invoice column — always built by explicit keyword construction
    # (amount minus SUM(payments)), never via .model_validate(invoice)
    # directly. from_attributes=True is kept for consistency with every
    # other response schema in this codebase, harmless since this class is
    # never actually validated off an ORM row.
    outstanding_balance: Decimal


class InvoiceDetailResponse(InvoiceResponse):
    payments: list[InvoicePaymentResponse]


class InvoiceListResponse(BaseModel):
    items: list[InvoiceResponse]
    next_cursor: str | None = None
