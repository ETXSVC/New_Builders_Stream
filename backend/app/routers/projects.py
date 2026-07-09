import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.core.deps import CurrentUser, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import Phase, Project, Task
from app.models.project import VALID_STATUSES
from app.schemas.project import (
    ProjectClientDashboardResponse,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectPatchRequest,
    ProjectResponse,
    ProjectStatusUpdateRequest,
)
from app.services.audit import write_audit_log
from app.services.project_transitions import is_legal_transition

router = APIRouter(prefix="/projects", tags=["projects"])

# docs/07-security-compliance.md Section 2's RBAC matrix: Admin/PM get "Full
# CRUD" on Project Management, so create/general-field-edit stay restricted
# to those two roles — same two roles Task 1.5/1.12's PATCH /leads uses,
# matching design decision #3 (PATCH /projects/{id} is an Admin/PM-only
# extension beyond the literal API spec route table).
_WRITE_ROLES = ("admin", "project_manager")

# Every role that has *some* read access to Project Management per the
# matrix: Admin/PM (full), Field Crew (assigned only), Accountant (read,
# financial-fields-only — Phase 1 has no financial fields on `projects` yet,
# so this collapses to plain read), Client (sanitized dashboard only, and
# only via GET /projects/{id} — see _GET_ROLES below, which is a superset of
# this tuple).
_LIST_ROLES = ("admin", "project_manager", "accountant", "field_crew")

# GET /projects/{id} is the one route every read-capable role can hit — the
# API spec (Section 4) documents exactly one GET /projects/{id} route and
# design decision #8 makes it double as the client's sanitized dashboard
# (response SHAPE differs by role, not access). `client` is deliberately
# absent from _LIST_ROLES: the API spec only ever documents GET
# /projects/{id} for clients, never a list route, so GET /projects blocks
# `client` with a 403 (the more literal reading of "there's no list route
# for clients at all" — see Task 1.12's spec and this router's tests).
_GET_ROLES = (*_LIST_ROLES, "client")


def _with_field_crew_scope(query, current: CurrentUser):
    """Field crew's assigned-only visibility predicate: a project qualifies
    if ANY of its tasks, through ANY of its phases, is assigned to this
    user. Shared by list_projects and _get_project_or_404 so the two
    enforcement points can't drift apart — this is RBAC-enforcement logic
    (docs/07-security-compliance.md Section 2: Field Crew gets "Read
    assigned" for Project Management, an unqualified statement covering
    both list and single-item read), so a future change to the predicate
    (task status filtering, reassignment handling, etc.) only needs to
    happen once. Expressed as a correlated EXISTS rather than a JOIN so a
    field_crew user with multiple matching tasks on the same project
    doesn't get duplicate rows for it."""
    assigned_task_exists = (
        select(Task.id)
        .join(Phase, Phase.id == Task.phase_id)
        .where(Phase.project_id == Project.id, Task.assignee_id == current.user.id)
        .exists()
    )
    return query.where(assigned_task_exists)


async def _get_project_or_404(current: CurrentUser, project_id: uuid.UUID) -> Project:
    """Shared existence/tenant/RBAC-scope check, same pattern as leads.py's
    _get_lead_or_404 — RLS makes another tenant's project invisible, so this
    404 covers "doesn't exist" and "exists but isn't yours" identically,
    intentionally indistinguishable from outside.

    Also enforces field_crew's assigned-only read scope (_with_field_crew_scope)
    here, not just on the list route — a field_crew user requesting a
    project they have no task on gets the same 404 as a genuinely
    nonexistent/cross-tenant one, for the same information-disclosure
    reason every other 404 in this codebase is existence-indistinguishable.
    Folded into the initial query (rather than a separate EXISTS round
    trip after fetching the row) so this is one query, not two."""
    query = select(Project).where(Project.id == project_id)
    if current.role == "field_crew":
        query = _with_field_crew_scope(query, current)

    result = await current.session.execute(query)
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    return project


async def _client_dashboard_response(
    current: CurrentUser, project: Project
) -> ProjectClientDashboardResponse:
    """Constructs the sanitized `client`-role shape (design decision #8's
    correction). `phase_count`/`task_count`/`completed_task_count` are NOT
    ORM columns — see ProjectClientDashboardResponse's docstring for why
    this MUST be built via explicit COUNT queries and explicit field
    assignment, never `model_validate()` on the bare `project` row. Both
    counts are scoped to this project_id and rely on `phases`/`tasks`'
    own tenant_isolation RLS policies for tenant safety, same as every
    other query in this router."""
    phase_count = await current.session.scalar(
        select(func.count()).select_from(Phase).where(Phase.project_id == project.id)
    )
    task_count = await current.session.scalar(
        select(func.count())
        .select_from(Task)
        .join(Phase, Phase.id == Task.phase_id)
        .where(Phase.project_id == project.id)
    )
    completed_task_count = await current.session.scalar(
        select(func.count())
        .select_from(Task)
        .join(Phase, Phase.id == Task.phase_id)
        .where(Phase.project_id == project.id, Task.status == "done")
    )

    return ProjectClientDashboardResponse(
        id=project.id,
        name=project.name,
        status=project.status,
        site_address=project.site_address,
        projected_start_date=project.projected_start_date,
        phase_count=phase_count or 0,
        task_count=task_count or 0,
        completed_task_count=completed_task_count or 0,
    )


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
) -> ProjectResponse:
    project = Project(
        company_id=current.company_id,
        lead_id=payload.lead_id,
        name=payload.name,
        site_address=payload.site_address,
        projected_start_date=payload.projected_start_date,
        status="draft",
    )
    current.session.add(project)
    await current.session.flush()
    # No audit_log entry here, unlike Lead's create_lead: Lead.created's
    # audit entry exists because a Lead's creation is itself the
    # CRM-pipeline-entry event worth a durable "who/when" record independent
    # of the row (docs/07-security-compliance.md Section 5 calls out
    # "Project status changes", not Project creation, as needing an audit
    # trail). A manually-created Project already carries created_at/
    # company_id/who-created-it isn't tracked on the row itself the way
    # CommunicationLog's author_id is, but Project creation isn't in Section
    # 5's enumerated list of financially/legally significant state changes
    # the way status transitions are (that's Task 1.13's
    # `project.status_changed` entry) — so no audit_log entry is written on
    # create here, consistent with the security doc's own scope for what
    # needs an audit trail.
    # No explicit commit here — get_current_user (design decision #8/
    # Inherited Invariant #4) commits current.session once, after this
    # handler returns.

    return ProjectResponse.model_validate(project)


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    current: CurrentUser = Depends(require_role(*_LIST_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ProjectListResponse:
    if status_filter is not None and status_filter not in VALID_STATUSES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"status must be one of {VALID_STATUSES}"
        )

    # No explicit company_id filter — same pattern as list_leads: the
    # tenant_isolation RLS policy (0004 migration) already scopes every row
    # this query can see to the caller's active tenant (and descendants).
    query = select(Project)

    if current.role == "field_crew":
        query = _with_field_crew_scope(query, current)
    # admin/project_manager: full company-scoped list (RLS-scoped, no extra
    # filter). accountant: same — Phase 1 has no financial fields on
    # `projects` yet (design decision #8), so "financial-fields-only" read
    # collapses to plain read access for now; the field-level restriction
    # has nothing to actually restrict until Phase 2 adds financial columns.

    if status_filter is not None:
        query = query.where(Project.status == status_filter)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Project.created_at,
        id_col=Project.id,
        cursor=cursor,
        limit=limit,
    )

    return ProjectListResponse(
        items=[ProjectResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/{project_id}", response_model=ProjectResponse | ProjectClientDashboardResponse)
async def get_project(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_GET_ROLES)),
) -> ProjectResponse | ProjectClientDashboardResponse:
    project = await _get_project_or_404(current, project_id)

    # Role-based response SHAPE, per design decision #8: `client` gets the
    # sanitized dashboard (no `lead_id`/`company_id`, plus computed progress
    # counts); every other read-capable role gets the full ProjectResponse.
    # (field_crew's ADDITIONAL restriction — which specific projects they
    # can reach at all — is enforced upstream in _get_project_or_404, not
    # here; this branch is shape-only.)
    if current.role == "client":
        return await _client_dashboard_response(current, project)

    return ProjectResponse.model_validate(project)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def patch_project(
    project_id: uuid.UUID,
    payload: ProjectPatchRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
) -> ProjectResponse:
    project = await _get_project_or_404(current, project_id)

    update_fields = payload.model_dump(exclude_unset=True)
    for field_name, value in update_fields.items():
        setattr(project, field_name, value)

    # updated_at bumps automatically via UpdatedAtMixin's onupdate=utcnow the
    # moment any setattr() above makes this row dirty and it gets flushed.
    await current.session.flush()
    # No explicit commit here — get_current_user (Inherited Invariant #4)
    # commits current.session once, after this handler returns.

    return ProjectResponse.model_validate(project)


@router.patch("/{project_id}/status", response_model=ProjectResponse)
async def update_project_status(
    project_id: uuid.UUID,
    payload: ProjectStatusUpdateRequest,
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
) -> ProjectResponse:
    """Task 1.13: the Project status state machine, entirely separate from
    `patch_project` above (design decision #3 — Project splits field edits
    and status transitions into two routes/schemas, unlike Lead's combined
    `PATCH /leads/{id}`). Reuses `_get_project_or_404` for the existence/
    tenant check; field_crew can never reach this route at all (`_WRITE_ROLES`
    is admin/project_manager only), so the field_crew-scoping half of that
    helper is inert here — it's reused purely to avoid duplicating the
    existence/tenant-404 check, not because field_crew's assigned-only
    visibility is relevant to this route."""
    project = await _get_project_or_404(current, project_id)

    previous_status = project.status
    requested_status = payload.status
    # A resubmission of the project's current status is a no-op, not a
    # transition — it isn't modeled in project_transitions.PROJECT_TRANSITIONS
    # (no self-loops), same precedent as Lead's PATCH /leads/{id}.
    status_changing = requested_status != previous_status

    # Validate the transition BEFORE touching the ORM object at all — same
    # atomicity discipline as Lead's update_lead: raising here, before any
    # setattr, guarantees nothing from this request is staged for the
    # eventual single commit get_current_user performs (Inherited Invariant #4).
    if status_changing and not is_legal_transition(previous_status, requested_status):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Illegal project status transition: {previous_status} -> {requested_status}",
        )

    if status_changing:
        # Change Orders business rule (Functional Requirements Section 3:
        # "A Project cannot move to Completed while it has open (non-approved)
        # Change Orders") is NOT enforced here — `change_orders` doesn't exist
        # in Phase 1 (out of scope). See project_transitions.py's module
        # docstring for the full note on what needs to be added here, layered
        # on top of (not replacing) the is_legal_transition() check above,
        # once Change Orders ships in Phase 2. Do not forget this.
        project.status = requested_status

    # updated_at bumps automatically via UpdatedAtMixin's onupdate=utcnow the
    # moment the setattr above makes this row dirty and it gets flushed.
    await current.session.flush()

    if status_changing:
        await write_audit_log(
            current.session,
            company_id=current.company_id,
            actor_id=current.user.id,
            action="project.status_changed",
            entity_type="project",
            entity_id=project.id,
            metadata={"from": previous_status, "to": requested_status, "reason": payload.reason},
        )

    return ProjectResponse.model_validate(project)
