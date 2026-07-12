import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import ChangeOrder
from app.routers.projects import _get_project_or_404
from app.schemas.change_order import (
    ChangeOrderCreateRequest,
    ChangeOrderListResponse,
    ChangeOrderResponse,
)

# Task 2.21: Change Orders. Deliberately its OWN file rather than more
# additions to projects.py, following tasks.py's own precedent (Task 1.14)
# exactly: projects.py was already the largest router (~600 lines by this
# point in the plan, larger than when tasks.py itself split off), and Change
# Orders will keep growing across this task and Tasks 2.22/2.23
# (send-for-signature, approve/reject, and a project-completion-blocking
# check) — splitting now avoids repeating tasks.py's own "already unwieldy"
# problem a second time.
#
# No APIRouter prefix, same reasoning tasks.py gives for itself: both routes
# below nest under /projects/{project_id}/change-orders, but Task 2.22 will
# add top-level /change-orders/{id}/... routes (a ChangeOrder addressed
# directly, not via a project_id path param) — the same mixed-prefix shape
# tasks.py already has (phase/task creation nests under
# /projects/{project_id}/..., but PATCH /tasks/{id} is top-level). Each route
# below spells out its own full path string, exactly like tasks.py does.
router = APIRouter(tags=["change_orders"])

# US-3.6 / API spec Section 4: Change Order creation is Admin/PM only, same
# ("admin", "project_manager") tuple every other project-nested creation
# route in this codebase uses (tasks.py's own _WRITE_ROLES, projects.py's
# _WRITE_ROLES). Duplicated here rather than imported, matching tasks.py's
# own precedent of owning its role constants rather than reaching into
# another router's private namespace for a value this small.
_WRITE_ROLES = ("admin", "project_manager")

# Read access for `GET /projects/{id}/change-orders` (list). Deliberately
# EXCLUDES `client`, unlike Estimate's own _READ_ROLES (app/routers/
# estimates.py), which does include `client`. The RBAC matrix's Estimation
# row gives `client` "Approve/reject own estimate (e-sign)" — that's WHY
# Estimate's list route scopes `client` to `status='sent'` estimates
# specifically (the ones actually awaiting the client's action), not to
# every Estimate in the tenant. `ChangeOrder.VALID_STATUSES`
# (app/models/change_order.py, Task 2.20) is only
# ("pending", "approved", "rejected") — there is no "sent"-equivalent status
# to scope client visibility by yet, since send-for-signature/approve/reject
# for Change Orders doesn't exist until Task 2.22. Exposing every `pending`
# Change Order to `client` now, before there's any concrete "this is ready
# for your action" signal, would contradict the established pattern (client
# only ever sees things actually awaiting their own action, never internal
# drafts-in-progress). This is a deliberate, TEMPORARY scope decision for
# THIS task, not a final answer — Task 2.22 (which adds the client-facing
# send-for-signature/approve/reject flow) is the natural place to revisit
# whether/how `client` gets read access here.
_READ_ROLES = ("admin", "project_manager", "accountant")


@router.post(
    "/projects/{project_id}/change-orders",
    response_model=ChangeOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_change_order(
    project_id: uuid.UUID,
    payload: ChangeOrderCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
) -> ChangeOrderResponse:
    """Task 2.21. `_get_project_or_404` first, same ordering as every other
    project-nested write route in this codebase (existence/tenant check
    before any semantic validation of the payload) — a cross-tenant
    project_id and a not-`active` project both fail, but the caller learns
    "not found" before "wrong status", never the other way around, same
    precedent `upload_document`'s docstring gives for its own ordering
    (app/routers/projects.py).

    Only legal against an `active` Project — US-3.6: "create a Change Order
    against an ACTIVE Project" (not draft/pre_construction/suspended/
    completed/archived). Rejected with 409, not 422: this isn't a malformed
    request body, it's a real row (the Project) that exists and is visible
    to the caller but is in the wrong state for this operation — the same
    category of conflict `update_project_status`'s illegal-transition check
    uses 409 for.

    `company_id=current.company_id`, not `project.company_id` — matching the
    established precedent this codebase's own nested-resource-creation
    routes actually use: `upload_document` and `create_daily_log`
    (app/routers/projects.py) both set `company_id=current.company_id` on
    the child row they create, not `company_id=project.company_id`. Followed
    here for consistency with that precedent rather than introducing a new
    one.

    `status="pending"` always — `ChangeOrderCreateRequest` has no `status`
    field (see its own docstring), so a caller cannot set it via payload.

    No audit_log entry: docs/07-security-compliance.md Section 5's
    audit-worthy-action list is "Change Order approval," not creation.
    (Note: `create_estimate` does write an `estimate.created` audit entry —
    so this is NOT a "creation is never audited" codebase-wide rule, just
    what Section 5's own enumerated list actually asks for here.) Task
    2.22's approve route is where an audit entry belongs for Change
    Orders, exactly like `Estimate.approved`.
    """
    project = await _get_project_or_404(current, project_id)

    if project.status != "active":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Change Orders can only be created against an active Project "
            f"(current status: {project.status!r})",
        )

    change_order = ChangeOrder(
        project_id=project.id,
        company_id=current.company_id,
        description=payload.description,
        cost_delta=payload.cost_delta,
        schedule_impact_days=payload.schedule_impact_days,
        status="pending",
    )
    current.session.add(change_order)
    await current.session.flush()
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns.

    return ChangeOrderResponse.model_validate(change_order)


@router.get("/projects/{project_id}/change-orders", response_model=ChangeOrderListResponse)
async def list_change_orders(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ChangeOrderListResponse:
    """Task 2.21. Not in the API spec's literal route table (only `POST
    /projects/{id}/change-orders` is listed there) — added for the same
    "spec is conceptual" reasoning applied repeatedly through this and the
    Phase 1 plan (e.g. `list_documents`'/`list_daily_logs`' own docstrings,
    app/routers/projects.py): without a list route, a PM has no way to see
    a project's Change Order history at all.

    `_get_project_or_404` first, then `paginate()` scoped to this project —
    copies `list_documents`'s exact structure (imports, `Query` params,
    the `paginate()` call itself with `created_at_col`/`id_col`).

    Uses `_READ_ROLES` (admin, project_manager, accountant — `client`
    excluded), not `_get_project_or_404`'s field_crew-scoping machinery:
    field_crew has no read access to Change Orders at all per the RBAC
    matrix's Project Management row (field_crew's only granted verb there is
    "create Daily Logs"), so it's simply absent from `_READ_ROLES` and
    blocked with a 403 at the `require_role` dependency layer before this
    handler body ever runs.
    """
    project = await _get_project_or_404(current, project_id)

    query = select(ChangeOrder).where(ChangeOrder.project_id == project.id)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=ChangeOrder.created_at,
        id_col=ChangeOrder.id,
        cursor=cursor,
        limit=limit,
    )

    return ChangeOrderListResponse(
        items=[ChangeOrderResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )
