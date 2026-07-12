import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ChangeOrderCreateRequest(BaseModel):
    """Body for `POST /projects/{id}/change-orders` (Task 2.21). Per
    docs/04-database-schema.md Section 4's `change_orders` table and
    `ChangeOrder`'s own docstring (Task 2.20, `app/models/change_order.py`).

    Deliberately has NO `status` field: the server always sets
    `status="pending"` at creation (the router, never client input, is the
    only place that value is ever assigned) — same "server owns the initial
    status, schema doesn't expose it" pattern `EstimateCreateRequest` already
    establishes (`app/schemas/estimate.py`), which likewise has no `status`
    field even though `Estimate.status` exists and defaults server-side.

    `cost_delta`'s sign is deliberately unconstrained here too, for the same
    reason `ChangeOrder.cost_delta` itself carries no CHECK constraint
    (Task 2.20): per US-3.6, a Change Order can legitimately be a credit
    (negative) or an add (positive) to project cost, so there's no business
    rule for Pydantic to enforce here that the DB column itself doesn't
    enforce.
    """

    description: str
    cost_delta: Decimal
    schedule_impact_days: int = 0


class ChangeOrderResponse(BaseModel):
    """Full model. No `updated_at` field — `ChangeOrder` (Task 2.20) has no
    such column at all (TimestampMixin only, no UpdatedAtMixin); see that
    model's own docstring for why `status` being mutable post-create doesn't
    require one here the way it did for `Estimate`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    company_id: uuid.UUID
    description: str
    cost_delta: Decimal
    schedule_impact_days: int
    status: str
    esignature_id: uuid.UUID | None
    created_at: datetime


class ChangeOrderListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /projects/{id}/change-orders`
    (Task 2.21), following the exact pattern of `DailyLogListResponse`
    (app/schemas/daily_log.py). `next_cursor` is `None` once the caller has
    reached the last page."""

    items: list[ChangeOrderResponse]
    next_cursor: str | None = None
