"""Task 3.11: `POST/GET /projects/{project_id}/subcontractor-assignments`.

Deliberately its OWN file rather than more additions to `subcontractors.py`
or `projects.py`, following `change_orders.py`'s own precedent (see that
router's module docstring) exactly: `SubcontractorAssignment` is
conceptually project-nested (both routes live under
`/projects/{project_id}/subcontractor-assignments`) but is a distinct
resource from both `Project` and `Subcontractor`, with real business logic
of its own (the Admin-override-required expired-compliance rule below) that
doesn't belong bolted onto either of those routers' entity-focused files.

Reuses `_get_project_or_404` (`app/routers/projects.py`) and
`_get_subcontractor_or_404` (`app/routers/subcontractors.py`) via
cross-router import rather than redefining either — the exact same pattern
`change_orders.py` uses for `_get_project_or_404`.
"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.core.tier_gating import require_module
from app.models import ComplianceDocument, SubcontractorAssignment
from app.models.compliance_document import VALID_DOC_TYPES
from app.routers.projects import _get_project_or_404
from app.routers.subcontractors import _get_subcontractor_or_404
from app.schemas.subcontractor_assignment import (
    SubcontractorAssignmentCreateRequest,
    SubcontractorAssignmentListResponse,
    SubcontractorAssignmentResponse,
)
from app.services.audit import write_audit_log

# No APIRouter prefix, same reasoning change_orders.py gives for itself: both
# routes below nest under /projects/{project_id}/subcontractor-assignments,
# and there is no top-level /subcontractor-assignments/{id}/... route in this
# plan (unlike ChangeOrder's send-for-signature/approve/reject), but the
# convention of each route spelling out its own full path string is kept for
# consistency with the rest of this codebase's project-nested-but-own-file
# routers.
router = APIRouter(tags=["subcontractor_assignments"])

# docs/07-security-compliance.md Section 2's RBAC matrix, Compliance row:
# Admin = Full CRUD (including override), Project Manager = Read + assign
# only (assign, but NEVER override — see create_subcontractor_assignment's
# own docstring), Accountant = Read only. Field Crew and Client have no
# documented grant on this row at all, so both are absent from both tuples
# below. Own tuples, not a reuse of subcontractors.py's/compliance.py's own
# _WRITE_ROLES/_READ_ROLES — same "two routers' RBAC shouldn't drift
# silently if one changes without the other" reasoning compliance.py's own
# _READ_ROLES comment gives.
_WRITE_ROLES = ("admin", "project_manager")
_READ_ROLES = ("admin", "project_manager", "accountant")


async def _has_expired_compliance_document(current: CurrentUser, subcontractor_id: uuid.UUID) -> bool:
    """True iff `subcontractor_id` has at least one `ComplianceDocument` row
    with `doc_type` in `VALID_DOC_TYPES` (imported from
    `app/models/compliance_document.py`, not redefined here) and
    `expires_on < today`. The `doc_type IN VALID_DOC_TYPES` filter is
    strictly redundant against the DB's own CHECK constraint
    (`ck_compliance_documents_doc_type` — no row can exist with any other
    `doc_type`), but it's kept explicit to mirror the design spec's own
    wording for this check verbatim and to stay correct even if
    `VALID_DOC_TYPES` narrows in a future migration without a matching
    backfill.

    Deliberately checks for an EXPIRED document specifically, not an ABSENT
    one — a subcontractor with zero `compliance_documents` rows at all does
    NOT block assignment (the design spec's own explicitly-resolved
    judgment call: "missing" and "expired" are different states, and only
    the latter gates this flow). `date.today()`, not a stored/cached value,
    matching `get_compliance_dashboard`'s own precedent
    (`app/routers/compliance.py`) for computing "today" live rather than
    trusting anything precomputed.

    `.limit(1)` + `scalar_one_or_none()`: this is an existence check, not a
    listing — there's no reason to fetch every expired row just to learn
    whether at least one exists.
    """
    result = await current.session.execute(
        select(ComplianceDocument.id)
        .where(
            ComplianceDocument.subcontractor_id == subcontractor_id,
            ComplianceDocument.doc_type.in_(VALID_DOC_TYPES),
            ComplianceDocument.expires_on < date.today(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


@router.post(
    "/projects/{project_id}/subcontractor-assignments",
    response_model=SubcontractorAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_subcontractor_assignment(
    project_id: uuid.UUID,
    payload: SubcontractorAssignmentCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
    _tier: CurrentUser = Depends(require_module("compliance")),
) -> SubcontractorAssignmentResponse:
    """Task 3.11: the Admin-override-required expired-compliance rule.

    Ordering: `_get_project_or_404` first, then `_get_subcontractor_or_404`
    on `payload.subcontractor_id` — a cross-tenant/nonexistent id in either
    the path or the body 404s before any semantic (expired-document)
    validation runs, same "existence/tenant check before business-rule
    check" ordering `create_change_order`'s own docstring
    (`app/routers/change_orders.py`) establishes. Without the second
    `_get_subcontractor_or_404` call, a cross-tenant `subcontractor_id`
    would silently pass through to `_has_expired_compliance_document`
    (whose query is itself RLS-scoped and would just find nothing), letting
    a nonexistent/invisible subcontractor be "assigned" freely instead of
    404ing — this is why that helper is imported and reused here rather
    than skipped.

    Both helpers are independently RLS-scoped, which is not by itself
    sufficient: a PARENT company's session (unswitched, no `X-Tenant-ID`)
    has simultaneous RLS visibility into every descendant branch via
    `get_all_descendant_ids()`, so `project` and `subcontractor` can each
    individually pass their own 404 check while belonging to two different
    SIBLING branches. Without an explicit same-company check between them,
    such a session could cross-wire a Branch B subcontractor into a Branch
    A project — a dangling cross-tenant reference invisible to any session
    scoped narrowly to just one of the two branches. The
    `subcontractor.company_id != project.company_id` check below closes
    this, 404ing with the same "not found" message `_get_subcontractor_or_404`
    itself would use, consistent with this codebase's "doesn't exist" and
    "exists but isn't yours" being intentionally indistinguishable from
    outside.

    The business-rule branching below is a role-conditional check INSIDE
    the handler body, not encoded as two separate `require_role` gates —
    same shape `list_estimates`'s own `if current.role == "client":
    query = query.where(...)` uses (`app/routers/estimates.py`), and the
    same "business-rule check lives in the router" pattern
    `update_project_status`'s Change-Order-block check establishes
    (`app/routers/projects.py`, Task 2.23): `_WRITE_ROLES` alone
    (admin/project_manager) can't express "PM may assign a COMPLIANT
    subcontractor but never an expired one, while Admin may do either, and
    only with a supplied reason for the expired case" — that's inherently a
    per-request, data-dependent decision, not a static role gate.

    Four cases, matching the design spec's own Section 5 testing note
    exactly:
      - PM, expired: 409, unconditionally — a PM can NEVER override, even
        if `override_reason` was somehow supplied in the request body. The
        409 branch below is checked before `override_reason` is ever
        inspected, so there's no code path where a PM's supplied reason
        changes the outcome.
      - PM, compliant (or zero documents): 201, no `override_reason` needed
        or stored.
      - Admin, expired, non-blank `override_reason`: 201, WITH an
        `audit_log` entry and `override_reason` persisted onto the new row.
      - Admin, expired, blank/omitted `override_reason`: 422. `""` (empty
        string) and whitespace-only strings both count as blank here, not
        just `None` — `not override_reason or not override_reason.strip()`
        catches all three, since a caller who explicitly sends `""` hasn't
        actually stated a reason any more than one who omitted the field
        entirely.
      - Either role, compliant (or zero documents): 201, `override_reason`
        is ignored even if supplied — forced to `None` before the row is
        built, matching `SubcontractorAssignment.override_reason`'s own
        docstring ("populated only when the assignment overrides an
        expired-compliance block", `app/models/subcontractor_assignment.py`).
        A caller who passes a reason for a compliant subcontractor doesn't
        get it silently persisted onto a row where it would be misleading.

    `company_id=project.company_id`, not `current.company_id`, on BOTH the
    new `SubcontractorAssignment` row and the audit log entry below — same
    rationale as `create_change_order`'s own fix
    (`app/routers/change_orders.py`) and `update_project_status`'s audit
    entry (`app/routers/projects.py`, Task 2.23): a parent company's session
    can legitimately act on a descendant branch's Project without switching
    `X-Tenant-ID` first (`_get_project_or_404` already makes the
    descendant's Project reachable via RLS's `get_all_descendant_ids()`
    grant). Using `current.company_id` here would stamp both rows with the
    PARENT's id instead of the Project's own, making them invisible to a
    session later scoped directly to the descendant branch. Applied here
    from the start, not fixed after the fact, per the post-Phase-2
    follow-up's own lesson.

    No audit_log entry for the non-override paths (PM/Admin assigning a
    compliant subcontractor): only the override itself is the
    audit-worthy action per this task's own spec — same "not every
    create needs an audit entry" precedent `create_subcontractor`/
    `upload_compliance_document` (`app/routers/subcontractors.py`) already
    establish.
    """
    project = await _get_project_or_404(current, project_id)
    subcontractor = await _get_subcontractor_or_404(current, payload.subcontractor_id)
    if subcontractor.company_id != project.company_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subcontractor not found")

    has_expired = await _has_expired_compliance_document(current, subcontractor.id)

    override_reason = payload.override_reason
    if has_expired:
        if current.role == "project_manager":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Subcontractor has expired compliance document(s); only an "
                "Admin can override this assignment",
            )
        # current.role == "admin" (the only other _WRITE_ROLES member).
        if not override_reason or not override_reason.strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "override_reason is required to assign a Subcontractor with "
                "expired compliance document(s)",
            )
    else:
        # No expired document: override_reason is ignored/not required even
        # if supplied — see this function's own docstring for why it's
        # forced to None rather than persisted as-is.
        override_reason = None

    assignment = SubcontractorAssignment(
        project_id=project.id,
        subcontractor_id=subcontractor.id,
        company_id=project.company_id,
        assigned_by=current.user.id,
        override_reason=override_reason,
    )
    current.session.add(assignment)
    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    if has_expired:
        await write_audit_log(
            current.session,
            company_id=project.company_id,
            actor_id=current.user.id,
            action="subcontractor.assigned_with_expired_docs",
            entity_type="subcontractor_assignment",
            entity_id=assignment.id,
            metadata={"reason": override_reason, "subcontractor_id": str(subcontractor.id)},
        )

    return SubcontractorAssignmentResponse.model_validate(assignment)


@router.get(
    "/projects/{project_id}/subcontractor-assignments",
    response_model=SubcontractorAssignmentListResponse,
)
async def list_subcontractor_assignments(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> SubcontractorAssignmentListResponse:
    """Task 3.11. `_get_project_or_404` first, then `paginate()` scoped to
    this project — copies `list_change_orders`'s exact structure (imports,
    `Query` params, the `paginate()` call itself with `created_at_col`/
    `id_col`), same as every other project-nested list route in this
    codebase.

    `_READ_ROLES` (admin, project_manager, accountant) — no `client` here,
    unlike `list_change_orders`'s Task 2.22 addition: there is no
    client-facing action to take on a `SubcontractorAssignment` anywhere in
    this plan, so there's no equivalent reason to grant `client` read
    access the way ChangeOrder's pending-approval workflow required.

    No explicit `company_id` filter: the tenant_isolation RLS policy on
    `subcontractor_assignments` already scopes every row this query can see
    to the caller's active tenant, same pattern every other list route in
    this codebase relies on.
    """
    project = await _get_project_or_404(current, project_id)

    query = select(SubcontractorAssignment).where(SubcontractorAssignment.project_id == project.id)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=SubcontractorAssignment.created_at,
        id_col=SubcontractorAssignment.id,
        cursor=cursor,
        limit=limit,
    )

    return SubcontractorAssignmentListResponse(
        items=[SubcontractorAssignmentResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )
