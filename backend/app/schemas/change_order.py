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
    # Populated by the router via a join (not a mapped relationship on
    # ChangeOrder) — see list_all_change_orders below. `create_change_order`/
    # `list_change_orders` (nested-under-project routes, where the caller
    # already knows the project) still pass this through
    # model_validate(change_order) with project_name simply absent from the
    # instance; Pydantic v2's from_attributes leaves an unset field at its
    # default. Given a default of None here (not a required field) so those
    # two existing call sites don't need to change.
    project_name: str | None = None


class ChangeOrderListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /projects/{id}/change-orders`
    (Task 2.21), following the exact pattern of `DailyLogListResponse`
    (app/schemas/daily_log.py). `next_cursor` is `None` once the caller has
    reached the last page."""

    items: list[ChangeOrderResponse]
    next_cursor: str | None = None


class ChangeOrderRejectRequest(BaseModel):
    """Body for `POST /change-orders/{id}/reject` (Task 2.22) — the exact
    same shape as `EstimateRejectRequest` (`app/schemas/estimate.py`, Task
    2.19): `reason` is the ONE required field, since a rejection with no
    stated reason isn't what US-4.5's "reject it with a reason" (the same
    story this router's own approve/reject flow reuses for Change Orders)
    asks for. Plain `application/json`, unlike `approve`'s
    `multipart/form-data` body — rejection carries no binary signature
    artifact to submit here.

    No length cap on `reason`, matching `EstimateRejectRequest`'s own
    "free-text, no cap" convention — this is audit-log context, not a
    bounded VARCHAR column.
    """

    reason: str
