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
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import delete, select

from app.config import settings
from app.core.uploads import read_upload_limited
from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.events import publish
from app.core.money import CENTS
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.tier_gating import require_module
from app.models import CostCatalogItem, Estimate, EstimateLineItem, Lead, MarkupProfile, Project
from app.models.estimate import VALID_STATUSES
from app.schemas.estimate import (
    CategorySubtotal,
    EstimateCalculationResponse,
    EstimateCreateRequest,
    EstimateDetailResponse,
    EstimateListResponse,
    EstimatePatchRequest,
    EstimateRejectRequest,
    EstimateResponse,
)
from app.schemas.estimate_line_item import EstimateLineItemResponse, EstimateLineItemsReplaceRequest
from app.services.audit import write_audit_log
from app.services.catalog_resolution import resolve_visible_catalog_items
from app.services.esignature import capture_esignature
from app.services.estimate_calculation import calculate_estimate
from app.tasks.estimate_pdf import generate_estimate_pdf

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
# read back from the DB. `CENTS` lives in `app/core/money.py`, not here —
# `app/services/estimate_calculation.py` (Task 2.12) needs this same
# constant, and a service importing it from a router would invert this
# codebase's established dependency direction.


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


def _require_estimate_sent(estimate: Estimate) -> None:
    """Task 2.19: `approve`/`reject` share an identical "only legal from
    status='sent'" precondition — extracted here rather than duplicated
    inline in both routes, matching this router's existing "small shared
    helper for a check used more than once" precedent (`_get_estimate_or_404`
    itself). Raises 409 if `estimate.status` isn't `'sent'`; a no-op
    otherwise."""
    if estimate.status != "sent":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Estimate must be in 'sent' status to approve/reject, got '{estimate.status}'",
        )


@router.post("", response_model=EstimateResponse, status_code=status.HTTP_201_CREATED)
async def create_estimate(
    payload: EstimateCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> EstimateResponse:
    """US-4.1: an Estimate is built "against a Lead or Project" — exactly
    one of `project_id`/`lead_id`, never neither, never both. Each
    referenced id must additionally resolve to something real and visible
    (Inherited Invariant #8's "doesn't exist or isn't visible to you" 404,
    same pattern `create_catalog_item_override`'s `parent_catalog_item_id`
    check uses), and a referenced Lead must additionally be far enough
    along the pipeline to carry an Estimate at all
    (`_LEAD_STATUSES_ELIGIBLE_FOR_ESTIMATE` above).

    `markup_profile_id` IS validated against a real, visible MarkupProfile
    here (added during Task 2.12's review, closing a gap Task 2.10 had
    deliberately left open): the FK constraint alone only checks row
    EXISTENCE, not RLS visibility, so a well-formed cross-tenant
    `markup_profile_id` was previously accepted here with 201 and only
    surfaced as an unhandled `NoResultFound` 500 the first time
    `POST /estimates/{id}/calculate` (Task 2.12) tried to look the profile
    up — a real, ordinary-API-reachable crash, not a theoretical edge case.
    Closing it at creation time, the same "doesn't exist or isn't visible
    to you" 404 every other referenced-id check in this route already
    uses, is more correct than only catching the symptom later in
    `calculate_estimate`.
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

    # `resolved_company_id` — the referenced Project's/Lead's own
    # `company_id`, NOT `current.company_id` — is what actually stamps the
    # new Estimate below. A parent company's session can legitimately
    # create an Estimate against a descendant branch's Project/Lead
    # without switching `X-Tenant-ID` to that branch first (RLS's
    # `get_all_descendant_ids()` grant already makes the descendant's rows
    # visible/writable). Using `current.company_id` would silently stamp
    # this Estimate with the PARENT's id instead of its own Project's/
    # Lead's, producing a row whose `company_id` disagrees with the
    # resource it's actually built against — a session later scoped
    # directly to the descendant branch would then find its own Project's/
    # Lead's Estimate invisible under RLS. Same bug class already fixed in
    # change_orders.py/expenses.py/subcontractor_assignments.py and
    # projects.py's upload_document/create_daily_log, per the post-Phase-2
    # audit of this exact pattern — this route was missed by that audit.
    resolved_company_id = current.company_id

    if payload.project_id is not None:
        # No explicit company_id filter — same pattern as every other
        # single-row-by-id lookup in this codebase: the tenant_isolation
        # RLS policy already scopes visibility to the caller's own tenant.
        result = await current.session.execute(
            select(Project).where(Project.id == payload.project_id)
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
        resolved_company_id = project.company_id

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
        resolved_company_id = lead.company_id

    markup_result = await current.session.execute(
        select(MarkupProfile).where(MarkupProfile.id == payload.markup_profile_id)
    )
    if markup_result.scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Markup profile not found")

    estimate = Estimate(
        company_id=resolved_company_id,
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
        company_id=resolved_company_id,
        actor_id=current.user.id,
        action="estimate.created",
        entity_type="estimate",
        entity_id=estimate.id,
    )
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    return EstimateResponse.model_validate(estimate)


@router.patch("/{estimate_id}", response_model=EstimateResponse)
async def update_estimate(
    estimate_id: uuid.UUID,
    payload: EstimatePatchRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> EstimateResponse:
    """Draft-only (spec Decision 1, item 5) — 409 once sent/approved/rejected,
    same "existence/tenant before semantic validation" ordering every other
    guarded mutation in this router uses. Only `markup_profile_id` is
    accepted; changing it does NOT retroactively touch already-computed
    `subtotal`/`total` or any line item's `unit_rate_snapshot` — a caller
    must re-run `POST /calculate` to see the new markup applied, same
    "recalculation is a deliberate, explicit step" precedent
    `calculate_estimate_totals`'s own docstring establishes.
    """
    estimate = await _get_estimate_or_404(current, estimate_id)

    if estimate.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Estimate must be in 'draft' status to edit, got '{estimate.status}'",
        )

    markup_result = await current.session.execute(
        select(MarkupProfile).where(MarkupProfile.id == payload.markup_profile_id)
    )
    if markup_result.scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Markup profile not found")

    estimate.markup_profile_id = payload.markup_profile_id
    await current.session.flush()
    return EstimateResponse.model_validate(estimate)


@router.delete("/{estimate_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_estimate(
    estimate_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> None:
    """Draft-only, same guard as update_estimate above. Line items cascade
    with the parent row at the DB level — migration `0007_estimates_schema.py`
    declares `estimate_line_items.estimate_id` with `ondelete="CASCADE"`
    (verified directly in that migration file), so no explicit
    `delete(EstimateLineItem)` call is needed here before deleting the
    estimate itself; Postgres removes the child rows as part of the same
    statement."""
    estimate = await _get_estimate_or_404(current, estimate_id)

    if estimate.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Estimate must be in 'draft' status to delete, got '{estimate.status}'",
        )

    await current.session.delete(estimate)
    await current.session.flush()


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

    # parent_name resolution: two disjoint id sets (project-backed vs
    # lead-backed estimates in this page), each resolved with one query —
    # avoids N+1 without needing a join in the base paginate() query (which
    # would have to LEFT JOIN both projects and leads and coalesce, adding
    # complexity to the one Select paginate() operates on for a page that
    # may be entirely one kind or the other).
    project_ids = {row.project_id for row in rows if row.project_id is not None}
    lead_ids = {row.lead_id for row in rows if row.lead_id is not None}

    project_names: dict[uuid.UUID, str] = {}
    if project_ids:
        project_result = await current.session.execute(
            select(Project.id, Project.name).where(Project.id.in_(project_ids))
        )
        project_names = dict(project_result.tuples().all())

    lead_names: dict[uuid.UUID, str] = {}
    if lead_ids:
        lead_result = await current.session.execute(
            select(Lead.id, Lead.project_name).where(Lead.id.in_(lead_ids))
        )
        lead_names = dict(lead_result.tuples().all())

    items = []
    for row in rows:
        response = EstimateResponse.model_validate(row)
        if row.project_id is not None:
            response.parent_name = project_names.get(row.project_id)
        elif row.lead_id is not None:
            response.parent_name = lead_names.get(row.lead_id)
        items.append(response)

    return EstimateListResponse(items=items, next_cursor=next_cursor)


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
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
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
    line items are left completely untouched. Three independent checks,
    all performed before any DELETE/INSERT is issued:
      1. `estimate.is_snapshotted` -> 409 (design decision #4: an approved/
         snapshotted Estimate's line items are immutable).
      2. `estimate.status == "sent"` -> 409: an estimate awaiting the
         client's signature must not be editable out from under them
         between send-for-signature and their approve/reject (closes a
         gap this guard previously missed — `is_snapshotted` alone only
         starts being `True` at approval, not at send). `"rejected"`
         deliberately stays editable, per reject_estimate's own docstring.
      3. Every input line's `cost_catalog_item_id` must resolve via
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

    # `status == "sent"` is blocked separately from `is_snapshotted` above:
    # an estimate stops being editable the moment it's sent for the
    # client's signature, not only once approved/snapshotted. Without this,
    # line items could still be replaced (and calculate_estimate_totals
    # re-run) between send-for-signature and the client's approve/reject —
    # letting the client e-sign a total they never actually reviewed.
    # `status == "rejected"` deliberately remains editable here — see
    # reject_estimate's own docstring above ("this route... does not
    # otherwise lock the estimate"), a documented, separate decision this
    # fix does not change.
    if estimate.status == "sent":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Estimate is awaiting client signature and its line items cannot be modified "
            "until it is approved or rejected",
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
        # See `CENTS`'s own module-level comment above for why this
        # quantize() is required, not optional: unquantized Decimal
        # multiplication would return more than 2 decimal places.
        line_total = (quantity * unit_rate_snapshot).quantize(CENTS, rounding=ROUND_HALF_UP)
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


@router.post("/{estimate_id}/calculate", response_model=EstimateCalculationResponse)
async def calculate_estimate_totals(
    estimate_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> EstimateCalculationResponse:
    """Task 2.12: US-4.3's "I can trigger a recalculation" — runs
    `app/services/estimate_calculation.py`'s fixed-order calculation engine
    (docs/03-technical-architecture.md Section 6) against this Estimate's
    CURRENT line items and MarkupProfile, then persists the result as the
    authoritative `estimate.subtotal`/`estimate.total`. "Client-submitted
    totals are always ignored in favor of a server-side recompute" (Section
    6's own closing line) — this route takes no request body at all; there
    is nothing for a client to submit here, the recompute is unconditional
    and always server-derived.

    **409 if `estimate.is_snapshotted`** — identical guard, identical
    rationale, to `replace_estimate_line_items` above (design decision #4):
    an approved/snapshotted Estimate's totals are as immutable as its line
    items, and recomputing them would silently contradict the very
    snapshot that was taken.

    **409 if `estimate.status == "sent"`** — same additional guard as
    `replace_estimate_line_items` above, same rationale: a total
    recalculated while an estimate is awaiting the client's signature would
    let them e-sign a total they never actually reviewed. `"rejected"`
    deliberately stays recalculable, per reject_estimate's own docstring.

    Both checks are raised BEFORE `calculate_estimate` is ever called — a
    409 here guarantees `estimate.subtotal`/`total` are left completely
    untouched, same "one transaction, one outcome" discipline as Task
    2.11's own guard.

    Returns `EstimateCalculationResponse` (`app/schemas/estimate.py`) — the
    same header + line_items shape `GET /estimates/{id}` returns, plus this
    route's own `category_breakdown` (resolved judgment call #5): the
    caller gets to see the just-recomputed totals, the line items they were
    computed from, and the category-level view in one response, without a
    follow-up `GET`.
    """
    estimate = await _get_estimate_or_404(current, estimate_id)

    if estimate.is_snapshotted:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Estimate is snapshotted and its totals can no longer be recalculated",
        )

    # Same "sent" guard, same rationale, as replace_estimate_line_items
    # above: a total recalculated between send-for-signature and the
    # client's approve/reject would let the client e-sign a total they
    # never actually reviewed. `status == "rejected"` deliberately remains
    # recalculable here — see reject_estimate's own docstring.
    if estimate.status == "sent":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Estimate is awaiting client signature and its totals cannot be recalculated "
            "until it is approved or rejected",
        )

    calculation = await calculate_estimate(current.session, estimate)

    estimate.subtotal = calculation.subtotal
    estimate.total = calculation.total
    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    line_items_result = await current.session.execute(
        select(EstimateLineItem)
        .where(EstimateLineItem.estimate_id == estimate.id)
        .order_by(EstimateLineItem.id.asc())
    )
    line_items = list(line_items_result.scalars().all())

    return EstimateCalculationResponse(
        **EstimateResponse.model_validate(estimate).model_dump(),
        line_items=[EstimateLineItemResponse.model_validate(li) for li in line_items],
        category_breakdown=[
            CategorySubtotal(category=entry.category, subtotal=entry.subtotal)
            for entry in calculation.category_breakdown
        ],
    )


@router.post(
    "/{estimate_id}/export", response_model=EstimateResponse, status_code=status.HTTP_202_ACCEPTED
)
async def export_estimate_pdf(
    estimate_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> EstimateResponse:
    """Task 2.15: enqueues async PDF generation
    (`app/tasks/estimate_pdf.py`'s `generate_estimate_pdf` Dramatiq actor)
    and returns immediately (202) with the Estimate's now-`pending` header —
    `EstimateResponse` (resolved judgment call #6), not
    `EstimateDetailResponse`/`EstimateCalculationResponse`, since this route
    doesn't touch line items, matching `create_estimate`'s own "minimal
    sufficient schema per route" precedent (Task 2.10).

    No `total IS NULL` guard (resolved judgment call #7) and no
    `is_snapshotted` guard (resolved judgment call #8): neither is asked for
    by this task's own spec, and both are real, reachable, already-supported
    states this route has no reason to reject — an un-calculated Estimate
    renders "Not yet calculated" placeholders (Task 2.13's
    `render_estimate_html`), and a snapshotted/approved Estimate is exactly
    the state where a client-facing PDF is most likely to actually be
    needed (a signed, final proposal document). The `is_snapshotted` guard
    that DOES exist on `replace_estimate_line_items`/
    `calculate_estimate_totals` above exists specifically because those
    routes MUTATE line items/totals, which this one does not.

    `estimate.pdf_status` is set to `'pending'` via the normal
    request-scoped session (Inherited Invariant #4 — no explicit commit
    here; `get_current_user` commits `current.session` once, after this
    handler returns) before the job is enqueued. `current.user.id` (the
    admin/PM making this call) is captured now and passed through the
    Dramatiq message payload as `requesting_user_id` — the actor needs a
    real, non-optional user id to call `set_current_user` inside its own,
    separately-managed session (Inherited Invariant #3's worker exception;
    see `app/tasks/estimate_pdf.py`'s module docstring for the full
    rationale, including why this is NOT used to write an audit_log entry).
    """
    estimate = await _get_estimate_or_404(current, estimate_id)

    estimate.pdf_status = "pending"
    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    generate_estimate_pdf.send(str(estimate.id), str(current.user.id))

    return EstimateResponse.model_validate(estimate)


@router.get("/{estimate_id}/pdf")
async def download_estimate_pdf(
    estimate_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> Response:
    """Streams the exported PDF from `pdf_storage_path`. Same read roles as
    `get_estimate` (admin/PM/accountant/client) — a client needs this to
    actually see what they're about to sign, same reasoning `_READ_ROLES`
    already documents at the top of this module.

    409, not 404, when `pdf_status != "ready"`: the Estimate itself exists
    and is visible, it just has no artifact to serve yet (or export failed) —
    a real, reachable state, not "doesn't exist."
    """
    estimate = await _get_estimate_or_404(current, estimate_id)

    if estimate.pdf_status != "ready" or estimate.pdf_storage_path is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Estimate PDF is not ready (pdf_status={estimate.pdf_status!r})",
        )

    absolute_path = Path(settings.storage_root) / estimate.pdf_storage_path
    pdf_bytes = absolute_path.read_bytes()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="estimate-{estimate.id}.pdf"'},
    )


@router.post("/{estimate_id}/send-for-signature", response_model=EstimateResponse)
async def send_estimate_for_signature(
    estimate_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> EstimateResponse:
    """Task 2.19: US-4.5's send-for-signature step — marks an Estimate as
    awaiting the client's approve/reject action. No e-signature is captured
    here at all; this route only flips `status` to `'sent'` so the estimate
    becomes visible to `client`-role callers (`list_estimates`'s own
    `status='sent'` scoping) and eligible for the `approve`/`reject` routes
    below, both of which require `status='sent'` as their own precondition.

    **409 if `estimate.total IS NULL`**: `total`
    only becomes non-NULL after at least one successful
    `POST /estimates/{id}/calculate` run — sending an un-calculated estimate
    for the client's signature makes no sense, there is nothing meaningful
    for them to review yet. Checked AFTER `_get_estimate_or_404` (existence/
    tenant before semantic validation, the same ordering
    `calculate_estimate_totals`'s own `is_snapshotted` check uses above).
    """
    estimate = await _get_estimate_or_404(current, estimate_id)

    if estimate.total is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Estimate has not been calculated yet and cannot be sent for signature",
        )

    estimate.status = "sent"
    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    return EstimateResponse.model_validate(estimate)


@router.post("/{estimate_id}/approve", response_model=EstimateResponse)
async def approve_estimate(
    estimate_id: uuid.UUID,
    request: Request,
    signer_name: str = Form(...),
    signer_email: str = Form(...),
    signature_artifact: UploadFile = File(...),
    current: CurrentUser = Depends(require_role("client")),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> EstimateResponse:
    """Task 2.19: US-4.5's "As a Client, I can review an emailed Estimate
    and approve it with an e-signature." `require_role("client")` only
    (design decision #3's authenticated-in-app-client model) — Admin/PM/
    Accountant never approve their own estimate on a client's behalf.

    Only legal from `status='sent'` (`_require_estimate_sent`, 409
    otherwise) — checked immediately after `_get_estimate_or_404`, before
    the uploaded file is even read, so an illegal-state call never touches
    the filesystem.

    `multipart/form-data`, matching `upload_document`'s exact precedent
    (`app/routers/projects.py`) — `signer_name`/`signer_email` as `Form(...)`
    fields, `signature_artifact` as `File(...)`, read via `.read()` into raw
    bytes. No content-type/extension validation on the uploaded file:
    `write_esignature_artifact_file` already hardcodes a `.png` extension
    regardless of what was actually uploaded (Task 2.18), so there is
    nothing meaningful to validate here.

    Side effects, in order: capture the e-signature (`capture_esignature`,
    Task 2.18, `document_type="estimate"` — a valid member of
    `VALID_DOCUMENT_TYPES`, `app/models/esignature.py`), link it onto this
    Estimate, flip `status='approved'` and **`is_snapshotted=True`** (design
    decision #4 — from this instant forward, `PUT /estimates/{id}/lines` and
    `POST /estimates/{id}/calculate` both 409 on this estimate permanently),
    write an `estimate.approved` audit log entry, then publish
    `ESTIMATE_APPROVED`.

    `ip_address`: `request.client.host` if `request.client` is not `None`,
    else the literal string `"unknown"` — defensive against the rare ASGI
    scope where `request.client` is `None`; `capture_esignature`'s
    `ip_address` parameter is a non-optional `str` matching
    `Esignature.ip_address`'s non-nullable column, so some value must
    always be passed.

    `ESTIMATE_APPROVED`'s payload includes `project_id`, which **may be
    `None`** — an Estimate created against a bare Lead (no Project yet) has
    no `project_id` at all (`Estimate.project_id`'s own nullable column).
    This is intentional and expected, not an oversight:
    `app.services.estimate_approved_handler.handle_estimate_approved`
    (Task 3.39, registered via `app.core.event_handlers
    .register_event_handlers()`) no-ops silently on a `None` project_id
    rather than requiring one; this task deliberately does not add a NOT
    NULL constraint or an artificial Project requirement just to make this
    field always populated.
    """
    estimate = await _get_estimate_or_404(current, estimate_id)
    _require_estimate_sent(estimate)

    signature_artifact_bytes = await read_upload_limited(
        signature_artifact, settings.max_signature_upload_bytes
    )
    ip_address = request.client.host if request.client else "unknown"

    esignature = await capture_esignature(
        current.session,
        company_id=current.company_id,
        signer_name=signer_name,
        signer_email=signer_email,
        ip_address=ip_address,
        document_type="estimate",
        signature_artifact_bytes=signature_artifact_bytes,
    )

    estimate.esignature_id = esignature.id
    estimate.status = "approved"
    estimate.is_snapshotted = True
    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="estimate.approved",
        entity_type="estimate",
        entity_id=estimate.id,
    )

    # project_id may be None — see this route's own docstring above for why
    # that's intentional and Phase 3's job to handle, not this task's.
    #
    # session=current.session (Task 3.39): app.services.estimate_approved_handler
    # .handle_estimate_approved is now registered against this event and
    # must reuse this exact AsyncSession (Inherited Invariant #4 — see that
    # module's own docstring) so its Invoice/audit_log writes join the same
    # transaction get_current_user commits once, after this route returns.
    # Matches LEAD_WON's own publish() call (app/routers/leads.py) passing
    # session=current.session for the identical reason.
    await publish(
        "ESTIMATE_APPROVED",
        session=current.session,
        estimate_id=estimate.id,
        project_id=estimate.project_id,
        company_id=estimate.company_id,
        approved_total=estimate.total,
    )

    return EstimateResponse.model_validate(estimate)


@router.post("/{estimate_id}/reject", response_model=EstimateResponse)
async def reject_estimate(
    estimate_id: uuid.UUID,
    payload: EstimateRejectRequest,
    current: CurrentUser = Depends(require_role("client")),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> EstimateResponse:
    """Task 2.19: US-4.5's "or reject it with a reason." Same `client`-only
    role gate and `status='sent'` precondition (`_require_estimate_sent`,
    409 otherwise) as `approve` above — both routes share the exact same
    check via that one helper.

    No e-signature is captured and `is_snapshotted` stays `False`: a
    rejection isn't a signed document, so there is nothing to snapshot and
    nothing decoupling this Estimate's line items/totals from live catalog
    data. `PUT /estimates/{id}/lines` and `POST /estimates/{id}/calculate`
    both remain legal on a rejected estimate — this route only records the
    rejection (`status='rejected'` plus an `estimate.rejected` audit log
    entry carrying `{reason}`), it does not otherwise lock the estimate.

    `EstimateRejectRequest` (`app/schemas/estimate.py`) is a plain JSON
    body, not `multipart/form-data` — unlike `approve`, there is no binary
    signature artifact to submit here.
    """
    estimate = await _get_estimate_or_404(current, estimate_id)
    _require_estimate_sent(estimate)

    estimate.status = "rejected"
    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    await write_audit_log(
        current.session,
        company_id=current.company_id,
        actor_id=current.user.id,
        action="estimate.rejected",
        entity_type="estimate",
        entity_id=estimate.id,
        metadata={"reason": payload.reason},
    )

    return EstimateResponse.model_validate(estimate)
