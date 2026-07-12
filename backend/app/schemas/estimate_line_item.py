import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class EstimateLineItemInput(BaseModel):
    """One entry inside `EstimateLineItemsReplaceRequest.items` — not a
    standalone create-route body (there is no `POST
    /estimates/{id}/lines`; line items are only ever written via the
    batch-replace route, API spec Section 5). `unit_rate_snapshot`/
    `line_total` are deliberately NOT fields here: both are
    server-computed at replace-time (unit_rate copied from the referenced
    `CostCatalogItem.unit_rate` at that moment, line_total derived from
    `quantity * unit_rate_snapshot`), never supplied by the caller — same
    "don't accept server-computed values from the client" discipline as
    `CostCatalogItemResponse.is_override` being computed rather than
    stored input.

    `quantity` is `Decimal`, never `float` (this codebase's monetary/
    quantity invariant, same as `MarkupProfileCreateRequest.overhead_pct`/
    `CostCatalogItemCreateRequest.unit_rate`).
    """

    cost_catalog_item_id: uuid.UUID
    quantity: Decimal


class EstimateLineItemsReplaceRequest(BaseModel):
    """Body for `PUT /estimates/{id}/lines` (Task 2.10) — API spec Section
    5's documented "batch replace line items" shape: the full, authoritative
    set of line items for the Estimate, replacing whatever was there
    before (not a partial add/patch)."""

    items: list[EstimateLineItemInput]


class EstimateLineItemResponse(BaseModel):
    """Full model. No `EstimateLineItemListResponse` envelope exists
    alongside this response (unlike `Estimate`/`Lead`/`CostCatalogItem`):
    line items have no independent list route in docs/05-api-specification.md
    Section 5's route table — they're only ever read as part of an
    `EstimateResponse` (nested, once Task 2.10's `GET /estimates/{id}`
    is built), never paginated/listed on their own."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    estimate_id: uuid.UUID
    company_id: uuid.UUID
    cost_catalog_item_id: uuid.UUID
    quantity: Decimal
    unit_rate_snapshot: Decimal
    line_total: Decimal
