import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BomLineManualCreateRequest(BaseModel):
    """A PM-typed line not sourced from any estimate — no cost_catalog_item_id
    accepted here (design spec Decision 1: manual lines carry their own
    description/unit directly, `source="manual"` set by the route, never by
    the caller)."""

    description: str = Field(..., min_length=1, max_length=255)
    unit: str = Field(..., min_length=1, max_length=50)
    quantity: Decimal


class BomLinePatchRequest(BaseModel):
    """Two independent actions in one request shape (design spec Decision
    3): `ordered=True` marks the line ordered (idempotent — a second
    request with `ordered=True` on an already-ordered line does not reset
    `ordered_at`); `vendor_id` attaches/reassigns a vendor, independently
    of `ordered`. There is no `ordered=False` un-marking path — not
    required by the spec, and `ordered_at`'s semantics on an un-mark
    aren't specified, so it's deliberately not built."""

    ordered: bool | None = None
    vendor_id: uuid.UUID | None = None


class BomLineReceiptCreateRequest(BaseModel):
    quantity: Decimal


class BomLineReceiptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    bom_line_id: uuid.UUID
    quantity: Decimal
    received_at: datetime
    recorded_by_user_id: uuid.UUID


class BomLineResponse(BaseModel):
    """No `model_config`/`from_attributes` — `quantity_received` and
    `status` aren't columns on the BomLine ORM instance (quantity_received
    is a SUM over a different table; status is derived from it), so this
    schema can never be built via `.model_validate(some_bom_line)` alone.
    Same reasoning `ProjectClientDashboardResponse` documents for its own
    router-computed fields (`app/schemas/project.py`) — always construct
    this explicitly, passing every field including the two computed ones."""

    id: uuid.UUID
    company_id: uuid.UUID
    project_id: uuid.UUID
    cost_catalog_item_id: uuid.UUID | None
    vendor_id: uuid.UUID | None
    description: str
    unit: str
    quantity: Decimal
    ordered: bool
    ordered_at: datetime | None
    source: Literal["estimate", "manual"]
    quantity_received: Decimal
    status: Literal["needed", "ordered", "partially_received", "received"]
    created_at: datetime
    updated_at: datetime


class BomLineListResponse(BaseModel):
    items: list[BomLineResponse]
    next_cursor: str | None = None
