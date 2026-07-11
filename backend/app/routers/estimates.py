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
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, select

from app.core.deps import CurrentUser, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import CostCatalogItem, Estimate, EstimateLineItem, Lead, Project
from app.models.estimate import VALID_STATUSES
from app.schemas.estimate import (
    EstimateCreateRequest,
    EstimateDetailResponse,
    EstimateListResponse,
    EstimateResponse,
)
from app.schemas.estimate_line_item import EstimateLineItemResponse, EstimateLineItemsReplaceRequest
from app.services.audit import write_audit_log
from app.services.catalog_resolution import resolve_visible_catalog_items

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

# `EstimateLineItem.line_total`'s column is `Numeric(12, 2)`
# (app/models/estimate_line_item.py) — but `quantity * unit_rate_snapshot`
# in exact Decimal arithmetic does NOT stay at 2 decimal places on its own:
# Decimal multiplication's result scale is the SUM of its operands' scales
# (e.g. Decimal("10.00") * Decimal("45.00") -> Decimal("450.0000"), 4
# decimal places, not 2), even though both operands individually came from
# 2-decimal-place currency columns/fields. Quantizing explicitly to 2
# places here — rather than relying on Postgres to silently round on
# INSERT — guarantees the value this handler returns in its own response
# (built from the in-memory ORM object, never re-queried after flush)
# always matches exactly what a subsequent `GET /estimates/{id}` would
# read back from the DB. ROUND_HALF_UP (ties away from zero) matches
# PostgreSQL's own NUMERIC rounding behavior for positive amounts, which
# every quantity/rate in this domain is.
_CENTS = Decimal("0.01")


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


@router.put("/{estimate_id}/lines", response_model=EstimateDetailResponse)
async def replace_estimate_line_items(
    estimate_id: uuid.UUID,
    payload: EstimateLineItemsReplaceRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
) -> EstimateDetailResponse:
    """Task 2.11: US-4.3's "As a Project Manager, I can add/edit line items
    with quantities" — a full batch replace (API spec's own wording), not a
    partial patch/append: every existing `EstimateLineItem` row for this
    Estimate is deleted and the request body's `items` are inserted fresh,
    in the same request. There is no natural key to diff old vs. new line
    items against beyond `cost_catalog_item_id` (and the API spec doesn't
    forbid duplicate `cost_catalog_item_id` entries in one request), so
    delete-then-insert is the simplest reading of "full replace" that avoids
    inventing an unrequested dedup/merge rule.

    **Validate everything before mutating anything**, same "one transaction,
    one outcome" discipline `update_lead`'s status-transition check
    established in `app/routers/leads.py`: this handler never calls
    `session.commit()` itself (Inherited Invariant #4 — `get_current_user`
    commits once after the handler returns), so as long as nothing below
    raises AFTER the DELETE/INSERTs are issued, a mid-request 409/422 here
    guarantees the eventual commit never happens at all and the estimate's
    line items are left completely untouched. Two independent checks, both
    performed before any DELETE/INSERT is issued:
      1. `estimate.is_snapshotted` -> 409 (design decision #4: an approved/
         snapshotted Estimate's line items are immutable).
      2. Every input line's `cost_catalog_item_id` must resolve via
         `resolve_visible_catalog_items` -> 422 on the FIRST one that
         doesn't. Resolved (not raw-table-queried) so the rate captured
         below respects inheritance/override resolution — a PM building an
         estimate against an item their branch has overridden must get the
         override's rate, not the ancestor's original, same reasoning
         `create_catalog_item_override` (`app/routers/catalogs.py`) already
         applies to its own `parent_catalog_item_id` visibility check.
         `resolve_visible_catalog_items` is called ONCE for the whole
         request (not once per input line) and the result turned into an
         id-keyed dict for lookup, same shape `create_catalog_item_override`
         uses for its own single-call/membership-check pattern.

    `unit_rate_snapshot` is COPIED from the resolved item's `unit_rate` at
    this moment, never a live reference (schema doc Section 9's historical-
    immutability rule, `EstimateLineItem.unit_rate_snapshot`'s own docstring
    in `app/models/estimate_line_item.py`) — a later edit to the catalog
    item must not retroactively change what this Estimate shows. `line_total
    = quantity * unit_rate_snapshot` is plain `Decimal` arithmetic (both
    operands are already `Decimal` — `EstimateLineItemInput.quantity` and
    `CostCatalogItem.unit_rate` are both `Numeric` columns / `Decimal`
    schema fields, never `float`), matching Inherited Invariant #9.

    Does NOT recompute `estimate.subtotal`/`total` — that is `POST
    /estimates/{id}/calculate`'s job (Task 2.12), a deliberately separate,
    explicit step per US-4.3's own "I can trigger a recalculation" framing.

    `EstimateLineItem.company_id` is set to `current.company_id` on every
    new row, matching `create_estimate`'s own convention of always sourcing
    `company_id` from the current user rather than the parent row (the two
    are guaranteed equal here, since `_get_estimate_or_404` already
    RLS-scoped `estimate` to the caller's own tenant, but `current.company_id`
    is used for consistency with every other create path in this codebase).

    Returns `EstimateDetailResponse` (Task 2.10's `GET /estimates/{id}`
    shape) rather than a bespoke response — the frontend needs to see the
    resulting line items immediately after a replace, and this schema
    already exists precisely for "header + current line items in one
    response."
    """
    estimate = await _get_estimate_or_404(current, estimate_id)

    if estimate.is_snapshotted:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Estimate is snapshotted and its line items can no longer be modified",
        )

    resolved_items = await resolve_visible_catalog_items(current.session, current.company_id)
    resolved_by_id: dict[uuid.UUID, CostCatalogItem] = {item.id: item for item in resolved_items}

    resolved_lines: list[tuple[uuid.UUID, Decimal, CostCatalogItem]] = []
    for line in payload.items:
        catalog_item = resolved_by_id.get(line.cost_catalog_item_id)
        if catalog_item is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"cost_catalog_item_id {line.cost_catalog_item_id} does not resolve to a "
                "visible catalog item for this company",
            )
        resolved_lines.append((line.cost_catalog_item_id, line.quantity, catalog_item))

    # Nothing above this point has touched estimate_line_items — both
    # failure modes (409, 422) raise before either the DELETE or any INSERT
    # is issued.
    await current.session.execute(
        delete(EstimateLineItem).where(EstimateLineItem.estimate_id == estimate.id)
    )

    new_line_items: list[EstimateLineItem] = []
    for cost_catalog_item_id, quantity, catalog_item in resolved_lines:
        unit_rate_snapshot = catalog_item.unit_rate
        # See `_CENTS`'s own module-level comment above for why this
        # quantize() is required, not optional: unquantized Decimal
        # multiplication would return more than 2 decimal places.
        line_total = (quantity * unit_rate_snapshot).quantize(_CENTS, rounding=ROUND_HALF_UP)
        line_item = EstimateLineItem(
            estimate_id=estimate.id,
            company_id=current.company_id,
            cost_catalog_item_id=cost_catalog_item_id,
            quantity=quantity,
            unit_rate_snapshot=unit_rate_snapshot,
            line_total=line_total,
        )
        current.session.add(line_item)
        new_line_items.append(line_item)

    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    return EstimateDetailResponse(
        **EstimateResponse.model_validate(estimate).model_dump(),
        line_items=[EstimateLineItemResponse.model_validate(li) for li in new_line_items],
    )
