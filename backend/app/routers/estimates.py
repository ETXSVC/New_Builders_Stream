"""Task 2.10: `POST /estimates`, `GET /estimates`, `GET /estimates/{id}`.

Task 2.9 (models/schemas, merged) deliberately left the "exactly one of
project_id/lead_id, and it must actually resolve to something real and
eligible" rule out of `EstimateCreateRequest` (see that schema's own
docstring in `app/schemas/estimate.py`) — a DB lookup can't happen inside a
Pydantic validator, so it's this router's job, same division of
responsibility `ProjectStatusUpdateRequest`/`LeadUpdateRequest` already
establish between "is this a well-formed value" (schema) and "is this a
legal thing to do against the current DB state" (router).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import Estimate, EstimateLineItem, Lead, Project
from app.models.estimate import VALID_STATUSES
from app.schemas.estimate import (
    EstimateCreateRequest,
    EstimateDetailResponse,
    EstimateListResponse,
    EstimateResponse,
)
from app.schemas.estimate_line_item import EstimateLineItemResponse
from app.services.audit import write_audit_log

router = APIRouter(prefix="/estimates", tags=["estimates"])

# docs/07-security-compliance.md Section 2's RBAC matrix, Estimation row:
# "Full CRUD" for Admin/PM only — same two roles catalogs.py's _WRITE_ROLES
# uses for its structurally identical Admin/PM-full, Accountant-read,
# Client-scoped shape.
_WRITE_ROLES = ("admin", "project_manager")

# Every role with *some* read access to Estimation per the matrix: Admin/PM
# (full), Accountant ("Read"), and Client — whose only documented grant is
# "Approve/reject own estimate (e-sign)" (design decision #3's "own
# estimate" framing), which this task's own spec text interprets as also
# implying read access to `sent`-status estimates: the approve/reject
# action needs the client to be able to SEE what they're approving first.
# `client` is therefore included here (unlike catalogs.py's _READ_ROLES,
# which excludes it outright — Cost Catalog/Markup Profile data has no
# client-facing grant at all) but is additionally SCOPED inside
# list_estimates/get_estimate below, not just gated at the role-check
# layer. Field Crew gets nothing on this row and is absent from both tuples.
_READ_ROLES = ("admin", "project_manager", "accountant", "client")

# "Estimating or later in the forward pipeline" per Task 2.10's own
# judgment call: app/services/lead_transitions.py documents the linear
# spine `new -> contacted -> estimating -> qualified -> won` (plus a `lost`
# off-ramp reachable from any non-terminal stage). A Lead is eligible to
# have an Estimate created against it once it has reached or passed the
# `estimating` point on that FORWARD spine — `estimating`, `qualified`, or
# `won`. `lost` is deliberately EXCLUDED even though it's chronologically
# reachable "after" some earlier non-terminal stages: `lost` is an off-ramp
# out of the pipeline, not a position further along it, and a Lead that has
# been lost shouldn't gain new Estimates created against it. `new`/
# `contacted` are excluded because they haven't reached `estimating` yet at
# all.
_LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE = ("estimating", "qualified", "won")


async def _get_estimate_or_404(current: CurrentUser, estimate_id: uuid.UUID) -> Estimate:
    """Shared existence/tenant check, same pattern as `_get_lead_or_404`/
    `_get_project_or_404` — RLS makes another tenant's estimate invisible,
    so this 404 covers both "doesn't exist" and "exists but isn't yours"
    identically (Inherited Invariant #8), intentionally indistinguishable
    from outside. Deliberately does NOT apply the client's `status='sent'`
    scoping that `list_estimates` applies below — Task 2.10's own spec
    frames that scoping around the list-and-act-on-it flow specifically,
    and doesn't ask for the same restriction on direct-by-id access."""
    result = await current.session.execute(select(Estimate).where(Estimate.id == estimate_id))
    estimate = result.scalar_one_or_none()
    if estimate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Estimate not found")
    return estimate


@router.post("", response_model=EstimateResponse, status_code=status.HTTP_201_CREATED)
async def create_estimate(
    payload: EstimateCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
) -> EstimateResponse:
    """US-4.1: an Estimate is built "against a Lead or Project" — exactly
    one of `project_id`/`lead_id`, never neither, never both. Each
    referenced id must additionally resolve to something real and visible
    (Inherited Invariant #8's "doesn't exist or isn't visible to you" 404,
    same pattern `create_catalog_item_override`'s `parent_catalog_item_id`
    check uses), and a referenced Lead must additionally be far enough
    along the pipeline to carry an Estimate at all
    (`_LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE` above).

    `markup_profile_id` is NOT validated against a real, visible
    MarkupProfile here — deliberately out of this task's scope (see the
    task's own resolved judgment calls): only `project_id`/`lead_id` get
    explicit rejection tests in Task 2.10's own test list. An
    invalid/cross-tenant `markup_profile_id` that somehow reached this far
    would surface as a DB-level IntegrityError on flush rather than a clean
    422/404 — left for a future task to tighten.
    """
    # Both absent, or both present, are equally rejected — the rule is
    # "exactly one," not "at least one." Checked before either id is
    # queried, so a caller supplying neither/both never leaks whether some
    # unrelated id would otherwise have resolved.
    if (payload.project_id is None) == (payload.lead_id is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Exactly one of project_id or lead_id must be supplied",
        )

    if payload.project_id is not None:
        # No explicit company_id filter — same pattern as every other
        # single-row-by-id lookup in this codebase: the tenant_isolation
        # RLS policy already scopes visibility to the caller's own tenant.
        result = await current.session.execute(
            select(Project).where(Project.id == payload.project_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    if payload.lead_id is not None:
        result = await current.session.execute(select(Lead).where(Lead.id == payload.lead_id))
        lead = result.scalar_one_or_none()
        if lead is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")

        # The Lead exists and is visible — it's just not in the right
        # STATE yet, which is a validation/precondition failure (422), not
        # a "doesn't exist" 404 and not an illegal-TRANSITION 409 (this
        # isn't a transition check at all, there's no status being
        # written to the Lead here).
        if lead.status not in _LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Lead must be in one of {_LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE} status "
                f"to create an Estimate against it, got '{lead.status}'",
            )

    estimate = Estimate(
        company_id=current.company_id,
        project_id=payload.project_id,
        lead_id=payload.lead_id,
        markup_profile_id=payload.markup_profile_id,
        status="draft",
        subtotal=None,
        total=None,
        is_snapshotted=False,
    )
    current.session.add(estimate)
    await current.session.flush()

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="estimate.created",
        entity_type="estimate",
        entity_id=estimate.id,
    )
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    return EstimateResponse.model_validate(estimate)


@router.get("", response_model=EstimateListResponse)
async def list_estimates(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> EstimateListResponse:
    if status_filter is not None and status_filter not in VALID_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"status must be one of {VALID_STATUSES}"
        )

    # No explicit company_id filter — the tenant_isolation RLS policy
    # already scopes every row this query can see to the caller's active
    # tenant, same pattern list_leads/list_projects rely on.
    query = select(Estimate)

    # Client's "own estimate" scoping (design decision #3): a client only
    # ever sees `sent`-status estimates — the ones actually awaiting their
    # approve/reject action — never drafts still being built internally.
    # Same `if current.role == X: query = query.where(...)` shape
    # list_projects's field_crew scoping uses.
    if current.role == "client":
        query = query.where(Estimate.status == "sent")

    if status_filter is not None:
        query = query.where(Estimate.status == status_filter)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Estimate.created_at,
        id_col=Estimate.id,
        cursor=cursor,
        limit=limit,
    )

    return EstimateListResponse(
        items=[EstimateResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/{estimate_id}", response_model=EstimateDetailResponse)
async def get_estimate(
    estimate_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> EstimateDetailResponse:
    """Returns the Estimate header fields plus its current line items in one
    call (`EstimateDetailResponse`, `app/schemas/estimate.py`) — the
    frontend needs both to render a usable Estimate-editing UI. No
    `client`-specific `status='sent'` scoping here (unlike `list_estimates`
    above) — see `_get_estimate_or_404`'s docstring for why."""
    estimate = await _get_estimate_or_404(current, estimate_id)

    line_items_result = await current.session.execute(
        select(EstimateLineItem)
        .where(EstimateLineItem.estimate_id == estimate.id)
        .order_by(EstimateLineItem.id.asc())
    )
    line_items = list(line_items_result.scalars().all())

    return EstimateDetailResponse(
        **EstimateResponse.model_validate(estimate).model_dump(),
        line_items=[EstimateLineItemResponse.model_validate(li) for li in line_items],
    )
