import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    ## Two shapes, exactly one per line (migration 0035)

    A line is either **catalogued** — `cost_catalog_item_id`, and the name,
    unit and price all come from that item — or **free-form**:
    `description` + `unit` + `unit_rate`, written by the estimator for work
    the catalog does not price (site cleanup, a permit fee, a one-off
    allowance). Never a mixture, never neither; `_exactly_one_shape` below
    rejects both, and a CHECK constraint makes the invalid state
    unrepresentable for any writer that bypasses this schema.

    `unit_rate` is the one place a caller's price reaches a stored column,
    and the docstring above says an estimator cannot assert a price. Both
    are true: that rule protects a catalogued line from disagreeing with its
    catalog item, and a free-form line has no catalog item to disagree with.
    Supplying `unit_rate` on a catalogued line is a 422, not a silent drop —
    a caller who tried to price a catalog item deserves to be told they
    cannot, rather than getting a 200 whose stored rate is not the one they
    sent.
    """

    cost_catalog_item_id: uuid.UUID | None = None
    quantity: Decimal
    expected_unit_rate: Decimal | None = None
    # Free-form lines only. Lengths mirror `cost_catalog_items.name`/`.unit`,
    # the fields these stand in for.
    description: str | None = Field(None, min_length=1, max_length=255)
    unit: str | None = Field(None, min_length=1, max_length=50)
    unit_rate: Decimal | None = None

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> "EstimateLineItemInput":
        catalogued = self.cost_catalog_item_id is not None
        free_form = any(
            value is not None for value in (self.description, self.unit, self.unit_rate)
        )
        if catalogued and free_form:
            raise ValueError(
                "a line item is either catalogued (cost_catalog_item_id) or free-form "
                "(description, unit, unit_rate) — not both; a catalogued line's name, "
                "unit and price all come from its catalog item"
            )
        if not catalogued and not free_form:
            raise ValueError(
                "supply either cost_catalog_item_id, or description + unit + unit_rate"
            )
        if free_form:
            # Checked as a group rather than field-by-field: a free-form line
            # with a description and no rate is not a line, it is half of one,
            # and the caller needs to hear that rather than have a price
            # invented for them.
            missing = [
                name
                for name, value in (
                    ("description", self.description),
                    ("unit", self.unit),
                    ("unit_rate", self.unit_rate),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"a free-form line item also requires: {', '.join(missing)}")
            if self.expected_unit_rate is not None:
                # `expected_unit_rate` guards against the CATALOG moving
                # underneath the caller. A free-form line has no catalog
                # entry, so there is nothing for it to be stale against, and
                # accepting it silently would imply a check that never ran.
                raise ValueError(
                    "expected_unit_rate does not apply to a free-form line item — there is "
                    "no catalog rate for it to be checked against"
                )
        return self


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
    # None on a free-form line (migration 0035), where `description`/`unit`
    # carry what the catalog item would have supplied.
    cost_catalog_item_id: uuid.UUID | None
    description: str | None
    unit: str | None
    quantity: Decimal
    unit_rate_snapshot: Decimal
    line_total: Decimal
