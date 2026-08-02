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

    `expected_unit_rate` is the rate the CALLER saw when they built this
    line, and is the one exception to "don't accept server-computed values
    from the client" above — because it is not accepted as a value. It is
    never stored and never used in any arithmetic; the route compares it to
    the catalog's current rate and refuses the whole request on a mismatch.
    An estimator still cannot assert a price.

    It exists because `unit_rate_snapshot` is copied at *replace-time*,
    which silently assumes the caller saw the rate an instant ago. They did
    not: an estimator picks items from the catalog panel, fills in
    quantities, and saves minutes later — and a catalog edit in between
    re-prices every line without telling anyone. The estimate then shows a
    rate the person who built it never saw, and may have quoted a customer
    against. The window is minutes today and would be days under any
    draft-now-submit-later flow (see
    `docs/superpowers/specs/2026-08-02-offline-pwa-design.md` §1.2).

    Optional, exactly like `expected_updated_at` in
    `app/services/concurrency.py`: omit it and the write proceeds
    unchecked, as before. Same reasoning as that module gives — making it
    mandatory is the stronger guarantee and the right eventual
    destination, but it would break every existing caller in one step.
    """

    cost_catalog_item_id: uuid.UUID
    quantity: Decimal
    expected_unit_rate: Decimal | None = None


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
