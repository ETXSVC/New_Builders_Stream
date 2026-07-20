import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import select, tuple_

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, encode_cursor, paginate
from app.core.tier_gating import require_module
from app.models import ChangeOrder, Project
from app.routers.projects import _get_project_or_404
from app.schemas.change_order import (
    ChangeOrderCreateRequest,
    ChangeOrderListResponse,
    ChangeOrderRejectRequest,
    ChangeOrderResponse,
)
from app.services.audit import write_audit_log
from app.services.esignature import capture_esignature

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

# Read access for `GET /projects/{id}/change-orders` (list). `client` WAS
# temporarily excluded here as of Task 2.21 — Task 2.22 adds the
# client-facing send-for-signature/approve/reject flow, giving `client` a
# real, concrete action to take on a `ChangeOrder` in `status="pending"`
# (the "awaiting the
# client's action" status for ChangeOrder, the analog of Estimate's `"sent"`
# — `ChangeOrder.VALID_STATUSES`, app/models/change_order.py, is only
# `("pending", "approved", "rejected")`, no `"sent"`-equivalent of its own).
# `client` is therefore now INCLUDED here, same as Estimate's own
# `_READ_ROLES` (app/routers/estimates.py) — but additionally SCOPED inside
# `list_change_orders` below to `status="pending"` rows only, the exact same
# `if current.role == "client": query = query.where(...)` shape
# `list_estimates` uses for its own `status="sent"` scoping. Without this,
# `client` would have no way to ever discover the id of a `ChangeOrder`
# awaiting their approval — there is still no singular
# `GET /change-orders/{id}` route in this plan, so this list route is the
# only discovery mechanism `client` has.
_READ_ROLES = ("admin", "project_manager", "accountant", "client")


async def _get_change_order_or_404(current: CurrentUser, change_order_id: uuid.UUID) -> ChangeOrder:
    """Task 2.22: top-level lookup helper (not nested under a project_id
    path param), mirroring `_get_estimate_or_404`'s exact shape in
    `app/routers/estimates.py` — RLS makes another tenant's ChangeOrder
    invisible, so this 404 covers both "doesn't exist" and "exists but
    isn't yours" identically, intentionally indistinguishable from outside.
    Needed because `send-for-signature`/`approve`/`reject` below are all
    addressed by `change_order_id` directly, unlike `create_change_order`/
    `list_change_orders` above, which are nested under `project_id`."""
    result = await current.session.execute(
        select(ChangeOrder).where(ChangeOrder.id == change_order_id)
    )
    change_order = result.scalar_one_or_none()
    if change_order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Change Order not found")
    return change_order


@router.get("/change-orders/{change_order_id}", response_model=ChangeOrderResponse)
async def get_change_order(
    change_order_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> ChangeOrderResponse:
    """No client status scoping here (unlike list_change_orders/
    list_all_change_orders below) — same "direct-by-id access isn't scoped,
    only list-and-act-on-it flows are" precedent `_get_estimate_or_404`'s
    docstring establishes for Estimates."""
    change_order = await _get_change_order_or_404(current, change_order_id)
    return ChangeOrderResponse.model_validate(change_order)


@router.get("/change-orders", response_model=ChangeOrderListResponse)
async def list_all_change_orders(
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ChangeOrderListResponse:
    """Company-wide (not nested under a project_id) — the discovery
    mechanism a client needs to find every pending Change Order awaiting
    their action across ALL their projects, without N per-project list
    calls (spec Decision 1, item 8). `client` is scoped to `status="pending"`,
    same as list_change_orders' own per-project scoping.

    Joined to `projects` for `project_name` — a bare ChangeOrder row alone
    isn't enough context for a cross-project list row (unlike the
    per-project list, where the caller already knows which project they're
    looking at).

    This route hand-rolls cursor pagination (join queries can't pass a plain
    `Select[tuple[Row]]` through `paginate()` the way a single-model query
    can) rather than reusing `paginate()` directly — mirroring `catalogs.py`'s
    own precedent of a bespoke pagination helper where the generic one
    doesn't fit.
    """
    if status_filter is not None and status_filter not in ("pending", "approved", "rejected"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "status must be one of ('pending', 'approved', 'rejected')",
        )

    query = select(ChangeOrder, Project.name).join(Project, ChangeOrder.project_id == Project.id)

    if current.role == "client":
        query = query.where(ChangeOrder.status == "pending")
    if status_filter is not None:
        query = query.where(ChangeOrder.status == status_filter)

    query = query.order_by(ChangeOrder.created_at.asc(), ChangeOrder.id.asc()).limit(limit + 1)
    if cursor is not None:
        cursor_created_at, cursor_id = decode_cursor(cursor)
        query = query.where(
            tuple_(ChangeOrder.created_at, ChangeOrder.id) > (cursor_created_at, cursor_id)
        )

    result = await current.session.execute(query)
    rows = result.all()

    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]
        last_co, _ = rows[-1]
        next_cursor = encode_cursor(last_co.created_at, last_co.id)

    items = []
    for change_order, project_name in rows:
        response = ChangeOrderResponse.model_validate(change_order)
        response.project_name = project_name
        items.append(response)

    return ChangeOrderListResponse(items=items, next_cursor=next_cursor)


def _require_change_order_pending(change_order: ChangeOrder) -> None:
    """Task 2.22: `send-for-signature`/`approve`/`reject` share an identical
    "only legal from status='pending'" precondition — extracted here rather
    than duplicated inline in all three routes, matching
    `_require_estimate_sent`'s own precedent (`app/routers/estimates.py`).

    `"pending"`, not `"sent"`: `ChangeOrder.VALID_STATUSES`
    (app/models/change_order.py) is only
    `("pending", "approved", "rejected")` — there is no `"sent"`-equivalent
    status for a ChangeOrder to transition into (see
    `send_change_order_for_signature`'s own docstring below for why that
    route itself never mutates `status`), so `"pending"` remains the only
    legal pre-decision state a ChangeOrder can be in when a client
    approves/rejects it, or when send-for-signature's own readiness gate
    runs. Raises 409 if `change_order.status` isn't `'pending'`; a no-op
    otherwise."""
    if change_order.status != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Change Order must be in 'pending' status, got '{change_order.status}'",
        )


@router.post(
    "/projects/{project_id}/change-orders",
    response_model=ChangeOrderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_change_order(
    project_id: uuid.UUID,
    payload: ChangeOrderCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
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

    `company_id=project.company_id`, not `current.company_id`: a parent
    company's session can legitimately act on a descendant branch's Project
    without switching `X-Tenant-ID` to that branch first (RLS's
    `get_all_descendant_ids()` grant already makes the descendant's rows
    visible/writable). Using `current.company_id` here would silently stamp
    this ChangeOrder with the PARENT's id instead of the Project's own,
    producing a row whose `company_id` disagrees with its own parent
    Project's `company_id` — a session later scoped directly to the
    descendant branch would then find its own Project's ChangeOrder
    invisible under RLS. `upload_document`/`create_daily_log`
    (app/routers/projects.py) had the identical bug and are fixed
    alongside this route, in a dedicated post-Phase-2 audit of this exact
    pattern across every nested-resource-creation route in this codebase —
    this docstring previously (incorrectly) cited those two routes'
    `current.company_id` usage as an established precedent to follow; that
    was itself the bug, not a precedent worth matching.

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
        company_id=project.company_id,
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

    Uses `_READ_ROLES` (admin, project_manager, accountant, client — see
    that constant's own comment for `client`'s Task 2.22 addition and
    scoping), not `_get_project_or_404`'s field_crew-scoping machinery:
    field_crew has no read access to Change Orders at all per the RBAC
    matrix's Project Management row (field_crew's only granted verb there is
    "create Daily Logs"), so it's simply absent from `_READ_ROLES` and
    blocked with a 403 at the `require_role` dependency layer before this
    handler body ever runs.

    `client`'s own scoping (Task 2.22, mirroring `list_estimates`'s
    `status="sent"` scoping exactly): a client only ever sees `pending`
    Change Orders on this project — the ones actually awaiting their
    approve/reject action — never already-decided `approved`/`rejected`
    ones.
    """
    project = await _get_project_or_404(current, project_id)

    query = select(ChangeOrder).where(ChangeOrder.project_id == project.id)

    if current.role == "client":
        query = query.where(ChangeOrder.status == "pending")

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


@router.post("/change-orders/{change_order_id}/send-for-signature", response_model=ChangeOrderResponse)
async def send_change_order_for_signature(
    change_order_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> ChangeOrderResponse:
    """Task 2.22. Unlike `send_estimate_for_signature`
    (`app/routers/estimates.py`, Task 2.19), this route does NOT transition
    `status` at all — `ChangeOrder.VALID_STATUSES` has only three values
    (`"pending"`, `"approved"`, `"rejected"`), with no `"sent"`-equivalent
    for a ChangeOrder to move into. This route's entire job is therefore a
    pure validation gate: confirm the ChangeOrder exists/is visible, confirm
    it's still `"pending"` (409 otherwise, via `_require_change_order_pending`,
    mirroring Estimate's own guard for the same category of check), and
    return its current, UNCHANGED state. This is deliberate, not an
    oversight — a direct consequence of ChangeOrder's smaller status enum
    compared to Estimate's — and is why there is no `session.flush()` and no
    `change_order.sent`-style audit_log entry here: nothing is ever mutated
    by this route, so there is nothing to persist and nothing to audit. The
    plan's own audit-entry list for this task only names
    `change_order.approved`/`change_order.rejected`, never a `.sent`
    variant.
    """
    change_order = await _get_change_order_or_404(current, change_order_id)
    _require_change_order_pending(change_order)

    return ChangeOrderResponse.model_validate(change_order)


@router.post("/change-orders/{change_order_id}/approve", response_model=ChangeOrderResponse)
async def approve_change_order(
    change_order_id: uuid.UUID,
    request: Request,
    signer_name: str = Form(...),
    signer_email: str = Form(...),
    signature_artifact: UploadFile = File(...),
    current: CurrentUser = Depends(require_role("client")),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> ChangeOrderResponse:
    """Task 2.22: the same shape as `approve_estimate` (Task 2.19,
    `app/routers/estimates.py`) — `require_role("client")` only (design
    decision #3's authenticated-in-app-client model), calling the SAME
    shared `capture_esignature()` (Task 2.18) with
    `document_type="change_order"` (a valid member of
    `VALID_DOCUMENT_TYPES`, `app/models/esignature.py`, anticipated by Task
    2.17 for exactly this call site).

    Only legal from `status='pending'` (`_require_change_order_pending`, 409
    otherwise) — checked immediately after `_get_change_order_or_404`,
    before the uploaded file is even read, so an illegal-state call never
    touches the filesystem, same ordering `approve_estimate` uses.

    `multipart/form-data`, matching `approve_estimate`'s exact precedent —
    `signer_name`/`signer_email` as `Form(...)` fields, `signature_artifact`
    as `File(...)`, read via `.read()` into raw bytes.

    `company_id=change_order.company_id`, not `current.company_id`, for
    both the captured `Esignature` and the audit log entry below — same
    rationale as `create_change_order`'s own fix (this router's module):
    the acting `client` could in principle hold membership in a parent
    company acting on a descendant's ChangeOrder without switching
    `X-Tenant-ID`, and the resulting `Esignature`/`audit_log` rows must
    stay co-located (same `company_id`) with the `ChangeOrder` they
    evidence, not drift to whatever company the acting session happens to
    be in at that moment.

    Side effects, in order: capture the e-signature, link it onto this
    ChangeOrder (`esignature_id`), flip `status='approved'`, write a
    `change_order.approved` audit log entry. **No `is_snapshotted` flag** —
    unlike Estimate, `ChangeOrder` has no editable line-item collection and
    no such column at all (`ChangeOrder`'s own docstring, Task 2.20):
    `description`/`cost_delta`/`schedule_impact_days` are never PATCH-able
    by any route in this plan (immutability by omission, no update route
    exists), so there is nothing further to lock down on approval beyond
    the `status` transition itself. **No event publish** — unlike
    `approve_estimate`'s `ESTIMATE_APPROVED` publish, this task's plan text
    does not mention any `CHANGE_ORDER_APPROVED` event, so none is added
    here; only what's explicitly asked for.

    `ip_address`: `request.client.host` if `request.client` is not `None`,
    else the literal string `"unknown"` — same defensive fallback
    `approve_estimate` uses, for the same reason (`capture_esignature`'s
    `ip_address` parameter is a non-optional `str`).
    """
    change_order = await _get_change_order_or_404(current, change_order_id)
    _require_change_order_pending(change_order)

    signature_artifact_bytes = await signature_artifact.read()
    ip_address = request.client.host if request.client else "unknown"

    esignature = await capture_esignature(
        current.session,
        company_id=change_order.company_id,
        signer_name=signer_name,
        signer_email=signer_email,
        ip_address=ip_address,
        document_type="change_order",
        signature_artifact_bytes=signature_artifact_bytes,
    )

    change_order.esignature_id = esignature.id
    change_order.status = "approved"
    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    await write_audit_log(
        current.session,
        company_id=change_order.company_id,
        actor_id=current.user.id,
        action="change_order.approved",
        entity_type="change_order",
        entity_id=change_order.id,
    )

    return ChangeOrderResponse.model_validate(change_order)


@router.post("/change-orders/{change_order_id}/reject", response_model=ChangeOrderResponse)
async def reject_change_order(
    change_order_id: uuid.UUID,
    payload: ChangeOrderRejectRequest,
    current: CurrentUser = Depends(require_role("client")),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("estimation")),
) -> ChangeOrderResponse:
    """Task 2.22: the same shape as `reject_estimate` (Task 2.19,
    `app/routers/estimates.py`) — same `client`-only role gate and
    `status='pending'` precondition (`_require_change_order_pending`, 409
    otherwise) as `approve_change_order` above, both sharing the exact same
    check via that one helper.

    No e-signature is captured: a rejection isn't a signed document, so
    there is nothing to snapshot and nothing decoupling this ChangeOrder
    from anything else. This route only records the rejection
    (`status='rejected'` plus a `change_order.rejected` audit log entry
    carrying `{reason}`), it does not otherwise lock the ChangeOrder.

    `ChangeOrderRejectRequest` (`app/schemas/change_order.py`) is a plain
    JSON body, not `multipart/form-data` — unlike `approve`, there is no
    binary signature artifact to submit here.

    `company_id=change_order.company_id`, not `current.company_id`, for the
    audit log entry — see `approve_change_order`'s docstring above for the
    full rationale.
    """
    change_order = await _get_change_order_or_404(current, change_order_id)
    _require_change_order_pending(change_order)

    change_order.status = "rejected"
    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    await write_audit_log(
        current.session,
        company_id=change_order.company_id,
        actor_id=current.user.id,
        action="change_order.rejected",
        entity_type="change_order",
        entity_id=change_order.id,
        metadata={"reason": payload.reason},
    )

    return ChangeOrderResponse.model_validate(change_order)
