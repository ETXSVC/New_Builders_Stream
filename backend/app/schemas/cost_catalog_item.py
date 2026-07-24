import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class CostCatalogItemCreateRequest(BaseModel):
    """Plain "brand-new catalog item" create — deliberately has NO
    `parent_catalog_item_id` field. Creating an *override* (a branch's local
    replacement for an ancestor's item) is a distinct operation from creating
    a standalone item, not a variant of the same request shape; Task 2.4/2.5
    define the override-specific routing (`resolve_visible_catalog_items`,
    a separate override endpoint/flow) on top of this plain create.

    `unit_rate` is `Decimal`, never `float` (this codebase's
    "monetary/percentage values are always Decimal" invariant, same as
    `MarkupProfileCreateRequest.overhead_pct`/`profit_pct`).
    """

    category: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=255)
    unit: str = Field(..., min_length=1, max_length=50)
    unit_rate: Decimal


class CostCatalogItemPatchRequest(BaseModel):
    """Body for `PATCH /catalogs/items/{id}`. All fields optional — a PATCH
    only touches what's supplied, matching `ProjectPatchRequest`'s own
    all-optional convention (`app/schemas/project.py`)."""

    category: str | None = Field(None, min_length=1, max_length=100)
    name: str | None = Field(None, min_length=1, max_length=255)
    unit: str | None = Field(None, min_length=1, max_length=50)
    unit_rate: Decimal | None = None


class CostCatalogItemResponse(BaseModel):
    """Full model, plus `is_override` — a value trivially derivable from
    this row's own `parent_catalog_item_id` (`is_override =
    parent_catalog_item_id is not None`), so it's implemented as a Pydantic
    v2 `@computed_field` directly on the schema rather than computed by the
    router.

    This differs from `ProjectClientDashboardResponse`
    (`app/schemas/project.py`), whose `phase_count`/`task_count`/
    `completed_task_count` fields are router-computed: those values require
    `COUNT` queries against OTHER tables (`phases`/`tasks`) that aren't
    reachable from a bare `Project` ORM instance, so that schema can't be
    built via `from_attributes=True` at all and documents that explicitly
    (no `model_config` on it). `is_override` has no such dependency — every
    `CostCatalogItem` instance already carries `parent_catalog_item_id`, so
    `CostCatalogItemResponse.model_validate(item)` (`from_attributes=True`)
    can compute it inline with no router involvement, no extra query, and no
    risk of the router forgetting to pass it.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    parent_catalog_item_id: uuid.UUID | None
    category: str
    name: str
    unit: str
    unit_rate: Decimal
    updated_at: datetime

    # mypy can't model decorators stacked on @property (a documented
    # pydantic/mypy limitation); the pattern itself is pydantic's own
    # recommended computed-field form.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_override(self) -> bool:
        return self.parent_catalog_item_id is not None


class CostCatalogItemListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /catalogs/items`, same shape
    as `LeadListResponse` (app/schemas/lead.py) — but see
    `app/routers/catalogs.py`'s own module docstring for why the cursor this
    route hands back is NOT produced by `app/core/pagination.py`'s
    `paginate()`: the list being paginated here is `resolve_visible_catalog_items`'
    already-materialized, in-memory, deduped result, not a SQLAlchemy
    `Select`. `next_cursor` is `None` once the caller has reached the last
    page, same convention as every other list envelope in this codebase."""

    items: list[CostCatalogItemResponse]
    next_cursor: str | None = None


class CostCatalogItemBulkCreateRequest(BaseModel):
    """Body for `POST /catalogs/items/bulk` — CSV import. Max 500 rows per
    call (spec Decision 9): large enough for a real catalog seed, small
    enough that one request stays well within normal timeout/payload
    budgets without needing chunked upload."""

    items: list[CostCatalogItemCreateRequest] = Field(..., max_length=500)


class CostCatalogItemBulkResultEntry(BaseModel):
    """One row's outcome. `detail` is populated on `status="error"` only —
    the created item's id isn't returned per-row (the caller can re-list to
    see the full resulting catalog); this response is a validation report,
    not a bulk-create response envelope."""

    index: int
    status: str
    detail: str | None = None


class CostCatalogItemBulkResponse(BaseModel):
    results: list[CostCatalogItemBulkResultEntry]
