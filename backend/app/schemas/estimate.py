import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class EstimateCreateRequest(BaseModel):
    """`POST /estimates` (Task 2.10). `project_id` and `lead_id` are both
    `uuid.UUID | None` here — deliberately NOT narrowed with a
    `@model_validator` enforcing "exactly one of project_id/lead_id" even
    though US-4.1 requires an Estimate be built "against a Lead or
    Project." Pydantic validators run on the request body in isolation,
    with no DB access; correctly enforcing this rule requires a DB lookup
    to confirm the referenced Lead/Project actually exists and is in a
    state that can carry an Estimate (e.g. the Lead is 'estimating'+
    status), not just that a UUID was supplied. Both fields absent, or an
    unrelated/nonexistent id being sent, must also be rejected, which is
    equally not expressible from the request body alone. That check
    therefore belongs in the ROUTER (Task 2.10), which has a DB session,
    not in this schema — same division of responsibility as
    `ProjectStatusUpdateRequest`/`LeadUpdateRequest`'s value-vs-transition
    validation split (`app/schemas/project.py`, `app/schemas/lead.py`).
    """

    project_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    markup_profile_id: uuid.UUID


class EstimateResponse(BaseModel):
    """Full model, including the three PDF-tracking fields (`pdf_status`,
    `pdf_storage_path`, `pdf_generated_at`) — design decision #5's
    documented extension beyond docs/04-database-schema.md Section 5's
    `estimates` table (see `Estimate`'s own docstring,
    `app/models/estimate.py`). These are included specifically so the
    frontend can poll this exact `GET /estimates/{id}` route to learn
    when an export has finished, rather than a separate export-status
    route.

    `subtotal`/`total`/`esignature_id` are all `Decimal | None` /
    `uuid.UUID | None` matching the model's nullable columns exactly:
    subtotal/total stay NULL until the first `POST
    /estimates/{id}/calculate` run (Task 2.12); esignature_id stays NULL
    until the Estimate is sent for signature (Task 2.17+). `project_id`/
    `lead_id` are both nullable for the same "against a Lead or Project"
    reason as `EstimateCreateRequest` above.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    project_id: uuid.UUID | None
    lead_id: uuid.UUID | None
    markup_profile_id: uuid.UUID
    status: str
    subtotal: Decimal | None
    total: Decimal | None
    is_snapshotted: bool
    esignature_id: uuid.UUID | None
    pdf_status: str
    pdf_storage_path: str | None
    pdf_generated_at: datetime | None
    created_at: datetime
    updated_at: datetime


class EstimateListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /estimates` (Task 2.10, not
    yet built), following the exact pattern of `LeadListResponse`
    (app/schemas/lead.py) / `CostCatalogItemListResponse`
    (app/schemas/cost_catalog_item.py): defined alongside the resource's
    own `XResponse` ahead of the route that will use it, same as Task
    2.3's schemas anticipated Task 2.5's `GET /catalogs/items`.
    `next_cursor` is `None` once the caller has reached the last page.
    """

    items: list[EstimateResponse]
    next_cursor: str | None = None
