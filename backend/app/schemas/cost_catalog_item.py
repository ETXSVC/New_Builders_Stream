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

    @computed_field
    @property
    def is_override(self) -> bool:
        return self.parent_catalog_item_id is not None
