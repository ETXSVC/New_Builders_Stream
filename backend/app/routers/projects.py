import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.config import settings
from app.core.deps import CurrentUser, block_if_read_only, require_role
from app.core.pagination import DEFAULT_LIMIT, MAX_LIMIT, paginate
from app.models import ChangeOrder, DailyLog, Document, Phase, Project, Task
from app.models.project import VALID_STATUSES
from app.schemas.daily_log import DailyLogCreateRequest, DailyLogListResponse, DailyLogResponse
from app.schemas.document import DocumentListResponse, DocumentResponse
from app.schemas.project import (
    ProjectClientDashboardListResponse,
    ProjectClientDashboardResponse,
    ProjectCreateRequest,
    ProjectListResponse,
    ProjectPatchRequest,
    ProjectResponse,
    ProjectStatusUpdateRequest,
)
from app.services.audit import write_audit_log
from app.services.document_storage import InvalidFileNameError, validate_file_name, write_document_file
from app.services.project_transitions import is_legal_transition

router = APIRouter(prefix="/projects", tags=["projects"])

# docs/07-security-compliance.md Section 2's RBAC matrix: Admin/PM get "Full
# CRUD" on Project Management, so create/general-field-edit stay restricted
# to those two roles — same two roles Task 1.5/1.12's PATCH /leads uses,
# matching design decision #3 (PATCH /projects/{id} is an Admin/PM-only
# extension beyond the literal API spec route table).
_WRITE_ROLES = ("admin", "project_manager")

# Every role that gets the FULL ProjectResponse shape on reads per the
# matrix: Admin/PM (full), Field Crew (assigned only), Accountant (read,
# financial-fields-only — Phase 1 has no financial fields on `projects` yet,
# so this collapses to plain read). Client is deliberately absent (see
# _GET_ROLES below, which is a superset of this tuple): every route that
# still uses _LIST_ROLES directly (documents, daily logs) stays closed to
# `client`.
_LIST_ROLES = ("admin", "project_manager", "accountant", "field_crew")

# The roles for GET /projects and GET /projects/{id} — every read-capable
# role including `client`. Design decision #8 makes GET /projects/{id}
# double as the client's sanitized dashboard (response SHAPE differs by
# role, not access), and the CRM+PM frontend spec (Decision 2 item 6)
# extends the same role-based-shape split to GET /projects: without a list
# route, `client` could GET a project by id but had no route that would
# ever tell them the id. This reverses Task 1.12's earlier "client gets
# 403 on the list route" reading — see list_projects and this router's
# tests.
_GET_ROLES = (*_LIST_ROLES, "client")

# Daily Logs are the one Project Management write field_crew has at all
# (docs/07-security-compliance.md Section 2's matrix, literal text: Field
# Crew = "Read assigned + create Daily Logs" — the ONLY write verb granted
# to that role anywhere in the Project Management row). This is
# deliberately a separate tuple from _WRITE_ROLES (admin, project_manager
# only) rather than a reuse: _WRITE_ROLES governs Project/Phase/Task/
# Document creation, none of which field_crew may do, so folding field_crew
# into _WRITE_ROLES would silently over-grant those other routes. Matches
# US-3.3 (functional requirements): "As Field Crew, I can ... submit a
# Daily Log ... for a Project."
_DAILY_LOG_WRITE_ROLES = ("admin", "project_manager", "field_crew")


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
    _ro: None = Depends(block_if_read_only),
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


@router.get("", response_model=ProjectListResponse | ProjectClientDashboardListResponse)
async def list_projects(
    current: CurrentUser = Depends(require_role(*_GET_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ProjectListResponse | ProjectClientDashboardListResponse:
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

    # Role-based response SHAPE, same split get_project already makes:
    # `client` gets the sanitized dashboard shape per item. The per-row
    # _client_dashboard_response COUNT queries are acceptable at list scale
    # (page size is capped at MAX_LIMIT; a client typically has 1-2
    # projects) — an aggregate rewrite is premature here.
    if current.role == "client":
        return ProjectClientDashboardListResponse(
            items=[await _client_dashboard_response(current, row) for row in rows],
            next_cursor=next_cursor,
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
    _ro: None = Depends(block_if_read_only),
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
    _ro: None = Depends(block_if_read_only),
) -> ProjectResponse:
    """Task 1.13: the Project status state machine, entirely separate from
    `patch_project` above (design decision #3 — Project splits field edits
    and status transitions into two routes/schemas, unlike Lead's combined
    `PATCH /leads/{id}`). Reuses `_get_project_or_404` for the existence/
    tenant check; field_crew can never reach this route at all (`_WRITE_ROLES`
    is admin/project_manager only), so the field_crew-scoping half of that
    helper is inert here — it's reused purely to avoid duplicating the
    existence/tenant-404 check, not because field_crew's assigned-only
    visibility is relevant to this route.

    The audit log entry below uses `company_id=project.company_id`, not
    `current.company_id` — same rationale as `upload_document`'s/
    `create_daily_log`'s own fix (this router, above): a parent company's
    session can legitimately transition a descendant branch's Project
    without switching `X-Tenant-ID` first (`_get_project_or_404` already
    makes the descendant's Project reachable via RLS's
    `get_all_descendant_ids()` grant). Using `current.company_id` here
    would record the `project.status_changed` audit entry under the
    PARENT's company instead of the Project's own, making it invisible to
    a session later scoped directly to the descendant branch auditing its
    own Project's history.
    """
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

    # Task 2.23: Change Orders business rule (Functional Requirements
    # Section 3: "A Project cannot move to Completed while it has open
    # (non-approved) Change Orders"), layered on top of (not replacing) the
    # is_legal_transition() check above. Deliberately lives here, not in
    # project_transitions.is_legal_transition() — same "table-driven check
    # vs. business-rule check in the router" split Task 1.18 established for
    # the LEAD_WON event; the transition table stays pure data with no DB
    # queries.
    #
    # Gated on `requested_status == "completed"` ONLY — not on
    # `previous_status`, so this applies uniformly to both
    # `active -> completed` and `suspended -> completed` (and any future
    # edge that also lands on `completed`), per the plan's explicit warning
    # against keying the check off "coming from suspended" specifically.
    #
    # Judgment call on "open (non-approved)": the functional requirement's
    # literal wording is ambiguous between "pending only" and "pending or
    # rejected" (both are, strictly, "non-approved"). Read the query as
    # `status == "pending"` ONLY — a `rejected` Change Order is a resolved/
    # closed item, not "open" in any reasonable sense; it sits in the same
    # "no longer blocking" category as `approved` for the purpose of "is
    # there still open work standing between this Project and completion."
    # Only `pending` Change Orders count as open.
    if status_changing and requested_status == "completed":
        pending_count = (
            await current.session.scalar(
                select(func.count())
                .select_from(ChangeOrder)
                .where(ChangeOrder.project_id == project.id, ChangeOrder.status == "pending")
            )
            or 0
        )
        if pending_count:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot complete project: {pending_count} Change Order(s) pending approval",
            )

    if status_changing:
        project.status = requested_status

    # updated_at bumps automatically via UpdatedAtMixin's onupdate=utcnow the
    # moment the setattr above makes this row dirty and it gets flushed.
    await current.session.flush()

    if status_changing:
        await write_audit_log(
            current.session,
            company_id=project.company_id,
            actor_id=current.user.id,
            action="project.status_changed",
            entity_type="project",
            entity_id=project.id,
            metadata={"from": previous_status, "to": requested_status, "reason": payload.reason},
        )

    return ProjectResponse.model_validate(project)


@router.post(
    "/{project_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    project_id: uuid.UUID,
    file_name: str = Form(...),
    file: UploadFile = File(...),
    current: CurrentUser = Depends(require_role(*_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> DocumentResponse:
    """Task 1.15. `require_role("admin", "project_manager")` per the API
    spec table and the RBAC matrix (docs/07-security-compliance.md Section
    2: Project Management is "Full CRUD" for Admin/PM only, matching
    _WRITE_ROLES's existing rationale above).

    `_get_project_or_404` first, same order every other project-nested
    write route in this router/tasks.py uses (existence/tenant check
    before any semantic validation of the payload) — a cross-tenant
    project_id and an invalid file_name both fail, but the caller learns
    "not found" before "bad filename", never the other way around, so a
    cross-tenant probe can't be used to fish for filename-validation
    feedback.

    `company_id=project.company_id`, not `current.company_id`: a parent
    company's session can legitimately act on a descendant branch's Project
    without switching `X-Tenant-ID` to that branch first (RLS's
    `get_all_descendant_ids()` grant already makes the descendant's rows
    visible/writable). Using `current.company_id` here would silently stamp
    this Document — and the filesystem path it's written to, both derived
    from the same `company_id` value below — with the PARENT's id instead
    of the Project's own, producing a row whose `company_id` disagrees with
    its own parent Project's `company_id`. A session later scoped directly
    to the descendant branch (rather than the parent) would then find its
    own Project's Document invisible under RLS. Fixed as part of a
    dedicated post-Phase-2 audit of this exact pattern across every
    nested-resource-creation route in this codebase.
    """
    project = await _get_project_or_404(current, project_id)

    try:
        validate_file_name(file_name)
    except InvalidFileNameError as exc:
        # 422, not 400/403: file_name is well-formed input (a string) that
        # is semantically invalid in this context — same category this
        # router/tasks.py already uses 422 for (list_projects's `status`
        # filter, create_task's `phase_id`), never sanitized-and-accepted.
        # Deliberately raised BEFORE any DB query below and BEFORE
        # write_document_file() is ever called, so an invalid file_name
        # never causes a partial write or an orphaned Document row.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # version = previous_max_version + 1 for this exact (project_id,
    # file_name) pair, scoped to this project only — a same-named file in a
    # DIFFERENT project is an unrelated document, not a new version of this
    # one. A correlated MAX() query alongside the ORM insert below, same
    # "COUNT/MAX query living next to ordinary ORM code" comfort level as
    # _client_dashboard_response's COUNT queries above.
    previous_max_version = await current.session.scalar(
        select(func.max(Document.version)).where(
            Document.project_id == project.id, Document.file_name == file_name
        )
    )
    version = (previous_max_version or 0) + 1

    content = await file.read()
    try:
        storage_path = write_document_file(
            company_id=project.company_id,
            project_id=project.id,
            version=version,
            file_name=file_name,
            content=content,
        )
    except FileExistsError as exc:
        # A genuinely concurrent upload of the same file_name computed the
        # same `version` and won the race (see write_document_file's
        # docstring in app/services/document_storage.py) — 409, not a
        # silently-overwritten file or an unhandled 500. The caller can
        # simply retry the upload, which will read a fresh (now higher)
        # previous_max_version. Found during this task's code-quality review.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A concurrent upload of this file_name is in progress; retry the upload",
        ) from exc

    document = Document(
        project_id=project.id,
        company_id=project.company_id,
        file_name=file_name,
        storage_path=storage_path,
        version=version,
        uploaded_by=current.user.id,
    )
    current.session.add(document)
    await current.session.flush()
    # No explicit commit — get_current_user (Inherited Invariant #4) commits
    # current.session once, after this handler returns. No audit_log entry
    # either: same reasoning as create_project/create_phase above — Document
    # upload isn't in docs/07-security-compliance.md Section 5's enumerated
    # list of state changes needing an audit trail (that list is status
    # transitions), so none is written here.

    return DocumentResponse.model_validate(document)


@router.get("/{project_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_LIST_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> DocumentListResponse:
    """Task 1.15. Not in the API spec's literal route table (only `POST
    /projects/{id}/documents` is listed there) — added for the same reason
    design decision #3 added `PATCH /projects/{id}`: the spec doc "describes
    API contracts conceptually," and without a list route there is no way
    to satisfy US-3.4's "the most recent version is shown by default with
    prior versions accessible" at all.

    `_LIST_ROLES` (admin, project_manager, accountant, field_crew) reused
    as-is from list_projects above: the RBAC matrix's Project Management row
    gives every one of those four roles some form of read access, and
    `client` is deliberately excluded — the API spec never documents any
    list-shaped route for `client` (design decision #8: client only ever
    gets the single sanitized `GET /projects/{id}` dashboard route), same
    reasoning list_projects's own docstring gives for excluding `client`
    from that route.

    `_get_project_or_404` handles field_crew's assigned-only scoping here
    exactly as it does for phase/task creation: a field_crew caller whose
    project isn't theirs (no assigned task on it) gets a 404 before this
    function ever queries `documents`, so no separate per-document
    field_crew filter is needed below — visibility is gated at the project
    level, not the document level (documents have no assignee concept of
    their own).
    """
    project = await _get_project_or_404(current, project_id)

    # "Most recent version per file_name" is applied to the base query
    # BEFORE paginate() ever sees it (rather than, say, filtering the page
    # of rows paginate() returns AFTER the fact) — filtering post-pagination
    # would make `limit` apply to the wrong population (it would count
    # superseded versions against the page size, and a page could legally
    # end up smaller than `limit` even with plenty more distinct file_names
    # left to show) and would fight the cursor's meaning (the cursor is a
    # (created_at, id) position over the FILTERED population, not the raw
    # table, or a resumed page could re-skip/re-include rows inconsistently
    # across requests). A correlated subquery is used, rather than a
    # GROUP BY on file_name, because paginate() needs actual Document ORM
    # rows (with all response fields), not just aggregated (file_name,
    # max_version) pairs.
    DocumentVersion = aliased(Document)
    latest_version_for_file = (
        select(func.max(DocumentVersion.version))
        .where(
            DocumentVersion.project_id == Document.project_id,
            DocumentVersion.file_name == Document.file_name,
        )
        .correlate(Document)
        .scalar_subquery()
    )
    query = select(Document).where(
        Document.project_id == project.id,
        Document.version == latest_version_for_file,
    )

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=Document.created_at,
        id_col=Document.id,
        cursor=cursor,
        limit=limit,
    )

    return DocumentListResponse(
        items=[DocumentResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/{project_id}/documents/{document_id}/download")
async def download_document(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_LIST_ROLES)),
) -> FileResponse:
    """Streams the stored file (CRM+PM frontend spec, Decision 2 item 1).
    Same visibility rules as the document list: _get_project_or_404 covers
    tenant/role/project scope, and the document must belong to the path's
    project (a mismatched pair 404s — same id-pair discipline as every
    nested resource in this codebase). storage_path is always relative to
    settings.storage_root (document_storage.py's invariant), and file_name
    was traversal-validated at upload, so joining them is safe here."""
    project = await _get_project_or_404(current, project_id)

    result = await current.session.execute(
        select(Document).where(Document.id == document_id, Document.project_id == project.id)
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")

    file_path = Path(settings.storage_root) / document.storage_path
    if not file_path.is_file():
        # Row exists but the file is gone from disk — surface as 404 (the
        # resource is unretrievable) rather than a raw 500.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document file missing from storage")

    return FileResponse(file_path, filename=document.file_name, media_type="application/octet-stream")


@router.post(
    "/{project_id}/daily-logs",
    response_model=DailyLogResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_daily_log(
    project_id: uuid.UUID,
    payload: DailyLogCreateRequest,
    current: CurrentUser = Depends(require_role(*_DAILY_LOG_WRITE_ROLES)),
    _ro: None = Depends(block_if_read_only),
) -> DailyLogResponse:
    """Task 1.16. `require_role("admin", "project_manager", "field_crew")`
    per _DAILY_LOG_WRITE_ROLES above — the RBAC matrix's Project Management
    row is the only place field_crew gets any write verb at all ("create
    Daily Logs"), so this route intentionally does NOT reuse _WRITE_ROLES.

    `_get_project_or_404` first, same ordering as every other project-nested
    write route in this router (existence/tenant/field-crew-assigned-scope
    check before touching the payload) — this doubles as field_crew's
    project-level scoping: a field_crew caller with no assigned task on
    `project_id` gets a 404 here before a DailyLog row is ever created,
    exactly like upload_document's field_crew scoping. US-3.3 ("submit a
    Daily Log ... for a Project") and every other field_crew scoping
    decision in this plan point the same direction: assigned-project-only,
    not "any project in the tenant."

    `author_id=current.user.id` always — DailyLogCreateRequest has no
    `author_id` field (see its docstring), so a caller cannot claim to be
    someone else's author by payload manipulation; this line is the only
    place author_id is ever set.

    No update/delete route exists anywhere in this router for daily_logs,
    and Task 1.10's migration additionally `REVOKE`s UPDATE/DELETE on this
    table from `app_user` at the DB level (design decision #6) — immutable
    once submitted, matching US-3.3's acceptance criterion, the same
    two-layer discipline (no route + DB-level REVOKE) Task 1.7 established
    for communication_logs.

    `company_id=project.company_id`, not `current.company_id` — see
    `upload_document`'s docstring above for the full rationale (a parent
    company's session acting on a descendant branch's Project, without
    switching `X-Tenant-ID`, must not stamp this child row with the
    parent's own company_id).
    """
    project = await _get_project_or_404(current, project_id)

    daily_log = DailyLog(
        project_id=project.id,
        company_id=project.company_id,
        author_id=current.user.id,
        log_date=payload.log_date,
        weather=payload.weather,
        notes=payload.notes,
    )
    current.session.add(daily_log)
    await current.session.flush()
    # No explicit commit (Inherited Invariant #4 — get_current_user commits
    # current.session once). No audit_log entry: same reasoning as
    # create_project/create_phase/upload_document above — Daily Log
    # submission isn't in docs/07-security-compliance.md Section 5's
    # enumerated list of state changes requiring an audit trail (status
    # transitions, approvals, overrides), and immutability is already
    # DB-enforced independently of the audit log.

    return DailyLogResponse.model_validate(daily_log)


@router.get("/{project_id}/daily-logs", response_model=DailyLogListResponse)
async def list_daily_logs(
    project_id: uuid.UUID,
    current: CurrentUser = Depends(require_role(*_LIST_ROLES)),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> DailyLogListResponse:
    """Task 1.16. The plan spec says "same read roles as project detail" —
    read literally that would mean _GET_ROLES (which additionally includes
    `client`). This deliberately uses `_LIST_ROLES` (client-EXCLUDED)
    instead, for the same reason list_documents does immediately above:
    `client`'s only documented read surface anywhere in the RBAC matrix or
    API spec is the single sanitized `GET /projects/{id}` dashboard route
    (design decision #8: "client only ever gets the single sanitized
    dashboard route"), which itself now exposes `phase_count`/`task_count`/
    `completed_task_count` as the client-facing substitute for granular
    per-record detail. A `client` caller hitting a raw, unsanitized,
    paginated list of daily log notes would both contradict design decision
    #8 and go beyond US-3.5's explicit exclusion of "internal task detail"
    from what the client sees. `_LIST_ROLES` = admin, project_manager,
    accountant, field_crew — the matrix's Project Management row gives all
    four some read access (accountant's row says "Read (financial fields
    only)", which for Phase 1 collapses to plain read, same reasoning
    _LIST_ROLES's own comment gives above).

    `_get_project_or_404` applies field_crew's assigned-only scoping here
    exactly as list_documents does: an unassigned field_crew caller gets a
    404 before any daily_logs query runs.
    """
    project = await _get_project_or_404(current, project_id)

    query = select(DailyLog).where(DailyLog.project_id == project.id)

    rows, next_cursor = await paginate(
        current.session,
        query,
        created_at_col=DailyLog.created_at,
        id_col=DailyLog.id,
        cursor=cursor,
        limit=limit,
    )

    return DailyLogListResponse(
        items=[DailyLogResponse.model_validate(row) for row in rows],
        next_cursor=next_cursor,
    )
