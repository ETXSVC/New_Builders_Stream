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
)

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


async def _get_project_or_404(current: CurrentUser, project_id: uuid.UUID) -> Project:
    """Shared existence/tenant check, same pattern as leads.py's
    _get_lead_or_404 — RLS makes another tenant's project invisible, so this
    404 covers both "doesn't exist" and "exists but isn't yours",
    intentionally indistinguishable from outside."""
    result = await current.session.execute(select(Project).where(Project.id == project_id))
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
        # docs/07-security-compliance.md Section 2's RBAC matrix: Field Crew
        # gets "Read assigned" only, not a blanket company-scoped list — a
        # project qualifies if ANY of its tasks, through ANY of its phases,
        # is assigned to this user. Expressed as a correlated EXISTS rather
        # than a JOIN so a field_crew user with multiple matching tasks on
        # the same project doesn't get duplicate rows for it.
        assigned_task_exists = (
            select(Task.id)
            .join(Phase, Phase.id == Task.phase_id)
            .where(Phase.project_id == Project.id, Task.assignee_id == current.user.id)
            .exists()
        )
        query = query.where(assigned_task_exists)
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


@router.get("/{project_id}", response_model=None)
async def get_project(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_GET_ROLES)),
) -> ProjectResponse | ProjectClientDashboardResponse:
    project = await _get_project_or_404(current, project_id)

    # Role-based response SHAPE, per design decision #8: `client` gets the
    # sanitized dashboard (no `lead_id`/`company_id`, plus computed progress
    # counts); every other read-capable role gets the full ProjectResponse.
    # This is the one route every role in _GET_ROLES can reach — the RBAC
    # distinction here is about shape, not which projects within the
    # caller's own tenant are visible (see this module's _GET_ROLES comment).
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
