import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.models import Phase, Project, Task
from app.routers.projects import _get_project_or_404
from app.schemas.phase import (
    PhaseCreateRequest,
    PhaseListResponse,
    PhaseResponse,
    PhaseWithTasksResponse,
)
from app.schemas.task import (
    MyTaskListResponse,
    MyTaskResponse,
    TaskCreateRequest,
    TaskResponse,
    TaskUpdateRequest,
)

# Duplicated from projects.py rather than imported, matching leads.py's own
# precedent for this identical ("admin", "project_manager") tuple
# (_LEAD_ROLES) — this codebase's established convention is that each
# router owns its role constants rather than reaching into another
# router's private (underscore-prefixed) namespace for a value this small.
_WRITE_ROLES = ("admin", "project_manager")

# Same read set projects.py's _LIST_ROLES grants (each router owns its role
# constants — established convention, see _WRITE_ROLES above). client is
# excluded: their project view is the counts-only dashboard shape.
_READ_ROLES = ("admin", "project_manager", "accountant", "field_crew")

# Task 1.14: Phases and Tasks. Deliberately its OWN file rather than more
# additions to projects.py (already the largest router at ~310 lines before
# this task, and the field_crew ownership+field-restriction logic below is
# the most involved RBAC shape in the codebase so far — splitting it out
# keeps that complexity legible instead of burying it at the bottom of an
# already-long file). The routes below don't share a single URL prefix
# (phase/task CREATE nest under /projects/{project_id}/..., but PATCH
# /tasks/{id} is its own top-level resource — a Task isn't addressed via a
# project_id path param), so this router intentionally has no APIRouter
# prefix and each route spells out its own full path, same as how
# projects.py itself would look if it mixed /projects and /something-else
# routes.
router = APIRouter(tags=["phases", "tasks"])


async def _get_task_or_404(current: CurrentUser, task_id: uuid.UUID) -> Task:
    """Own existence/tenant/RBAC-scope check for PATCH /tasks/{id}. A Task
    isn't reached via a project_id path param (unlike phase/task creation
    below), so it needs its own lookup — same shape as projects.py's
    _get_project_or_404: RLS makes another tenant's task invisible, so this
    404 covers "doesn't exist" and "exists but isn't yours" identically.

    Also folds in field_crew's assigned-only visibility here, not just as a
    later check in the route handler: docs/07-security-compliance.md
    Section 2's RBAC matrix gives field_crew "read assigned" for Project
    Management, unqualified — a task NOT assigned to them is invisible to
    them, period, not merely off-limits to edit. So a field_crew caller
    patching a task that isn't theirs gets the exact same 404 as a
    genuinely nonexistent task.

    This is deliberately different from the field-restriction check the
    route handler applies afterward (field_crew attempting to set
    assignee_id on a task that IS theirs) — that's a 403, not a 404,
    because at that point the task IS visible to them (it passed this
    function), and the rejection is about which FIELDS they're allowed to
    touch, not whether they can see the row at all. Same "404 for anything
    you can't see, 403 for role-based restrictions on things you CAN see"
    split projects.py's own docstrings already establish for
    _get_project_or_404 vs. the RBAC-blocked write routes."""
    query = select(Task).where(Task.id == task_id)
    if current.role == "field_crew":
        query = query.where(Task.assignee_id == current.user.id)

    result = await current.session.execute(query)
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Task not found")

    return task


@router.post(
    "/projects/{project_id}/phases",
    response_model=PhaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_phase(
    project_id: uuid.UUID,
    payload: PhaseCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> PhaseResponse:
    # Reuses projects.py's _get_project_or_404 purely to avoid duplicating
    # the existence/tenant-404 check — same reuse rationale as Task 1.13's
    # update_project_status (itself modeled on Task 1.7's _get_lead_or_404
    # reuse for communication logs). field_crew can never reach this route
    # at all (_WRITE_ROLES is admin/project_manager only), so the
    # field_crew-scoping half of that helper is inert here.
    project = await _get_project_or_404(current, project_id)

    # `company_id=project.company_id`, not `current.company_id`: a parent
    # company's session can legitimately act on a descendant branch's
    # Project without switching `X-Tenant-ID` to that branch first (RLS's
    # `get_all_descendant_ids()` grant already makes the descendant's rows
    # visible/writable). Using `current.company_id` here would silently
    # stamp this Phase with the PARENT's id instead of the Project's own —
    # a session later scoped directly to the descendant branch would then
    # find its own Project's Phase invisible under RLS. Same bug class
    # already fixed in change_orders.py/expenses.py/subcontractor_assignments.py
    # and projects.py's upload_document/create_daily_log, per the
    # post-Phase-2 audit of this exact pattern — this route was missed by
    # that audit.
    phase = Phase(
        project_id=project_id,
        company_id=project.company_id,
        name=payload.name,
        sequence=payload.sequence,
    )
    current.session.add(phase)
    await current.session.flush()
    # No audit_log entry: same reasoning as create_project's docstring in
    # projects.py — Phase creation isn't in docs/07-security-compliance.md
    # Section 5's enumerated list of financially/legally significant state
    # changes (that list is status transitions: Lead, Project), so no
    # audit trail is written here, consistent with that doc's own scope.
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns.

    return PhaseResponse.model_validate(phase)


@router.post(
    "/projects/{project_id}/tasks",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_task(
    project_id: uuid.UUID,
    payload: TaskCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> TaskResponse:
    project = await _get_project_or_404(current, project_id)

    # phase_id must belong to the SAME project as the path's project_id —
    # an application-layer check, same pattern as Phase 0's
    # create_child_company's `company_id != current.company_id` guard
    # (nothing in the schema or a DB constraint can express "this FK value
    # must also match that OTHER FK's parent" on its own). Folding the
    # project_id filter directly into the WHERE clause, rather than
    # fetching the phase by id alone and comparing .project_id in Python,
    # means a cross-project phase_id and a genuinely nonexistent phase_id
    # are indistinguishable from this query's perspective — deliberately,
    # since both are "not a valid phase for this request" the same way.
    #
    # Status code choice: 422, not 404. This isn't about whether the
    # caller can SEE the phase (RLS already scopes phase_id, like every
    # other query in this router, to the caller's own tenant — a
    # cross-tenant phase_id would be invisible regardless) — it's that the
    # VALUE supplied is semantically invalid in the context of THIS
    # request's project_id path param, the same category of error this
    # router's own list_projects uses 422 for (`status` filter value not
    # legal given context). A 404 here would conflate "phase doesn't
    # exist/isn't yours" with "phase exists, is yours, just isn't part of
    # this project" — two different failure modes worth distinguishing for
    # the caller, unlike the existence-vs-tenant case 404s are used for
    # everywhere else in this codebase.
    phase_result = await current.session.execute(
        select(Phase).where(Phase.id == payload.phase_id, Phase.project_id == project_id)
    )
    phase = phase_result.scalar_one_or_none()
    if phase is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "phase_id must belong to the project in the URL",
        )

    # `company_id=project.company_id`, not `current.company_id` — same
    # parent/descendant-branch reasoning as create_phase above, and the
    # same bug class already fixed in this codebase's other nested-resource-
    # creation routes.
    task = Task(
        phase_id=payload.phase_id,
        company_id=project.company_id,
        name=payload.name,
        due_date=payload.due_date,
        assignee_id=payload.assignee_id,
        status="open",
    )
    current.session.add(task)
    await current.session.flush()
    # No audit_log entry — same reasoning as create_phase above.

    return TaskResponse.model_validate(task)


@router.get("/projects/{project_id}/phases", response_model=PhaseListResponse)
async def list_phases(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> PhaseListResponse:
    """Phases ordered by (sequence, id), each with its tasks nested,
    ordered by (created_at, id). _get_project_or_404 covers existence,
    tenant scope, and field_crew's assigned-projects-only visibility, same
    as the create routes above."""
    project = await _get_project_or_404(current, project_id)

    phase_result = await current.session.execute(
        select(Phase).where(Phase.project_id == project.id).order_by(Phase.sequence, Phase.id)
    )
    phases = phase_result.scalars().all()

    task_result = await current.session.execute(
        select(Task)
        .join(Phase, Task.phase_id == Phase.id)
        .where(Phase.project_id == project.id)
        .order_by(Task.created_at, Task.id)
    )
    tasks_by_phase: dict[uuid.UUID, list[Task]] = {}
    for task in task_result.scalars().all():
        tasks_by_phase.setdefault(task.phase_id, []).append(task)

    return PhaseListResponse(
        items=[
            PhaseWithTasksResponse(
                id=phase.id,
                project_id=phase.project_id,
                company_id=phase.company_id,
                name=phase.name,
                sequence=phase.sequence,
                tasks=[TaskResponse.model_validate(t) for t in tasks_by_phase.get(phase.id, [])],
            )
            for phase in phases
        ]
    )


@router.get("/tasks", response_model=MyTaskListResponse)
async def list_my_tasks(
    assignee: str = Query(...),
    current: CurrentUser = Depends(require_role(*_READ_ROLES)),
) -> MyTaskListResponse:
    """Cross-project list of the CURRENT USER's assigned tasks. `assignee`
    accepts only the literal "me" — there is no legitimate frontend need to
    list another user's assignments today (422 otherwise, YAGNI). Ordered
    by due date (nulls last) then creation, capped at 200."""
    if assignee != "me":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, 'assignee only supports the value "me"'
        )

    result = await current.session.execute(
        select(Task, Project.id, Project.name)
        .join(Phase, Task.phase_id == Phase.id)
        .join(Project, Phase.project_id == Project.id)
        .where(Task.assignee_id == current.user.id)
        .order_by(Task.due_date.asc().nulls_last(), Task.created_at, Task.id)
        .limit(200)
    )
    return MyTaskListResponse(
        items=[
            MyTaskResponse(
                id=task.id,
                phase_id=task.phase_id,
                company_id=task.company_id,
                name=task.name,
                assignee_id=task.assignee_id,
                due_date=task.due_date,
                status=task.status,
                created_at=task.created_at,
                project_id=project_id,
                project_name=project_name,
            )
            for task, project_id, project_name in result.all()
        ]
    )


@router.patch("/tasks/{task_id}", response_model=TaskResponse)
async def patch_task(
    task_id: uuid.UUID,
    payload: TaskUpdateRequest,
    current: CurrentUser = Depends(require_role("admin", "project_manager", "field_crew")),
    _ro: None = Depends(block_if_read_only),
) -> TaskResponse:
    """The genuinely new RBAC shape Task 1.14 calls out: role AND ownership
    AND a field-level restriction, combined, not just a role gate.

    - admin/project_manager: unrestricted — can set any field this schema
      exposes (status, assignee_id) on any task in their tenant.
    - field_crew: can set `status` ONLY, and ONLY on a task assigned to
      them (`assignee_id == current.user.id`).

    Ownership is enforced by _get_task_or_404 (404 if the task isn't
    theirs — they can't see it at all, so it doesn't "exist" from their
    point of view, matching projects.py's _get_project_or_404 precedent).

    The field-level restriction below is enforced with an explicit 403,
    not a silent drop of disallowed fields. Alternative considered: quietly
    ignore any field in the payload a field_crew caller isn't allowed to
    touch (the same shape ProjectPatchRequest uses for `status`, which
    doesn't exist on that schema at all, so extra fields are dropped by
    Pydantic itself before the router ever sees them). Rejected here
    because TaskUpdateRequest's `assignee_id` field is real and admin/PM
    ARE allowed to set it — the restriction is role-conditional, not
    schema-level, so silently ignoring it would mean a field_crew caller
    who explicitly tried to reassign a task gets a 200 that looks
    successful but did something other than what they asked. An explicit
    403 makes the privilege boundary visible instead of silently
    swallowing the attempt (same "professional honesty"/fail-loud
    instinct behind this codebase's other explicit-rejection choices,
    e.g. the illegal-status-transition 409s in leads.py/projects.py rather
    than silently no-op'ing an illegal transition).
    """
    task = await _get_task_or_404(current, task_id)

    update_fields = payload.model_dump(exclude_unset=True)

    if current.role == "field_crew":
        # Keyed on which fields are PRESENT in the payload, not on whether
        # their values differ from the task's current row — a field_crew
        # caller who sends assignee_id set to its own unchanged current
        # value still gets rejected below. Don't "simplify" this into a
        # value-comparison check: that would silently readmit the exact
        # touch-without-permission gap this 403 exists to close.
        disallowed_fields = set(update_fields) - {"status"}
        if disallowed_fields:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"field_crew may only update 'status'; not permitted to update: "
                f"{sorted(disallowed_fields)}",
            )

    for field_name, value in update_fields.items():
        setattr(task, field_name, value)

    await current.session.flush()
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns.

    return TaskResponse.model_validate(task)
