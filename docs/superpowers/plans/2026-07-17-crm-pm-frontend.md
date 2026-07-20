# CRM + Project Management Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real CRM (leads, communication logs) and Project Management (projects, phases, tasks, documents, daily logs) screens for all four roles, replacing Foundation's placeholder dashboard, per `docs/superpowers/specs/2026-07-17-crm-pm-frontend-design.md`.

**Architecture:** Six small backend read additions (role on TokenResponse, client project-list access, phases-with-tasks list, my-tasks list, document download, dashboard summary), then frontend screens on Foundation's BFF pattern: thin Next.js Route Handlers proxy every backend call; pages are client components using `useAuth()` + `fetch("/api/...")`.

**Tech Stack:** FastAPI + SQLAlchemy (backend additions), Next.js 16 App Router, Tailwind v4, hand-written shadcn-style primitives, Playwright E2E. **Zero new npm or Python dependencies.**

---

## Prerequisite (before Task 1)

PRs #16 (MFA-optional redirect) and #17 (AuthContext hardening) modify `frontend/components/auth/{LoginForm,RegisterForm}.tsx` and `frontend/contexts/AuthContext.tsx` — files Tasks 8 of this plan also modifies. **This plan's code is written against the post-#16/#17 versions.** Before starting:

```bash
cd "D:\Development\New const proj mgt software\.worktrees\crm-pm-frontend"
git fetch origin
git log origin/main --oneline -5
```

If `origin/main` contains "fix: harden AuthContext against multi-tab and Strict-Mode refresh races" and "fix: stop forcing a detour through /account for MFA-less admins", run `git rebase origin/main` and continue. **If either is missing, STOP and report** — the user hasn't merged them yet; do not proceed against stale versions.

## Existing conventions to follow

- **Backend:** each router owns its role constants; `require_role(*ROLES)` dependency; RLS handles tenant scoping (no explicit company_id filters in queries); reads are never `block_if_read_only`-gated; cursor pagination via `app/core/pagination.py`'s `paginate()`; tests live one-file-per-feature in `backend/tests/`, duplicating the `_register_and_login`/`_invite_and_login_as` helpers per file (established convention, see test_phases_tasks.py).
- **Backend test runs (host-side):** `cd backend && .venv\Scripts\python.exe -m pytest tests/<file> -v` (Windows venv). Requires the dockerized Postgres+Redis up (`docker compose up -d postgres redis` from the worktree root is enough for tests; conftest creates its own test DB).
- **Frontend:** every dependency exact-pinned (no `^`/`~`) — this plan adds none; `"use client"` components fetch `/api/...` only, never the backend; Route Handlers forward the `Authorization` bearer and map `ApiError` → `{detail}` JSON + status; forms use the established hardening (try/catch network-failure message, `if (submitting) return` guard, `disabled={submitting}` inputs, `role="alert" aria-live="assertive"` errors); `npx tsc --noEmit` must pass per task (run inside the frontend container if host has no node_modules: `docker compose exec frontend npx tsc --noEmit`).
- **tsconfig.json self-normalizes** under `next dev`/`next build` (jsx, include list) — expected, never revert it. Delete stray `tsconfig.tsbuildinfo` before committing.

## File structure (full inventory)

**Backend — modified:** `app/schemas/auth.py`, `app/routers/auth.py`, `app/schemas/project.py`, `app/routers/projects.py`, `app/schemas/phase.py`, `app/schemas/task.py`, `app/routers/tasks.py`, `app/main.py`, tests for each.
**Backend — created:** `app/schemas/dashboard.py`, `app/routers/dashboard.py`, `tests/test_dashboard_summary.py`, `tests/test_my_tasks.py`, `tests/test_document_download.py`.
**Frontend — modified:** `lib/api/client.ts`, `lib/api/types.ts` (regenerated), `contexts/AuthContext.tsx`, `app/(app)/api/auth/login/route.ts`, `app/(app)/api/auth/refresh/route.ts`, `components/auth/LoginForm.tsx`, `components/auth/RegisterForm.tsx`, `middleware.ts`, `components/app-shell/Nav.tsx`, `app/(app)/dashboard/page.tsx`.
**Frontend — created:** `components/ui/select.tsx`, `components/ui/textarea.tsx`, `lib/state-machines.ts`, `lib/format.ts`, `components/ui/status-badge.tsx`, `lib/api/handler-utils.ts`, ~13 Route Handler files under `app/(app)/api/`, pages `app/(app)/leads/page.tsx`, `leads/new/page.tsx`, `leads/[id]/page.tsx`, `projects/page.tsx`, `projects/new/page.tsx`, `projects/[id]/page.tsx`, `my-tasks/page.tsx`, components under `components/{leads,projects,tasks,dashboard}/`, `e2e/crm-pm.spec.ts`.

---

### Task 1: Backend — `role` on TokenResponse

**Files:**
- Modify: `backend/app/schemas/auth.py` (TokenResponse, ~line 49)
- Modify: `backend/app/routers/auth.py` (login + refresh TokenResponse constructor sites)
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_auth.py`:

```python
async def test_login_and_refresh_return_role(client):
    await client.post(
        "/auth/register",
        json={
            "company_name": "Role Co",
            "admin_full_name": "Role Admin",
            "admin_email": "role-admin@acme.test",
            "admin_password": "supersecret123",
        },
    )
    login = await client.post(
        "/auth/login", json={"email": "role-admin@acme.test", "password": "supersecret123"}
    )
    assert login.status_code == 200
    body = login.json()
    assert body["role"] == "admin"

    refresh = await client.post("/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert refresh.status_code == 200
    assert refresh.json()["role"] == "admin"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_auth.py::test_login_and_refresh_return_role -v`
Expected: FAIL with `KeyError: 'role'`.

- [ ] **Step 3: Add the field.** In `backend/app/schemas/auth.py`, `TokenResponse` gains one field after `mfa_enrollment_required`:

```python
class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    default_company_id: uuid.UUID
    # Defaulted so any other TokenResponse constructor site compiles before
    # being updated — but login and refresh MUST wire this explicitly per
    # spec Decision 3, never rely on the default.
    mfa_enrollment_required: bool = False
    # The user's role in their default company, from the same membership row
    # login/refresh already resolve via _default_membership (CRM+PM frontend
    # spec, Decision 1): the frontend needs it to choose which UI to render,
    # and the JWT deliberately carries no role claim. Display/routing signal
    # only — the backend's require_role checks remain the sole authorization
    # boundary. No default: both mint sites must wire it explicitly.
    role: str
```

- [ ] **Step 4: Wire both mint sites.** In `backend/app/routers/auth.py`, both `TokenResponse(...)` constructor calls (one in `login`, one in `refresh`) already have `membership` in scope — add `role=membership.role,` to each, e.g. the refresh site becomes:

```python
        return TokenResponse(
            access_token=create_access_token(
                user_id=str(old_row.user_id), default_company_id=str(membership.company_id)
            ),
            refresh_token=new_secret,
            default_company_id=membership.company_id,
            mfa_enrollment_required=(
                membership.role == "admin" and user.mfa_activated_at is None
            ),
            role=membership.role,
        )
```

Make the identical one-line addition in `login`'s `TokenResponse(...)` return.

- [ ] **Step 5: Run the test file to verify it passes**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_auth.py -v`
Expected: all pass (the new test plus every existing one — no other test constructs `TokenResponse` directly).

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/auth.py backend/app/routers/auth.py backend/tests/test_auth.py
git commit -m "feat: role on TokenResponse from the default membership"
```

---

### Task 2: Backend — client role access to GET /projects

**Files:**
- Modify: `backend/app/schemas/project.py` (new list envelope)
- Modify: `backend/app/routers/projects.py` (`list_projects`)
- Test: `backend/tests/test_projects.py`

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_projects.py` (it already defines `_register_and_login`, `_invite_and_login_as`, and a project-creation helper — reuse them; check their exact names at the top of the file before writing):

```python
async def test_client_can_list_projects_sanitized(client):
    admin = await _register_and_login(client, "Client List Co", "client-list-admin@acme.test")
    created = await client.post(
        "/projects",
        json={"name": "Visible To Client", "site_address": "1 Main St"},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text

    member = await _invite_and_login_as(client, admin, "client", "client-list-user@acme.test")
    listed = await client.get("/projects", headers=member["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["name"] == "Visible To Client"
    # Sanitized shape: counts present, internal fields absent.
    assert item["phase_count"] == 0
    assert item["task_count"] == 0
    assert item["completed_task_count"] == 0
    assert "lead_id" not in item
    assert "company_id" not in item
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_projects.py::test_client_can_list_projects_sanitized -v`
Expected: FAIL with a 403 (client not in `_LIST_ROLES`).

- [ ] **Step 3: Add the sanitized list envelope.** In `backend/app/schemas/project.py`, after `ProjectListResponse`:

```python
class ProjectClientDashboardListResponse(BaseModel):
    """List envelope for the `client` role's `GET /projects` (CRM+PM
    frontend spec, Decision 2 item 6): same sanitized per-project shape the
    detail route already serves that role, so a client can discover their
    project(s) at all — before this, `client` could GET a project by id but
    had no route that would ever tell them the id."""

    items: list[ProjectClientDashboardResponse]
    next_cursor: str | None = None
```

- [ ] **Step 4: Extend the route.** In `backend/app/routers/projects.py`, change `list_projects`'s decorator, dependency, return annotation, and add the client branch after `paginate(...)`:

```python
@router.get("", response_model=ProjectListResponse | ProjectClientDashboardListResponse)
async def list_projects(
    current: CurrentUser = Depends(require_role(*_GET_ROLES)),
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(None),
) -> ProjectListResponse | ProjectClientDashboardListResponse:
```

(body unchanged through `paginate(...)`, then:)

```python
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
```

Import `ProjectClientDashboardListResponse` alongside the other project schema imports at the top of the file.

- [ ] **Step 5: Run the projects test file**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_projects.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/project.py backend/app/routers/projects.py backend/tests/test_projects.py
git commit -m "feat: client role can list projects (sanitized dashboard shape)"
```

---

### Task 3: Backend — GET /projects/{id}/phases with nested tasks

**Files:**
- Modify: `backend/app/schemas/phase.py`
- Modify: `backend/app/routers/tasks.py`
- Test: `backend/tests/test_phases_tasks.py`

- [ ] **Step 1: Write the failing test** — append to `backend/tests/test_phases_tasks.py` (reuse its existing `_register_and_login` and project-creation helpers — read the file's top first for exact names/signatures):

```python
async def test_list_phases_returns_phases_with_nested_tasks_in_sequence_order(client):
    admin = await _register_and_login(client, "Phase List Co", "phase-list@acme.test")
    project = await client.post(
        "/projects",
        json={"name": "Phase List Project", "site_address": "2 Main St"},
        headers=admin["headers"],
    )
    project_id = project.json()["id"]

    second = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": "Second", "sequence": 2},
        headers=admin["headers"],
    )
    first = await client.post(
        f"/projects/{project_id}/phases",
        json={"name": "First", "sequence": 1},
        headers=admin["headers"],
    )
    task = await client.post(
        f"/projects/{project_id}/tasks",
        json={"name": "In First", "phase_id": first.json()["id"]},
        headers=admin["headers"],
    )
    assert task.status_code == 201, task.text

    listed = await client.get(f"/projects/{project_id}/phases", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [p["name"] for p in items] == ["First", "Second"]
    assert [t["name"] for t in items[0]["tasks"]] == ["In First"]
    assert items[1]["tasks"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_phases_tasks.py::test_list_phases_returns_phases_with_nested_tasks_in_sequence_order -v`
Expected: FAIL with 405 (no GET route).

- [ ] **Step 3: Add the response schemas.** In `backend/app/schemas/phase.py`:

```python
from app.schemas.task import TaskResponse


class PhaseWithTasksResponse(PhaseResponse):
    """`GET /projects/{id}/phases` item shape (CRM+PM frontend spec,
    Decision 2 item 3): a phase plus its tasks, nested — the frontend's
    Phases & tasks accordion renders exactly this."""

    tasks: list[TaskResponse]


class PhaseListResponse(BaseModel):
    """NOT cursor-paginated, deliberately (unlike every other list envelope
    in this codebase): phases-per-project is bounded small by the domain (a
    construction project has a handful of phases, not thousands), and the
    accordion UI needs them all at once anyway."""

    items: list[PhaseWithTasksResponse]
```

(add `BaseModel` to the existing pydantic import if not present.)

- [ ] **Step 4: Add the route.** In `backend/app/routers/tasks.py`, add a read-roles constant next to `_WRITE_ROLES` and the route after the existing task-create route:

```python
# Same read set projects.py's _LIST_ROLES grants (each router owns its role
# constants — established convention, see _WRITE_ROLES above). client is
# excluded: their project view is the counts-only dashboard shape.
_READ_ROLES = ("admin", "project_manager", "accountant", "field_crew")
```

```python
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
```

Extend the imports at the top: `from app.schemas.phase import PhaseCreateRequest, PhaseListResponse, PhaseResponse, PhaseWithTasksResponse`.

- [ ] **Step 5: Run the file's tests**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_phases_tasks.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/phase.py backend/app/routers/tasks.py backend/tests/test_phases_tasks.py
git commit -m "feat: list phases with nested tasks for a project"
```

---

### Task 4: Backend — GET /tasks?assignee=me

**Files:**
- Modify: `backend/app/schemas/task.py`
- Modify: `backend/app/routers/tasks.py`
- Test: `backend/tests/test_my_tasks.py` (new)

- [ ] **Step 1: Write the failing test** — create `backend/tests/test_my_tasks.py` (copy `_register_and_login` and `_invite_and_login_as` verbatim from `tests/test_phases_tasks.py`, per the established per-file helper convention):

```python
"""CRM+PM frontend spec Decision 2 item 4: GET /tasks?assignee=me — the
cross-project task list behind the frontend's My Tasks view."""
import asyncpg

from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

# [copy _register_and_login and _invite_and_login_as here verbatim from
#  tests/test_phases_tasks.py]


async def _project_with_phase(client, admin, name):
    project = await client.post(
        "/projects",
        json={"name": name, "site_address": "3 Main St"},
        headers=admin["headers"],
    )
    phase = await client.post(
        f"/projects/{project.json()['id']}/phases",
        json={"name": "Phase"},
        headers=admin["headers"],
    )
    return project.json(), phase.json()


async def test_my_tasks_returns_only_own_tasks_with_project_context(client):
    admin = await _register_and_login(client, "My Tasks Co", "my-tasks-admin@acme.test")
    crew = await _invite_and_login_as(client, admin, "field_crew", "my-tasks-crew@acme.test")
    project, phase = await _project_with_phase(client, admin, "Crew Project")

    mine = await client.post(
        f"/projects/{project['id']}/tasks",
        json={"name": "Mine", "phase_id": phase["id"], "assignee_id": crew["user_id"]},
        headers=admin["headers"],
    )
    assert mine.status_code == 201, mine.text
    unassigned = await client.post(
        f"/projects/{project['id']}/tasks",
        json={"name": "Not mine", "phase_id": phase["id"]},
        headers=admin["headers"],
    )
    assert unassigned.status_code == 201

    listed = await client.get("/tasks?assignee=me", headers=crew["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [t["name"] for t in items] == ["Mine"]
    assert items[0]["project_id"] == project["id"]
    assert items[0]["project_name"] == "Crew Project"


async def test_my_tasks_rejects_unsupported_assignee_value(client):
    admin = await _register_and_login(client, "My Tasks Val Co", "my-tasks-val@acme.test")
    response = await client.get("/tasks?assignee=someone-else", headers=admin["headers"])
    assert response.status_code == 422
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_my_tasks.py -v`
Expected: FAIL with 405 (no GET /tasks route).

- [ ] **Step 3: Add the response schemas.** In `backend/app/schemas/task.py`:

```python
class MyTaskResponse(TaskResponse):
    """`GET /tasks?assignee=me` item shape: TaskResponse enriched with
    project context (tasks reference only their phase directly, but the My
    Tasks view renders "task · project · due date" rows)."""

    project_id: uuid.UUID
    project_name: str


class MyTaskListResponse(BaseModel):
    """NOT cursor-paginated: one user's open assignment list is bounded
    small in practice; capped at 200 in the route (with the cap noted in
    the response being fine to revisit if it's ever actually hit)."""

    items: list[MyTaskResponse]
```

- [ ] **Step 4: Add the route.** In `backend/app/routers/tasks.py` (after `list_phases`; add `Project` to the models import and `Query` to the fastapi import):

```python
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
```

Extend the schema import: `from app.schemas.task import MyTaskListResponse, MyTaskResponse, TaskCreateRequest, TaskResponse, TaskUpdateRequest`.

**Route-ordering note:** FastAPI matches `GET /tasks` and `PATCH /tasks/{task_id}` by method+path independently — no conflict regardless of declaration order.

- [ ] **Step 5: Run the new test file**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_my_tasks.py -v`
Expected: both pass.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/task.py backend/app/routers/tasks.py backend/tests/test_my_tasks.py
git commit -m "feat: cross-project my-tasks list endpoint"
```

---

### Task 5: Backend — document download route

**Files:**
- Modify: `backend/app/routers/projects.py`
- Test: `backend/tests/test_document_download.py` (new)

- [ ] **Step 1: Write the failing test** — create `backend/tests/test_document_download.py` (copy `_register_and_login` verbatim from `tests/test_phases_tasks.py`):

```python
"""CRM+PM frontend spec Decision 2 item 1: GET .../documents/{id}/download.
No download route existed before this — documents could be uploaded and
listed but never retrieved."""

# [copy _register_and_login here verbatim from tests/test_phases_tasks.py]


async def _project(client, admin, name):
    response = await client.post(
        "/projects",
        json={"name": name, "site_address": "4 Main St"},
        headers=admin["headers"],
    )
    return response.json()


async def test_download_round_trips_uploaded_bytes(client):
    admin = await _register_and_login(client, "Download Co", "download-admin@acme.test")
    project = await _project(client, admin, "Download Project")

    upload = await client.post(
        f"/projects/{project['id']}/documents",
        files={"file": ("plan.txt", b"blueprint bytes", "text/plain")},
        data={"file_name": "plan.txt"},
        headers=admin["headers"],
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["id"]

    download = await client.get(
        f"/projects/{project['id']}/documents/{document_id}/download",
        headers=admin["headers"],
    )
    assert download.status_code == 200, download.text
    assert download.content == b"blueprint bytes"
    assert "plan.txt" in download.headers["content-disposition"]


async def test_download_404_for_document_of_other_project(client):
    admin = await _register_and_login(client, "Download Iso Co", "download-iso@acme.test")
    project_a = await _project(client, admin, "Project A")
    project_b = await _project(client, admin, "Project B")

    upload = await client.post(
        f"/projects/{project_a['id']}/documents",
        files={"file": ("a.txt", b"a", "text/plain")},
        data={"file_name": "a.txt"},
        headers=admin["headers"],
    )
    document_id = upload.json()["id"]

    # Right document id, wrong project in the path — must 404, not leak.
    response = await client.get(
        f"/projects/{project_b['id']}/documents/{document_id}/download",
        headers=admin["headers"],
    )
    assert response.status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_document_download.py -v`
Expected: FAIL with 404s from FastAPI's router (no such route) on the first test's download step — the assert on status 200 fails.

- [ ] **Step 3: Add the route.** In `backend/app/routers/projects.py`, after the existing document list route. Add imports: `from pathlib import Path` (top-level stdlib imports), `from fastapi.responses import FileResponse`, `from app.config import settings` (check which of these the file already imports first):

```python
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
```

- [ ] **Step 4: Run the new test file**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_document_download.py -v`
Expected: both pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/projects.py backend/tests/test_document_download.py
git commit -m "feat: document download route - stored files were write-only before"
```

---

### Task 6: Backend — dashboard summary endpoint

**Files:**
- Create: `backend/app/schemas/dashboard.py`
- Create: `backend/app/routers/dashboard.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_dashboard_summary.py` (new)

- [ ] **Step 1: Write the failing test** — create `backend/tests/test_dashboard_summary.py` (copy `_register_and_login` and `_invite_and_login_as` verbatim from `tests/test_phases_tasks.py`):

```python
"""CRM+PM frontend spec Decision 2 item 2: GET /dashboard/summary."""
from datetime import date, timedelta

import asyncpg

from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

# [copy _register_and_login and _invite_and_login_as here verbatim from
#  tests/test_phases_tasks.py]


async def test_summary_counts_open_leads_active_projects_and_due_tasks(client):
    admin = await _register_and_login(client, "Summary Co", "summary-admin@acme.test")

    lead = await client.post(
        "/leads",
        json={
            "contact_name": "Open Lead",
            "project_name": "Open Lead Project",
            "email": "open-lead@acme.test",
            "project_type": "remodel",
        },
        headers=admin["headers"],
    )
    assert lead.status_code == 201

    project = await client.post(
        "/projects",
        json={"name": "Summary Project", "site_address": "5 Main St"},
        headers=admin["headers"],
    )
    project_id = project.json()["id"]
    for target in ("pre_construction", "active"):
        moved = await client.patch(
            f"/projects/{project_id}/status", json={"status": target}, headers=admin["headers"]
        )
        assert moved.status_code == 200, moved.text

    phase = await client.post(
        f"/projects/{project_id}/phases", json={"name": "P"}, headers=admin["headers"]
    )
    due_soon = await client.post(
        f"/projects/{project_id}/tasks",
        json={
            "name": "Due soon",
            "phase_id": phase.json()["id"],
            "due_date": (date.today() + timedelta(days=2)).isoformat(),
        },
        headers=admin["headers"],
    )
    assert due_soon.status_code == 201
    far_out = await client.post(
        f"/projects/{project_id}/tasks",
        json={
            "name": "Far out",
            "phase_id": phase.json()["id"],
            "due_date": (date.today() + timedelta(days=30)).isoformat(),
        },
        headers=admin["headers"],
    )
    assert far_out.status_code == 201

    summary = await client.get("/dashboard/summary", headers=admin["headers"])
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["open_leads"] == 1
    assert body["active_projects"] == 1
    assert body["tasks_due_this_week"] == 1


async def test_summary_denied_for_field_crew(client):
    admin = await _register_and_login(client, "Summary Deny Co", "summary-deny@acme.test")
    crew = await _invite_and_login_as(client, admin, "field_crew", "summary-crew@acme.test")
    response = await client.get("/dashboard/summary", headers=crew["headers"])
    assert response.status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_dashboard_summary.py -v`
Expected: FAIL with 404 (route doesn't exist).

- [ ] **Step 3: Create the schema.** `backend/app/schemas/dashboard.py`:

```python
from pydantic import BaseModel


class DashboardSummaryResponse(BaseModel):
    """GET /dashboard/summary (CRM+PM frontend spec, Decision 2 item 2).
    Exact COUNTs, not page-derived approximations — the list endpoints are
    cursor-paginated with no total field."""

    open_leads: int
    active_projects: int
    tasks_due_this_week: int
```

- [ ] **Step 4: Create the router.** `backend/app/routers/dashboard.py`:

```python
"""Dashboard summary counts (CRM+PM frontend spec, Decision 2 item 2).

admin/project_manager only: they are the only roles that see the dashboard
(the frontend redirects field_crew to /my-tasks and client to their project
before this is ever called), and field_crew has no lead access anyway. RLS
scopes every count to the caller's tenant — no explicit company_id filters,
same as every other router.
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.core.deps import CurrentUser, require_role
from app.models import Lead, Project, Task
from app.schemas.dashboard import DashboardSummaryResponse

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_ROLES = ("admin", "project_manager")


@router.get("/summary", response_model=DashboardSummaryResponse)
async def dashboard_summary(
    current: CurrentUser = Depends(require_role(*_ROLES)),
) -> DashboardSummaryResponse:
    today = date.today()
    week_out = today + timedelta(days=7)

    open_leads = await current.session.scalar(
        select(func.count()).select_from(Lead).where(Lead.status.notin_(("won", "lost")))
    )
    active_projects = await current.session.scalar(
        select(func.count()).select_from(Project).where(Project.status == "active")
    )
    tasks_due_this_week = await current.session.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.status != "done", Task.due_date.isnot(None), Task.due_date >= today, Task.due_date <= week_out)
    )

    return DashboardSummaryResponse(
        open_leads=open_leads or 0,
        active_projects=active_projects or 0,
        tasks_due_this_week=tasks_due_this_week or 0,
    )
```

- [ ] **Step 5: Register it.** In `backend/app/main.py`, add `dashboard` to the `from app.routers import (...)` list and `app.include_router(dashboard.router)` after the existing includes.

- [ ] **Step 6: Run the new test file, then the whole backend suite**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_dashboard_summary.py -v`
Expected: both pass.
Run: `cd backend && .venv\Scripts\python.exe -m pytest`
Expected: full suite green (this closes out all backend work; catch cross-cutting breakage now, not in Task 20).

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/dashboard.py backend/app/routers/dashboard.py backend/app/main.py backend/tests/test_dashboard_summary.py
git commit -m "feat: dashboard summary counts endpoint"
```

---

### Task 7: Frontend — regenerate API types; client.ts query support

**Files:**
- Modify: `frontend/lib/api/types.ts` (regenerated)
- Modify: `frontend/lib/api/client.ts`

- [ ] **Step 1: Install frontend deps in this worktree** (fresh checkout has no node_modules):

```bash
cd frontend && npm ci
```

- [ ] **Step 2: Bring up this worktree's stack and regenerate types.** Same Docker convention as every prior feature: check the MAIN repo (`docker compose ps` in `D:\Development\New const proj mgt software`); if running, `docker compose down` (no `-v`). Copy `.env` from the main repo root into the worktree root if absent (gitignored). Then from the worktree root: `docker compose up -d --build backend postgres redis`, wait for `curl http://localhost:8000/health` → `{"status":"ok"}`, apply migrations if the volume is fresh (`docker compose exec -e MIGRATIONS_DATABASE_URL=postgresql+asyncpg://postgres:devpassword@postgres:5432/builders_stream backend alembic upgrade head`), then:

```bash
cd frontend && npm run generate:api-types
```

Verify the diff includes the new routes: `git diff lib/api/types.ts | grep -E "dashboard/summary|/download|/tasks|/phases"` should show additions. Leave the stack up if proceeding straight to later tasks needing it, otherwise tear down and restart the main repo's stack (hard requirement, as always).

- [ ] **Step 3: Extend `apiFetch` with query-string support and export the base URL.** In `frontend/lib/api/client.ts`: change the `RequestOptions` interface and URL construction, and export the constant (needed by Task 13's multipart/stream handlers, which can't use JSON-only `apiFetch`):

```ts
export const BACKEND_API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
```

```ts
interface RequestOptions {
  accessToken?: string;
  companyId?: string;
  body?: unknown;
  params?: Record<string, string>;
  // Appended as a query string (?k=v&...). Entries with undefined values
  // are skipped, so callers can pass optional filters unconditionally.
  query?: Record<string, string | undefined>;
}
```

After the existing unresolved-placeholder check in `apiFetch`, add:

```ts
  if (options.query) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(options.query)) {
      if (value !== undefined) search.set(key, value);
    }
    const qs = search.toString();
    if (qs) url += `?${qs}`;
  }
```

- [ ] **Step 4: Type-check**

Run: `docker compose exec frontend npx tsc --noEmit` (or `cd frontend && npx tsc --noEmit` if host node_modules exist).
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/api/types.ts frontend/lib/api/client.ts
git commit -m "feat: regenerate API types for CRM+PM endpoints; apiFetch query support"
```

---

### Task 8: Frontend — role in AuthContext, auth handlers, middleware

**Files:**
- Modify: `frontend/contexts/AuthContext.tsx`
- Modify: `frontend/app/(app)/api/auth/login/route.ts`
- Modify: `frontend/app/(app)/api/auth/refresh/route.ts`
- Modify: `frontend/components/auth/LoginForm.tsx`
- Modify: `frontend/components/auth/RegisterForm.tsx`
- Modify: `frontend/middleware.ts`

- [ ] **Step 1: Thread `role` through AuthContext.** This file is the post-PR#17 hardened version (Web Locks + generation counter — see Prerequisite). Make exactly these changes:

```ts
interface AuthState {
  accessToken: string | null;
  mfaEnrollmentRequired: boolean;
  // From TokenResponse.role (the user's role in their default company) —
  // UI-rendering signal only; the backend's require_role checks remain the
  // sole authorization boundary. null until a session is confirmed.
  role: string | null;
}
```

```ts
type RefreshResult = { access_token: string; mfa_enrollment_required: boolean; role: string };
```

- `setSession` signature becomes `(accessToken: string, mfaEnrollmentRequired: boolean, role: string)`; its `setState` sets all three; the `AuthContextValue` interface matches.
- Every `setState` reset (initial state, `clearSession`) includes `role: null`.
- `scheduleRefresh`'s success path: `setState({ accessToken: data.access_token, mfaEnrollmentRequired: data.mfa_enrollment_required, role: data.role });`
- The mount-hydration effect's success path: `setSession(data.access_token, data.mfa_enrollment_required, data.role);`

- [ ] **Step 2: Return `role` from the login and refresh Route Handlers.** In both `frontend/app/(app)/api/auth/login/route.ts` and `frontend/app/(app)/api/auth/refresh/route.ts`: add `role: string;` to the cast of the `apiFetch` result, and `role: data.role,` to the `NextResponse.json({...})` success payload. (Cookie/no-store logic unchanged.)

- [ ] **Step 3: Update both forms' `setSession` calls.**
- `frontend/components/auth/LoginForm.tsx`: `setSession(data.access_token, data.mfa_enrollment_required, data.role);`
- `frontend/components/auth/RegisterForm.tsx`: `setSession(loginData.access_token, loginData.mfa_enrollment_required, loginData.role);`
(Both still `router.push("/dashboard")` — PR #16's behavior, unchanged.)

- [ ] **Step 4: Extend the middleware matcher.** In `frontend/middleware.ts`:

```ts
export const config = {
  matcher: [
    "/dashboard/:path*",
    "/account/:path*",
    "/leads/:path*",
    "/projects/:path*",
    "/my-tasks/:path*",
  ],
};
```

- [ ] **Step 5: Type-check** — `npx tsc --noEmit`, expect exit 0 (the compiler confirms every `setSession` caller was updated).

- [ ] **Step 6: Commit**

```bash
git add frontend/contexts/AuthContext.tsx "frontend/app/(app)/api/auth/login/route.ts" "frontend/app/(app)/api/auth/refresh/route.ts" frontend/components/auth/LoginForm.tsx frontend/components/auth/RegisterForm.tsx frontend/middleware.ts
git commit -m "feat: thread role through session state; guard CRM+PM routes in middleware"
```

---

### Task 9: Frontend — Select/Textarea primitives, state machines, shared helpers

**Files:**
- Create: `frontend/components/ui/select.tsx`, `frontend/components/ui/textarea.tsx`, `frontend/components/ui/status-badge.tsx`
- Create: `frontend/lib/state-machines.ts`, `frontend/lib/format.ts`, `frontend/lib/api/handler-utils.ts`

- [ ] **Step 1: Native select primitive.** `frontend/components/ui/select.tsx` (hand-written like Foundation's Input — native element, no new dependency):

```tsx
import * as React from "react";
import { cn } from "@/lib/utils";

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement>;

const Select = React.forwardRef<HTMLSelectElement, SelectProps>(({ className, ...props }, ref) => {
  return (
    <select
      className={cn(
        "flex h-10 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      ref={ref}
      {...props}
    />
  );
});
Select.displayName = "Select";

export { Select };
```

- [ ] **Step 2: Textarea primitive.** `frontend/components/ui/textarea.tsx`:

```tsx
import * as React from "react";
import { cn } from "@/lib/utils";

export type TextareaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement>;

const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(({ className, ...props }, ref) => {
  return (
    <textarea
      className={cn(
        "flex min-h-20 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm placeholder:text-slate-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      ref={ref}
      {...props}
    />
  );
});
Textarea.displayName = "Textarea";

export { Textarea };
```

- [ ] **Step 3: State-machine constants (display-only — spec Decision 6).** `frontend/lib/state-machines.ts`:

```ts
// Mirrors of the backend's transition tables, used ONLY to decide which
// action buttons to render (spec Decision 6). The backend's transition
// validation is the sole enforcement; a 409 from it is surfaced verbatim.

export const LEAD_STATUSES = ["new", "contacted", "estimating", "qualified", "won", "lost"] as const;

export const LEAD_TRANSITIONS: Record<string, string[]> = {
  new: ["contacted", "lost"],
  contacted: ["estimating", "lost"],
  estimating: ["qualified", "lost"],
  qualified: ["won", "lost"],
  won: [],
  lost: [],
};

// The linear "pipeline" path shown in the breadcrumb (lost is an exit, not
// a pipeline stage).
export const LEAD_PIPELINE = ["new", "contacted", "estimating", "qualified", "won"] as const;

export const PROJECT_TRANSITIONS: Record<string, string[]> = {
  draft: ["pre_construction"],
  pre_construction: ["active"],
  active: ["suspended", "completed"],
  suspended: ["active", "completed"],
  completed: ["archived"],
  archived: [],
};

export const TASK_STATUSES = ["open", "in_progress", "done"] as const;

export const STATUS_LABELS: Record<string, string> = {
  new: "New",
  contacted: "Contacted",
  estimating: "Estimating",
  qualified: "Qualified",
  won: "Won",
  lost: "Lost",
  draft: "Draft",
  pre_construction: "Pre-construction",
  active: "Active",
  suspended: "Suspended",
  completed: "Completed",
  archived: "Archived",
  open: "Open",
  in_progress: "In progress",
  done: "Done",
};

export function labelFor(status: string): string {
  return STATUS_LABELS[status] ?? status;
}
```

- [ ] **Step 4: Status badge.** `frontend/components/ui/status-badge.tsx`:

```tsx
import { cn } from "@/lib/utils";
import { labelFor } from "@/lib/state-machines";

const TONE_CLASSES: Record<string, string> = {
  green: "bg-green-50 text-green-700",
  blue: "bg-blue-50 text-blue-700",
  amber: "bg-amber-50 text-amber-700",
  red: "bg-red-50 text-red-700",
  slate: "bg-slate-100 text-slate-600",
};

const STATUS_TONES: Record<string, keyof typeof TONE_CLASSES> = {
  new: "blue",
  contacted: "blue",
  estimating: "amber",
  qualified: "amber",
  won: "green",
  lost: "red",
  draft: "slate",
  pre_construction: "amber",
  active: "green",
  suspended: "red",
  completed: "blue",
  archived: "slate",
  open: "slate",
  in_progress: "amber",
  done: "green",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={cn(
        "inline-block rounded-full px-2.5 py-0.5 text-xs font-medium",
        TONE_CLASSES[STATUS_TONES[status] ?? "slate"]
      )}
    >
      {labelFor(status)}
    </span>
  );
}
```

- [ ] **Step 5: Formatting helpers.** `frontend/lib/format.ts`:

```ts
export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  // Date-only strings (YYYY-MM-DD) must not shift across timezones — parse
  // the parts, don't hand the string to Date's UTC-assuming parser.
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const date = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function formatCurrency(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(numeric)) return String(value);
  return numeric.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}
```

- [ ] **Step 6: Route-handler helpers.** `frontend/lib/api/handler-utils.ts` (DRY for the ~13 proxy handlers; server-only since it's only imported by Route Handlers alongside client.ts):

```ts
import "server-only";

import { NextRequest, NextResponse } from "next/server";
import { ApiError } from "./client";

// The raw "Bearer <token>" header value, passed through to apiFetch's
// accessToken option after stripping the scheme. null → caller should 401.
export function bearerToken(request: NextRequest): string | null {
  const header = request.headers.get("Authorization");
  if (!header?.startsWith("Bearer ")) return null;
  return header.slice(7);
}

export function missingTokenResponse(): NextResponse {
  return NextResponse.json({ detail: "Missing access token" }, { status: 401 });
}

export function errorResponse(err: unknown, fallback: string): NextResponse {
  if (err instanceof ApiError) {
    return NextResponse.json({ detail: err.detail }, { status: err.status });
  }
  return NextResponse.json({ detail: fallback }, { status: 502 });
}
```

- [ ] **Step 7: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add frontend/components/ui/select.tsx frontend/components/ui/textarea.tsx frontend/components/ui/status-badge.tsx frontend/lib/state-machines.ts frontend/lib/format.ts frontend/lib/api/handler-utils.ts
git commit -m "feat: select/textarea primitives, status badges, state-machine mirrors, handler helpers"
```

---

### Task 10: Frontend — leads BFF Route Handlers

**Files:**
- Create: `frontend/app/(app)/api/leads/route.ts`
- Create: `frontend/app/(app)/api/leads/[id]/route.ts`
- Create: `frontend/app/(app)/api/leads/[id]/communications/route.ts`

- [ ] **Step 1: List + create.** `frontend/app/(app)/api/leads/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/leads", "get", {
      accessToken: token,
      query: {
        status: request.nextUrl.searchParams.get("status") ?? undefined,
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
        limit: request.nextUrl.searchParams.get("limit") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load leads");
  }
}

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/leads", "post", { accessToken: token, body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create lead");
  }
}
```

- [ ] **Step 2: Get + patch.** `frontend/app/(app)/api/leads/[id]/route.ts` (Next.js 16: `params` is a Promise):

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/leads/{lead_id}", "get", {
      accessToken: token,
      params: { lead_id: id },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load lead");
  }
}

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/leads/{lead_id}", "patch", {
      accessToken: token,
      params: { lead_id: id },
      body,
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to update lead");
  }
}
```

**Note:** if `apiFetch`'s `Method` type union lacks `"patch"`, extend it in `frontend/lib/api/client.ts` (`type Method = "get" | "post" | "put" | "patch" | "delete";`) as part of this task.

- [ ] **Step 3: Communications.** `frontend/app/(app)/api/leads/[id]/communications/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/leads/{lead_id}/communications", "get", {
      accessToken: token,
      params: { lead_id: id },
      query: { cursor: request.nextUrl.searchParams.get("cursor") ?? undefined },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load communications");
  }
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  const body = await request.json();
  try {
    const data = await apiFetch("/leads/{lead_id}/communications", "post", {
      accessToken: token,
      params: { lead_id: id },
      body,
    });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to log communication");
  }
}
```

- [ ] **Step 4: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add "frontend/app/(app)/api/leads"
git commit -m "feat: leads BFF route handlers"
```

---

### Task 10.5: Backend — company members list (spec-gap fix)

**Spec-gap note:** spec Decision 5 requires assignee pickers on tasks, but no endpoint anywhere lists a company's members — `assignee_id` would be un-pickable in the UI and My Tasks would stay empty in practice. This task closes that gap; it was surfaced during plan writing, after spec approval.

**Files:**
- Modify: `backend/app/schemas/company.py`
- Modify: `backend/app/routers/companies.py`
- Test: `backend/tests/test_company_members.py` (new)

- [ ] **Step 1: Write the failing test** — create `backend/tests/test_company_members.py` (copy `_register_and_login`/`_invite_and_login_as` verbatim from `tests/test_phases_tasks.py`):

```python
"""GET /companies/members — the assignee-picker data source (CRM+PM
frontend plan Task 10.5, a spec-gap fix: tasks have assignee_id but no
endpoint listed who could be assigned)."""
import asyncpg

from tests.conftest import TEST_DATABASE_URL

OWNER_DSN = TEST_DATABASE_URL.replace("+asyncpg", "")

# [copy _register_and_login and _invite_and_login_as here verbatim from
#  tests/test_phases_tasks.py]


async def test_members_lists_company_users_with_names_and_roles(client):
    admin = await _register_and_login(client, "Members Co", "members-admin@acme.test")
    await _invite_and_login_as(client, admin, "field_crew", "members-crew@acme.test")

    listed = await client.get("/companies/members", headers=admin["headers"])
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    by_email = {m["email"]: m for m in items}
    assert by_email["members-admin@acme.test"]["role"] == "admin"
    assert by_email["members-crew@acme.test"]["role"] == "field_crew"
    assert by_email["members-crew@acme.test"]["full_name"] == "Invited User"
    assert "user_id" in by_email["members-crew@acme.test"]


async def test_members_denied_for_field_crew(client):
    admin = await _register_and_login(client, "Members Deny Co", "members-deny@acme.test")
    crew = await _invite_and_login_as(client, admin, "field_crew", "members-deny-crew@acme.test")
    response = await client.get("/companies/members", headers=crew["headers"])
    assert response.status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_company_members.py -v`
Expected: FAIL (422 — `/companies/members` currently falls into `GET /companies/{company_id}`'s UUID path param).

- [ ] **Step 3: Add the schema.** In `backend/app/schemas/company.py` (match the file's existing imports — `uuid`, `BaseModel`):

```python
class CompanyMemberResponse(BaseModel):
    """One row of GET /companies/members — the task assignee picker's data
    source. user_id (not `id`) deliberately: this is a membership view, and
    the value callers need is exactly what tasks.assignee_id stores."""

    user_id: uuid.UUID
    full_name: str
    email: str
    role: str


class CompanyMemberListResponse(BaseModel):
    """Not paginated: a company's member count is seat-bounded (billing's
    included_seats model), far below any size needing cursors."""

    items: list[CompanyMemberResponse]
```

- [ ] **Step 4: Add the route.** In `backend/app/routers/companies.py`, **declared ABOVE the existing `GET /{company_id}` route** — FastAPI matches routes in declaration order, and a literal `/members` segment declared after the UUID path-param route would be swallowed by it (422 UUID parse error):

```python
_MEMBER_LIST_ROLES = ("admin", "project_manager")


@router.get("/members", response_model=CompanyMemberListResponse)
async def list_company_members(
    current: CurrentUser = Depends(require_role(*_MEMBER_LIST_ROLES)),
) -> CompanyMemberListResponse:
    """Members of the caller's active tenant, for task-assignee pickers.
    company_users' RLS scopes rows to the active tenant; the explicit
    company_id filter narrows a parent-company session (which can see
    descendant memberships) to the active tenant only — an assignee picker
    should offer this company's people, not the whole subtree's."""
    result = await current.session.execute(
        select(CompanyUser, User.full_name, User.email)
        .join(User, CompanyUser.user_id == User.id)
        .where(CompanyUser.company_id == current.company_id)
        .order_by(User.full_name, User.email)
    )
    return CompanyMemberListResponse(
        items=[
            CompanyMemberResponse(
                user_id=membership.user_id,
                full_name=full_name,
                email=email,
                role=membership.role,
            )
            for membership, full_name, email in result.all()
        ]
    )
```

Match the file's actual imports/context first: add `CompanyUser`, `User` to the models import and the two new schemas to the schema import. **Check how the file's existing handlers reference the active tenant** (`current.company_id` vs another attribute) and use that exact attribute.

- [ ] **Step 5: Run the new tests plus the companies coverage**

Run: `cd backend && .venv\Scripts\python.exe -m pytest tests/test_company_members.py -v`, then the file covering `GET /companies/{id}` (find it with `grep -rl "companies/" backend/tests` — likely `test_tenant_isolation.py`/`test_companies*.py`).
Expected: all pass (including proof the literal route didn't break the UUID route).

- [ ] **Step 6: Regenerate frontend types** (so `/companies/members` is typed): with the worktree stack up and the backend container rebuilt, `cd frontend && npm run generate:api-types`.

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/company.py backend/app/routers/companies.py backend/tests/test_company_members.py frontend/lib/api/types.ts
git commit -m "feat: company members list - the task assignee picker's data source"
```

---

### Task 11: Frontend — leads list + create pages

**Files:**
- Create: `frontend/components/leads/LeadForm.tsx`
- Create: `frontend/app/(app)/leads/page.tsx`
- Create: `frontend/app/(app)/leads/new/page.tsx`

- [ ] **Step 1: Shared lead form (create + edit).** `frontend/components/leads/LeadForm.tsx`:

```tsx
"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

export interface LeadFormValues {
  contact_name: string;
  project_name: string;
  email: string;
  phone: string;
  project_type: string;
  estimated_value: string;
  notes: string;
}

export const EMPTY_LEAD_FORM: LeadFormValues = {
  contact_name: "",
  project_name: "",
  email: "",
  phone: "",
  project_type: "",
  estimated_value: "",
  notes: "",
};

// Serializes form values into the backend's request shape: empty optional
// strings become null rather than "" (the backend's validators reject empty
// strings on length-floored fields).
export function leadPayload(values: LeadFormValues) {
  return {
    contact_name: values.contact_name,
    project_name: values.project_name,
    email: values.email,
    phone: values.phone || null,
    project_type: values.project_type,
    estimated_value: values.estimated_value || null,
    notes: values.notes || null,
  };
}

export function LeadForm({
  initial,
  submitLabel,
  onSubmit,
  submitting,
  error,
}: {
  initial: LeadFormValues;
  submitLabel: string;
  onSubmit: (values: LeadFormValues) => void;
  submitting: boolean;
  error: string | null;
}) {
  const [values, setValues] = React.useState<LeadFormValues>(initial);

  function set<K extends keyof LeadFormValues>(key: K) {
    return (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      setValues((v) => ({ ...v, [key]: e.target.value }));
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit(values);
      }}
      className="flex flex-col gap-4 w-full max-w-md"
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="contact_name">Contact name</Label>
        <Input id="contact_name" value={values.contact_name} onChange={set("contact_name")} disabled={submitting} required minLength={2} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="project_name">Project name</Label>
        <Input id="project_name" value={values.project_name} onChange={set("project_name")} disabled={submitting} required />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="email">Email</Label>
        <Input id="email" type="email" value={values.email} onChange={set("email")} disabled={submitting} required />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="phone">Phone (optional)</Label>
        <Input id="phone" value={values.phone} onChange={set("phone")} disabled={submitting} maxLength={20} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="project_type">Project type</Label>
        <Input id="project_type" value={values.project_type} onChange={set("project_type")} disabled={submitting} required placeholder="Remodel, new build, addition…" />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="estimated_value">Estimated value (optional)</Label>
        <Input id="estimated_value" type="number" min="0" step="0.01" value={values.estimated_value} onChange={set("estimated_value")} disabled={submitting} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="notes">Notes (optional)</Label>
        <Textarea id="notes" value={values.notes} onChange={set("notes")} disabled={submitting} />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <Button type="submit" disabled={submitting}>
        {submitLabel}
      </Button>
    </form>
  );
}
```

- [ ] **Step 2: Leads list page.** `frontend/app/(app)/leads/page.tsx`:

```tsx
"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { LEAD_STATUSES, labelFor } from "@/lib/state-machines";
import { formatCurrency, formatDate } from "@/lib/format";

interface Lead {
  id: string;
  contact_name: string;
  project_name: string;
  status: string;
  estimated_value: string | null;
  created_at: string;
}

export default function LeadsPage() {
  const { accessToken } = useAuth();
  const [leads, setLeads] = React.useState<Lead[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [statusFilter, setStatusFilter] = React.useState("");
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken) return;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (statusFilter) params.set("status", statusFilter);
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/leads?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load leads");
          return;
        }
        setLeads((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        setError("Unable to reach the server. Check your connection and try again.");
      } finally {
        setLoading(false);
      }
    },
    [accessToken, statusFilter]
  );

  React.useEffect(() => {
    load(null, true);
  }, [load]);

  return (
    <main className="p-6 flex flex-col gap-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Leads</h1>
        <Link href="/leads/new">
          <Button>New lead</Button>
        </Link>
      </div>
      <div className="flex items-center gap-2">
        <Select
          aria-label="Filter by status"
          className="w-44"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
        >
          <option value="">All statuses</option>
          {LEAD_STATUSES.map((s) => (
            <option key={s} value={s}>
              {labelFor(s)}
            </option>
          ))}
        </Select>
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && leads.length === 0 && !error && (
        <p className="text-sm text-slate-600">No leads yet — create your first lead.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {leads.map((lead) => (
          <li key={lead.id}>
            <Link href={`/leads/${lead.id}`} className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50">
              <span className="flex-1">
                <span className="block text-sm font-medium">{lead.contact_name}</span>
                <span className="block text-sm text-slate-600">{lead.project_name}</span>
              </span>
              <span className="text-sm text-slate-600">{formatCurrency(lead.estimated_value)}</span>
              <span className="text-sm text-slate-500">{formatDate(lead.created_at)}</span>
              <StatusBadge status={lead.status} />
            </Link>
          </li>
        ))}
      </ul>
      {nextCursor && (
        <Button variant="outline" onClick={() => load(nextCursor, false)} disabled={loading}>
          Load more
        </Button>
      )}
    </main>
  );
}
```

- [ ] **Step 3: New-lead page.** `frontend/app/(app)/leads/new/page.tsx`:

```tsx
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { EMPTY_LEAD_FORM, LeadForm, leadPayload, LeadFormValues } from "@/components/leads/LeadForm";

export default function NewLeadPage() {
  const router = useRouter();
  const { accessToken } = useAuth();
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function handleSubmit(values: LeadFormValues) {
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/leads", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify(leadPayload(values)),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create lead");
        return;
      }
      router.push(`/leads/${data.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="p-6 flex flex-col gap-4">
      <h1 className="text-xl font-semibold">New lead</h1>
      <LeadForm initial={EMPTY_LEAD_FORM} submitLabel="Create lead" onSubmit={handleSubmit} submitting={submitting} error={error} />
    </main>
  );
}
```

- [ ] **Step 4: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add frontend/components/leads "frontend/app/(app)/leads"
git commit -m "feat: leads list and create screens"
```

---

### Task 12: Frontend — lead detail (pipeline, transitions, edit, communication log)

**Files:**
- Create: `frontend/components/leads/LeadStatusPipeline.tsx`
- Create: `frontend/components/leads/CommunicationLog.tsx`
- Create: `frontend/app/(app)/leads/[id]/page.tsx`

- [ ] **Step 1: Pipeline breadcrumb.** `frontend/components/leads/LeadStatusPipeline.tsx`:

```tsx
import { cn } from "@/lib/utils";
import { LEAD_PIPELINE, labelFor } from "@/lib/state-machines";

export function LeadStatusPipeline({ status }: { status: string }) {
  if (status === "lost") {
    return <p className="text-sm text-red-600 font-medium">This lead was marked lost.</p>;
  }
  const currentIndex = LEAD_PIPELINE.indexOf(status as (typeof LEAD_PIPELINE)[number]);
  return (
    <ol className="flex items-center gap-2 text-xs" aria-label="Lead pipeline">
      {LEAD_PIPELINE.map((stage, index) => (
        <li key={stage} className="flex items-center gap-2">
          {index > 0 && <span aria-hidden="true" className="text-slate-300">→</span>}
          <span
            className={cn(
              index === currentIndex ? "font-semibold text-blue-700" : "text-slate-400"
            )}
            aria-current={index === currentIndex ? "step" : undefined}
          >
            {labelFor(stage)}
          </span>
        </li>
      ))}
    </ol>
  );
}
```

- [ ] **Step 2: Communication log.** `frontend/components/leads/CommunicationLog.tsx`:

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";

const CHANNELS = ["call", "email", "note", "sms"] as const;
const CHANNEL_LABELS: Record<string, string> = { call: "Call", email: "Email", note: "Note", sms: "SMS" };

interface Entry {
  id: string;
  channel: string;
  body: string;
  created_at: string;
}

export function CommunicationLog({ leadId }: { leadId: string }) {
  const { accessToken } = useAuth();
  const [entries, setEntries] = React.useState<Entry[]>([]);
  const [channel, setChannel] = React.useState<string>("call");
  const [body, setBody] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/leads/${leadId}/communications`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (response.ok) setEntries(data.items);
    } catch {
      // Non-blocking: the log section shows empty; the add-form's own error
      // handling covers the interactive path.
    }
  }, [accessToken, leadId]);

  React.useEffect(() => {
    load();
  }, [load]);

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/leads/${leadId}/communications`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ channel, body }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to log communication");
        return;
      }
      setBody("");
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-lg font-semibold">Communication log</h2>
      <form onSubmit={handleAdd} className="flex gap-2">
        <Select aria-label="Channel" className="w-28" value={channel} onChange={(e) => setChannel(e.target.value)} disabled={submitting}>
          {CHANNELS.map((c) => (
            <option key={c} value={c}>
              {CHANNEL_LABELS[c]}
            </option>
          ))}
        </Select>
        <Input
          aria-label="Communication summary"
          placeholder="Log a call, email, or note"
          className="flex-1"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          disabled={submitting}
          required
        />
        <Button type="submit" disabled={submitting}>
          Add
        </Button>
      </form>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {entries.length === 0 && <p className="text-sm text-slate-600">No communications logged yet.</p>}
      <ul className="flex flex-col gap-3">
        {entries.map((entry) => (
          <li key={entry.id} className="border-b border-slate-200 pb-3">
            <div className="flex justify-between text-xs">
              <span className="font-medium">{CHANNEL_LABELS[entry.channel] ?? entry.channel}</span>
              <span className="text-slate-500">{new Date(entry.created_at).toLocaleString()}</span>
            </div>
            <p className="mt-1 text-sm text-slate-700">{entry.body}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
```

- [ ] **Step 3: Lead detail page.** `frontend/app/(app)/leads/[id]/page.tsx`:

```tsx
"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { LeadStatusPipeline } from "@/components/leads/LeadStatusPipeline";
import { CommunicationLog } from "@/components/leads/CommunicationLog";
import { LeadForm, leadPayload, LeadFormValues } from "@/components/leads/LeadForm";
import { LEAD_TRANSITIONS, labelFor } from "@/lib/state-machines";
import { formatCurrency } from "@/lib/format";

interface Lead {
  id: string;
  contact_name: string;
  project_name: string;
  email: string;
  phone: string | null;
  status: string;
  estimated_value: string | null;
  project_type: string;
  notes: string | null;
}

export default function LeadDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { accessToken } = useAuth();
  const [lead, setLead] = React.useState<Lead | null>(null);
  const [editing, setEditing] = React.useState(false);
  const [wonBanner, setWonBanner] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/leads/${id}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load lead");
        return;
      }
      setLead(data);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, id]);

  React.useEffect(() => {
    load();
  }, [load]);

  async function patchLead(body: unknown, onSuccess?: (updated: Lead) => void) {
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/leads/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to update lead");
        return;
      }
      setLead(data);
      onSuccess?.(data);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (!lead) {
    return (
      <main className="p-6">
        {error ? (
          <p role="alert" className="text-sm text-red-600">{error}</p>
        ) : (
          <p className="text-sm text-slate-500">Loading…</p>
        )}
      </main>
    );
  }

  const nextStatuses = LEAD_TRANSITIONS[lead.status] ?? [];

  return (
    <main className="p-6 flex flex-col gap-6 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{lead.contact_name}</h1>
        <StatusBadge status={lead.status} />
      </div>
      <p className="text-sm text-slate-600 -mt-4">
        {lead.project_name} · {lead.project_type} · {formatCurrency(lead.estimated_value)} · {lead.email}
        {lead.phone ? ` · ${lead.phone}` : ""}
      </p>

      <LeadStatusPipeline status={lead.status} />

      {wonBanner && (
        <p className="text-sm text-green-800 bg-green-50 border border-green-200 rounded-md p-3">
          Lead won — a draft project was created automatically.{" "}
          <Link href="/projects" className="underline">
            Open projects
          </Link>{" "}
          to set its site address and get it moving.
        </p>
      )}

      {nextStatuses.length > 0 && (
        <div className="flex gap-2">
          {nextStatuses.map((next) => (
            <Button
              key={next}
              variant={next === "lost" ? "outline" : undefined}
              disabled={submitting}
              onClick={() =>
                patchLead({ status: next }, (updated) => {
                  if (updated.status === "won") setWonBanner(true);
                })
              }
            >
              Mark {labelFor(next).toLowerCase()}
            </Button>
          ))}
        </div>
      )}

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      <div>
        <Button variant="outline" size="sm" onClick={() => setEditing((v) => !v)}>
          {editing ? "Close edit" : "Edit details"}
        </Button>
      </div>
      {editing && (
        <LeadForm
          initial={{
            contact_name: lead.contact_name,
            project_name: lead.project_name,
            email: lead.email,
            phone: lead.phone ?? "",
            project_type: lead.project_type,
            estimated_value: lead.estimated_value ?? "",
            notes: lead.notes ?? "",
          }}
          submitLabel="Save changes"
          submitting={submitting}
          error={null}
          onSubmit={(values: LeadFormValues) => {
            patchLead(leadPayload(values), () => setEditing(false));
          }}
        />
      )}

      <CommunicationLog leadId={lead.id} />
    </main>
  );
}
```

**Note on `Button` variants:** Foundation's `components/ui/button.tsx` uses class-variance-authority — confirm the default variant's handling before finishing (omitting `variant` gives the default style; passing `variant={undefined}` is equivalent). Adjust the transition buttons if the actual variant union differs.

- [ ] **Step 4: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add frontend/components/leads "frontend/app/(app)/leads"
git commit -m "feat: lead detail - pipeline, transitions, editing, communication log"
```

---

### Task 13: Frontend — PM + dashboard + members BFF Route Handlers

**Files (all new):**
- `frontend/app/(app)/api/projects/route.ts` (GET, POST)
- `frontend/app/(app)/api/projects/[id]/route.ts` (GET, PATCH)
- `frontend/app/(app)/api/projects/[id]/status/route.ts` (PATCH)
- `frontend/app/(app)/api/projects/[id]/phases/route.ts` (GET, POST)
- `frontend/app/(app)/api/projects/[id]/tasks/route.ts` (POST)
- `frontend/app/(app)/api/tasks/[id]/route.ts` (PATCH)
- `frontend/app/(app)/api/my-tasks/route.ts` (GET)
- `frontend/app/(app)/api/projects/[id]/documents/route.ts` (GET, POST multipart)
- `frontend/app/(app)/api/projects/[id]/documents/[docId]/download/route.ts` (GET stream)
- `frontend/app/(app)/api/projects/[id]/daily-logs/route.ts` (GET, POST)
- `frontend/app/(app)/api/dashboard/summary/route.ts` (GET)
- `frontend/app/(app)/api/companies/members/route.ts` (GET)

- [ ] **Step 1: JSON proxies.** Every JSON handler follows Task 10's exact pattern (bearerToken → apiFetch → errorResponse). The list/create pair in full:

`frontend/app/(app)/api/projects/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  try {
    const data = await apiFetch("/projects", "get", {
      accessToken: token,
      query: {
        status: request.nextUrl.searchParams.get("status") ?? undefined,
        cursor: request.nextUrl.searchParams.get("cursor") ?? undefined,
        limit: request.nextUrl.searchParams.get("limit") ?? undefined,
      },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load projects");
  }
}

export async function POST(request: NextRequest) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const body = await request.json();
  try {
    const data = await apiFetch("/projects", "post", { accessToken: token, body });
    return NextResponse.json(data, { status: 201 });
  } catch (err) {
    return errorResponse(err, "Failed to create project");
  }
}
```

The rest are the same shape — write each file with the listed methods and backend paths (path params filled from the awaited `params` promise, query strings forwarded on GETs where noted):

- `projects/[id]/route.ts`: GET → `/projects/{project_id}`; PATCH → same path.
- `projects/[id]/status/route.ts`: PATCH → `/projects/{project_id}/status`.
- `projects/[id]/phases/route.ts`: GET → `/projects/{project_id}/phases`; POST → same path (201).
- `projects/[id]/tasks/route.ts`: POST → `/projects/{project_id}/tasks` (201).
- `tasks/[id]/route.ts`: PATCH → `/tasks/{task_id}`.
- `my-tasks/route.ts`: GET → `/tasks` with `query: { assignee: "me" }`.
- `projects/[id]/daily-logs/route.ts`: GET → `/projects/{project_id}/daily-logs` (forward `cursor`); POST → same path (201).
- `dashboard/summary/route.ts`: GET → `/dashboard/summary`.
- `companies/members/route.ts`: GET → `/companies/members`.

- [ ] **Step 2: Document handlers (multipart + stream — cannot use JSON-only `apiFetch`).** `frontend/app/(app)/api/projects/[id]/documents/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { apiFetch, BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, errorResponse, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  try {
    const data = await apiFetch("/projects/{project_id}/documents", "get", {
      accessToken: token,
      params: { project_id: id },
      query: { cursor: request.nextUrl.searchParams.get("cursor") ?? undefined },
    });
    return NextResponse.json(data);
  } catch (err) {
    return errorResponse(err, "Failed to load documents");
  }
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id } = await params;
  // Multipart pass-through: apiFetch is JSON-only, so this handler talks to
  // the backend directly (still server-side — the BFF boundary holds; the
  // browser never reaches the backend origin).
  const formData = await request.formData();
  try {
    const response = await fetch(`${BACKEND_API_URL}/projects/${encodeURIComponent(id)}/documents`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: formData,
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch {
    return NextResponse.json({ detail: "Failed to upload document" }, { status: 502 });
  }
}
```

`frontend/app/(app)/api/projects/[id]/documents/[docId]/download/route.ts`:

```ts
import { NextRequest, NextResponse } from "next/server";
import { BACKEND_API_URL } from "@/lib/api/client";
import { bearerToken, missingTokenResponse } from "@/lib/api/handler-utils";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string; docId: string }> }
) {
  const token = bearerToken(request);
  if (!token) return missingTokenResponse();
  const { id, docId } = await params;
  try {
    const upstream = await fetch(
      `${BACKEND_API_URL}/projects/${encodeURIComponent(id)}/documents/${encodeURIComponent(docId)}/download`,
      { headers: { Authorization: `Bearer ${token}` } }
    );
    if (!upstream.ok) {
      let detail = "Download failed";
      try {
        detail = (await upstream.json()).detail ?? detail;
      } catch {}
      return NextResponse.json({ detail }, { status: upstream.status });
    }
    // Stream the body through, preserving the filename the backend chose.
    return new NextResponse(upstream.body, {
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") ?? "application/octet-stream",
        "Content-Disposition": upstream.headers.get("Content-Disposition") ?? "attachment",
      },
    });
  } catch {
    return NextResponse.json({ detail: "Download failed" }, { status: 502 });
  }
}
```

- [ ] **Step 3: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add "frontend/app/(app)/api"
git commit -m "feat: PM, dashboard, and members BFF route handlers"
```

---

### Task 14: Frontend — projects list/create pages + role-aware Nav

**Files:**
- Create: `frontend/app/(app)/projects/page.tsx`, `frontend/app/(app)/projects/new/page.tsx`
- Modify: `frontend/components/app-shell/Nav.tsx`

- [ ] **Step 1: Projects list (role-adaptive).** `frontend/app/(app)/projects/page.tsx`:

```tsx
"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDate } from "@/lib/format";

interface StaffProject {
  id: string;
  name: string;
  site_address: string;
  status: string;
  projected_start_date: string | null;
}

interface ClientProject extends StaffProject {
  phase_count: number;
  task_count: number;
  completed_task_count: number;
}

export default function ProjectsPage() {
  const { accessToken, role } = useAuth();
  const [items, setItems] = React.useState<(StaffProject | ClientProject)[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken) return;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/projects?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load projects");
          return;
        }
        setItems((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        setError("Unable to reach the server. Check your connection and try again.");
      } finally {
        setLoading(false);
      }
    },
    [accessToken]
  );

  React.useEffect(() => {
    load(null, true);
  }, [load]);

  const canCreate = role === "admin" || role === "project_manager";

  return (
    <main className="p-6 flex flex-col gap-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Projects</h1>
        {canCreate && (
          <Link href="/projects/new">
            <Button>New project</Button>
          </Link>
        )}
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && items.length === 0 && !error && (
        <p className="text-sm text-slate-600">
          {canCreate ? "No projects yet — create your first project." : "No projects yet."}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {items.map((project) => (
          <li key={project.id}>
            <Link href={`/projects/${project.id}`} className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50">
              <span className="flex-1">
                <span className="block text-sm font-medium">{project.name}</span>
                <span className="block text-sm text-slate-600">{project.site_address || "No site address yet"}</span>
              </span>
              {"task_count" in project && (
                <span className="text-sm text-slate-600">
                  {project.completed_task_count}/{project.task_count} tasks done
                </span>
              )}
              <span className="text-sm text-slate-500">{formatDate(project.projected_start_date)}</span>
              <StatusBadge status={project.status} />
            </Link>
          </li>
        ))}
      </ul>
      {nextCursor && (
        <Button variant="outline" onClick={() => load(nextCursor, false)} disabled={loading}>
          Load more
        </Button>
      )}
    </main>
  );
}
```

- [ ] **Step 2: New-project page.** `frontend/app/(app)/projects/new/page.tsx`:

```tsx
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function NewProjectPage() {
  const router = useRouter();
  const { accessToken } = useAuth();
  const [name, setName] = React.useState("");
  const [siteAddress, setSiteAddress] = React.useState("");
  const [startDate, setStartDate] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          name,
          site_address: siteAddress,
          projected_start_date: startDate || null,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create project");
        return;
      }
      router.push(`/projects/${data.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="p-6 flex flex-col gap-4">
      <h1 className="text-xl font-semibold">New project</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-md">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="name">Project name</Label>
          <Input id="name" value={name} onChange={(e) => setName(e.target.value)} disabled={submitting} required />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="site_address">Site address</Label>
          <Input id="site_address" value={siteAddress} onChange={(e) => setSiteAddress(e.target.value)} disabled={submitting} required />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="start_date">Projected start date (optional)</Label>
          <Input id="start_date" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} disabled={submitting} />
        </div>
        {error && (
          <p role="alert" aria-live="assertive" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <Button type="submit" disabled={submitting}>
          Create project
        </Button>
      </form>
    </main>
  );
}
```

- [ ] **Step 3: Role-aware Nav links.** In `frontend/components/app-shell/Nav.tsx`, pull `role` from the existing `useAuth()` destructure and add links before the existing Account link (spec Decision 5: Leads+Projects for admin/PM, Projects for accountant, My Tasks for field_crew, nothing extra for client):

```tsx
        {(role === "admin" || role === "project_manager") && (
          <Link href="/leads" className="text-sm text-slate-600 hover:text-slate-900">
            Leads
          </Link>
        )}
        {(role === "admin" || role === "project_manager" || role === "accountant") && (
          <Link href="/projects" className="text-sm text-slate-600 hover:text-slate-900">
            Projects
          </Link>
        )}
        {role === "field_crew" && (
          <Link href="/my-tasks" className="text-sm text-slate-600 hover:text-slate-900">
            My tasks
          </Link>
        )}
```

Convert the existing raw `<a href="/account">` to a `next/link` `Link` while here (same classes), adding `import Link from "next/link";` if absent.

- [ ] **Step 4: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add "frontend/app/(app)/projects" frontend/components/app-shell/Nav.tsx
git commit -m "feat: projects list/create screens and role-aware nav"
```

---

### Task 15: Frontend — project detail page (overview, status actions, client variant, tab shell)

**Files:**
- Create: `frontend/components/projects/ProjectStatusActions.tsx`
- Create: `frontend/components/projects/ClientProjectDashboard.tsx`
- Create: `frontend/app/(app)/projects/[id]/page.tsx`

- [ ] **Step 1: Status actions.** `frontend/components/projects/ProjectStatusActions.tsx`:

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { PROJECT_TRANSITIONS, labelFor } from "@/lib/state-machines";

export function ProjectStatusActions({
  projectId,
  status,
  onChanged,
}: {
  projectId: string;
  status: string;
  onChanged: () => void;
}) {
  const { accessToken, role } = useAuth();
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canTransition = role === "admin" || role === "project_manager";
  const nextStatuses = PROJECT_TRANSITIONS[status] ?? [];
  if (!canTransition || nextStatuses.length === 0) return null;

  async function transition(next: string) {
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ status: next }),
      });
      const data = await response.json();
      if (!response.ok) {
        // Includes the backend's 409 for completion blocked by pending
        // change orders — surfaced verbatim (spec Decision 6).
        setError(data.detail ?? "Failed to change status");
        return;
      }
      onChanged();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2">
        {nextStatuses.map((next) => (
          <Button key={next} variant="outline" size="sm" disabled={submitting} onClick={() => transition(next)}>
            Move to {labelFor(next).toLowerCase()}
          </Button>
        ))}
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Client dashboard card.** `frontend/components/projects/ClientProjectDashboard.tsx`:

```tsx
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDate } from "@/lib/format";

export interface ClientProjectShape {
  id: string;
  name: string;
  status: string;
  site_address: string;
  projected_start_date: string | null;
  phase_count: number;
  task_count: number;
  completed_task_count: number;
}

export function ClientProjectDashboard({ project }: { project: ClientProjectShape }) {
  return (
    <Card className="max-w-md">
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>{project.name}</CardTitle>
          <StatusBadge status={project.status} />
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm text-slate-600">
        <p>{project.site_address || "Site address pending"}</p>
        <p>Projected start: {formatDate(project.projected_start_date)}</p>
        <p>
          {project.phase_count} {project.phase_count === 1 ? "phase" : "phases"} ·{" "}
          {project.completed_task_count} of {project.task_count} tasks complete
        </p>
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 3: Detail page with tab shell.** `frontend/app/(app)/projects/[id]/page.tsx`. Tabs are client-side `useState` — no nested routes. The three content tabs land in Tasks 16–17; in THIS task their panels render a "Coming in a later task." placeholder so the page compiles and ships incrementally:

```tsx
"use client";

import * as React from "react";
import { useParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { StatusBadge } from "@/components/ui/status-badge";
import { ProjectStatusActions } from "@/components/projects/ProjectStatusActions";
import { ClientProjectDashboard, ClientProjectShape } from "@/components/projects/ClientProjectDashboard";
import { formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

interface StaffProject {
  id: string;
  lead_id: string | null;
  name: string;
  site_address: string;
  status: string;
  projected_start_date: string | null;
}

const TABS = ["Overview", "Phases & tasks", "Documents", "Daily logs"] as const;
type Tab = (typeof TABS)[number];

export default function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { accessToken } = useAuth();
  const [project, setProject] = React.useState<StaffProject | ClientProjectShape | null>(null);
  const [tab, setTab] = React.useState<Tab>("Overview");
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/projects/${id}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load project");
        return;
      }
      setProject(data);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, id]);

  React.useEffect(() => {
    load();
  }, [load]);

  if (!project) {
    return (
      <main className="p-6">
        {error ? (
          <p role="alert" className="text-sm text-red-600">{error}</p>
        ) : (
          <p className="text-sm text-slate-500">Loading…</p>
        )}
      </main>
    );
  }

  // The backend shapes the response by role: the sanitized client shape
  // carries phase/task counts and no lead_id (spec Decision 1).
  if ("phase_count" in project) {
    return (
      <main className="p-6">
        <ClientProjectDashboard project={project} />
      </main>
    );
  }

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{project.name}</h1>
        <StatusBadge status={project.status} />
      </div>
      <p className="text-sm text-slate-600 -mt-4">
        {project.site_address || "No site address yet"} · projected start {formatDate(project.projected_start_date)}
      </p>

      <ProjectStatusActions projectId={project.id} status={project.status} onChanged={load} />

      <div className="flex gap-1 border-b border-slate-200" role="tablist">
        {TABS.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={cn(
              "px-3 py-2 text-sm",
              tab === t
                ? "border-b-2 border-blue-600 font-medium text-slate-900"
                : "text-slate-600 hover:text-slate-900"
            )}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "Overview" && <OverviewTab project={project} onSaved={load} />}
      {tab === "Phases & tasks" && <p className="text-sm text-slate-500">Coming in a later task.</p>}
      {tab === "Documents" && <p className="text-sm text-slate-500">Coming in a later task.</p>}
      {tab === "Daily logs" && <p className="text-sm text-slate-500">Coming in a later task.</p>}
    </main>
  );
}

function OverviewTab({ project, onSaved }: { project: StaffProject; onSaved: () => void }) {
  const { accessToken, role } = useAuth();
  const [name, setName] = React.useState(project.name);
  const [siteAddress, setSiteAddress] = React.useState(project.site_address);
  const [startDate, setStartDate] = React.useState(project.projected_start_date ?? "");
  const [submitting, setSubmitting] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canEdit = role === "admin" || role === "project_manager";

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSaved(false);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${project.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ name, site_address: siteAddress, projected_start_date: startDate || null }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to save project");
        return;
      }
      setSaved(true);
      onSaved();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (!canEdit) {
    return (
      <p className="text-sm text-slate-600">
        {project.site_address || "No site address yet"} · projected start {formatDate(project.projected_start_date)}
      </p>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-md">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ov-name">Project name</Label>
        <Input id="ov-name" value={name} onChange={(e) => setName(e.target.value)} disabled={submitting} required />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ov-site">Site address</Label>
        <Input id="ov-site" value={siteAddress} onChange={(e) => setSiteAddress(e.target.value)} disabled={submitting} required />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ov-start">Projected start date</Label>
        <Input id="ov-start" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} disabled={submitting} />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {saved && <p className="text-sm text-green-700">Saved.</p>}
      <Button type="submit" disabled={submitting}>
        Save changes
      </Button>
    </form>
  );
}
```

- [ ] **Step 4: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add frontend/components/projects "frontend/app/(app)/projects"
git commit -m "feat: project detail - overview, status actions, client dashboard variant"
```

---

### Task 16: Frontend — phases & tasks tab

**Files:**
- Create: `frontend/components/projects/PhasesTasksTab.tsx`
- Modify: `frontend/app/(app)/projects/[id]/page.tsx` (replace the "Phases & tasks" stub)

- [ ] **Step 1: The tab component.** `frontend/components/projects/PhasesTasksTab.tsx`:

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { TASK_STATUSES, labelFor } from "@/lib/state-machines";
import { formatDate } from "@/lib/format";

interface Task {
  id: string;
  name: string;
  assignee_id: string | null;
  due_date: string | null;
  status: string;
}

interface Phase {
  id: string;
  name: string;
  sequence: number;
  tasks: Task[];
}

interface Member {
  user_id: string;
  full_name: string;
  role: string;
}

export function PhasesTasksTab({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [phases, setPhases] = React.useState<Phase[]>([]);
  const [members, setMembers] = React.useState<Member[]>([]);
  const [expanded, setExpanded] = React.useState<Record<string, boolean>>({});
  const [newPhaseName, setNewPhaseName] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canEdit = role === "admin" || role === "project_manager";
  const authHeaders = React.useMemo(
    () => ({ Authorization: `Bearer ${accessToken}` }),
    [accessToken]
  );

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/projects/${projectId}/phases`, { headers: authHeaders });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load phases");
        return;
      }
      setPhases(data.items);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, authHeaders, projectId]);

  React.useEffect(() => {
    load();
  }, [load]);

  React.useEffect(() => {
    if (!accessToken || !canEdit) return;
    fetch("/api/companies/members", { headers: authHeaders })
      .then(async (r) => {
        const data = await r.json();
        if (r.ok) setMembers(data.items);
      })
      .catch(() => {});
  }, [accessToken, authHeaders, canEdit]);

  async function addPhase(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/phases`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({ name: newPhaseName, sequence: phases.length + 1 }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to add phase");
        return;
      }
      setNewPhaseName("");
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function patchTask(taskId: string, body: Record<string, unknown>) {
    if (!accessToken) return;
    setError(null);
    try {
      const response = await fetch(`/api/tasks/${taskId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to update task");
        return;
      }
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }

  const memberName = (userId: string | null) =>
    userId ? members.find((m) => m.user_id === userId)?.full_name ?? "Assigned" : "Unassigned";

  return (
    <section className="flex flex-col gap-4">
      {canEdit && (
        <form onSubmit={addPhase} className="flex gap-2">
          <Input
            aria-label="New phase name"
            placeholder="New phase name"
            className="max-w-xs"
            value={newPhaseName}
            onChange={(e) => setNewPhaseName(e.target.value)}
            disabled={submitting}
            required
          />
          <Button type="submit" disabled={submitting}>
            Add phase
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {phases.length === 0 && <p className="text-sm text-slate-600">No phases yet.</p>}
      {phases.map((phase) => {
        const isOpen = expanded[phase.id] ?? true;
        const done = phase.tasks.filter((t) => t.status === "done").length;
        return (
          <div key={phase.id} className="border border-slate-200 rounded-lg overflow-hidden">
            <button
              type="button"
              className="w-full flex items-center justify-between bg-slate-50 px-4 py-3 text-sm font-medium"
              aria-expanded={isOpen}
              onClick={() => setExpanded((prev) => ({ ...prev, [phase.id]: !isOpen }))}
            >
              <span>{phase.name}</span>
              <span className="text-xs text-slate-500">
                {phase.tasks.length} tasks · {done} done
              </span>
            </button>
            {isOpen && (
              <div className="px-4 pb-3">
                {phase.tasks.map((task) => (
                  <div key={task.id} className="flex items-center gap-3 border-t border-slate-200 py-2 text-sm">
                    <span className="flex-1">{task.name}</span>
                    <span className="text-slate-500 text-xs">{memberName(task.assignee_id)}</span>
                    <span className="text-slate-500 text-xs">{formatDate(task.due_date)}</span>
                    {canEdit || role === "field_crew" ? (
                      <Select
                        aria-label={`Status for ${task.name}`}
                        className="w-32 h-8"
                        value={task.status}
                        onChange={(e) => patchTask(task.id, { status: e.target.value })}
                      >
                        {TASK_STATUSES.map((s) => (
                          <option key={s} value={s}>
                            {labelFor(s)}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <StatusBadge status={task.status} />
                    )}
                  </div>
                ))}
                {canEdit && <NewTaskRow phaseId={phase.id} projectId={projectId} members={members} onCreated={load} />}
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}

function NewTaskRow({
  phaseId,
  projectId,
  members,
  onCreated,
}: {
  phaseId: string;
  projectId: string;
  members: Member[];
  onCreated: () => void;
}) {
  const { accessToken } = useAuth();
  const [name, setName] = React.useState("");
  const [dueDate, setDueDate] = React.useState("");
  const [assignee, setAssignee] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          name,
          phase_id: phaseId,
          due_date: dueDate || null,
          assignee_id: assignee || null,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to add task");
        return;
      }
      setName("");
      setDueDate("");
      setAssignee("");
      onCreated();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-wrap items-center gap-2 border-t border-slate-200 pt-3 mt-1">
      <Input
        aria-label="New task name"
        placeholder="New task"
        className="flex-1 min-w-40 h-8"
        value={name}
        onChange={(e) => setName(e.target.value)}
        disabled={submitting}
        required
      />
      <Input aria-label="Due date" type="date" className="w-36 h-8" value={dueDate} onChange={(e) => setDueDate(e.target.value)} disabled={submitting} />
      <Select aria-label="Assignee" className="w-40 h-8" value={assignee} onChange={(e) => setAssignee(e.target.value)} disabled={submitting}>
        <option value="">Unassigned</option>
        {members.map((m) => (
          <option key={m.user_id} value={m.user_id}>
            {m.full_name}
          </option>
        ))}
      </Select>
      <Button type="submit" size="sm" disabled={submitting}>
        Add task
      </Button>
      {error && (
        <p role="alert" aria-live="assertive" className="w-full text-sm text-red-600">
          {error}
        </p>
      )}
    </form>
  );
}
```

- [ ] **Step 2: Wire the tab in.** In `frontend/app/(app)/projects/[id]/page.tsx`: add `import { PhasesTasksTab } from "@/components/projects/PhasesTasksTab";` and replace the stub line with `{tab === "Phases & tasks" && <PhasesTasksTab projectId={project.id} />}`.

- [ ] **Step 3: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add frontend/components/projects "frontend/app/(app)/projects"
git commit -m "feat: phases and tasks tab - accordion, task creation, status/assignee editing"
```

---

### Task 17: Frontend — documents tab + daily logs tab

**Files:**
- Create: `frontend/components/projects/DocumentsTab.tsx`
- Create: `frontend/components/projects/DailyLogsTab.tsx`
- Modify: `frontend/app/(app)/projects/[id]/page.tsx` (replace both stubs)

- [ ] **Step 1: Documents tab.** `frontend/components/projects/DocumentsTab.tsx`:

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";

interface Doc {
  id: string;
  file_name: string;
  version: number;
  created_at: string;
}

export function DocumentsTab({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [docs, setDocs] = React.useState<Doc[]>([]);
  const [file, setFile] = React.useState<File | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement | null>(null);

  const canUpload = role === "admin" || role === "project_manager";

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/projects/${projectId}/documents`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load documents");
        return;
      }
      setDocs(data.items);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    load();
  }, [load]);

  async function handleUpload(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken || !file) return;
    setError(null);
    setSubmitting(true);
    try {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("file_name", file.name);
      const response = await fetch(`/api/projects/${projectId}/documents`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
        body: formData,
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to upload document");
        return;
      }
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDownload(doc: Doc) {
    if (!accessToken) return;
    setError(null);
    try {
      // fetch-with-bearer, then a programmatic download: a plain <a href>
      // navigation would carry no Authorization header (the access token
      // lives only in memory — Foundation's BFF session design).
      const response = await fetch(`/api/projects/${projectId}/documents/${doc.id}/download`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) {
        let detail = "Download failed";
        try {
          detail = (await response.json()).detail ?? detail;
        } catch {}
        setError(detail);
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = doc.file_name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }

  return (
    <section className="flex flex-col gap-4">
      {canUpload && (
        <form onSubmit={handleUpload} className="flex items-center gap-2">
          <input
            ref={fileInputRef}
            aria-label="Choose file"
            type="file"
            className="text-sm"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            disabled={submitting}
          />
          <Button type="submit" disabled={submitting || !file}>
            Upload
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {docs.length === 0 && <p className="text-sm text-slate-600">No documents yet.</p>}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {docs.map((doc) => (
          <li key={doc.id} className="flex items-center gap-4 px-4 py-3 text-sm">
            <span className="flex-1 font-medium">{doc.file_name}</span>
            <span className="text-slate-500">v{doc.version}</span>
            <span className="text-slate-500">{new Date(doc.created_at).toLocaleDateString()}</span>
            <Button variant="outline" size="sm" onClick={() => handleDownload(doc)}>
              Download
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}
```

- [ ] **Step 2: Daily logs tab.** `frontend/components/projects/DailyLogsTab.tsx`:

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { formatDate } from "@/lib/format";

interface DailyLog {
  id: string;
  log_date: string;
  weather: string | null;
  notes: string | null;
}

export function DailyLogsTab({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [logs, setLogs] = React.useState<DailyLog[]>([]);
  const [logDate, setLogDate] = React.useState(() => new Date().toISOString().slice(0, 10));
  const [weather, setWeather] = React.useState("");
  const [notes, setNotes] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canWrite = role === "admin" || role === "project_manager" || role === "field_crew";

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/projects/${projectId}/daily-logs`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load daily logs");
        return;
      }
      setLogs(data.items);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    load();
  }, [load]);

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/daily-logs`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ log_date: logDate, weather: weather || null, notes: notes || null }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to add daily log");
        return;
      }
      setWeather("");
      setNotes("");
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleAdd} className="flex flex-col gap-3 max-w-md">
          <div className="flex gap-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="log-date">Date</Label>
              <Input id="log-date" type="date" value={logDate} onChange={(e) => setLogDate(e.target.value)} disabled={submitting} required />
            </div>
            <div className="flex flex-col gap-1.5 flex-1">
              <Label htmlFor="log-weather">Weather (optional)</Label>
              <Input id="log-weather" value={weather} onChange={(e) => setWeather(e.target.value)} disabled={submitting} maxLength={100} />
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="log-notes">Notes</Label>
            <Textarea id="log-notes" value={notes} onChange={(e) => setNotes(e.target.value)} disabled={submitting} />
          </div>
          <Button type="submit" disabled={submitting} className="self-start">
            Add log entry
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {logs.length === 0 && <p className="text-sm text-slate-600">No daily logs yet.</p>}
      <ul className="flex flex-col gap-3">
        {logs.map((log) => (
          <li key={log.id} className="border-b border-slate-200 pb-3 text-sm">
            <div className="flex justify-between">
              <span className="font-medium">{formatDate(log.log_date)}</span>
              {log.weather && <span className="text-slate-500">{log.weather}</span>}
            </div>
            {log.notes && <p className="mt-1 text-slate-700 whitespace-pre-wrap">{log.notes}</p>}
          </li>
        ))}
      </ul>
    </section>
  );
}
```

- [ ] **Step 3: Wire both tabs in** (`projects/[id]/page.tsx`): import both, replace the two stubs with `<DocumentsTab projectId={project.id} />` and `<DailyLogsTab projectId={project.id} />`.

- [ ] **Step 4: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add frontend/components/projects "frontend/app/(app)/projects"
git commit -m "feat: documents and daily logs tabs"
```

---

### Task 18: Frontend — real dashboard + role redirects

**Files:**
- Create: `frontend/components/dashboard/SummaryCards.tsx`
- Modify: `frontend/app/(app)/dashboard/page.tsx` (full replacement of the placeholder)

- [ ] **Step 1: Summary cards.** `frontend/components/dashboard/SummaryCards.tsx`:

```tsx
"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";

interface Summary {
  open_leads: number;
  active_projects: number;
  tasks_due_this_week: number;
}

export function SummaryCards() {
  const { accessToken } = useAuth();
  const [summary, setSummary] = React.useState<Summary | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!accessToken) return;
    fetch("/api/dashboard/summary", { headers: { Authorization: `Bearer ${accessToken}` } })
      .then(async (r) => {
        const data = await r.json();
        if (!r.ok) {
          setError(data.detail ?? "Failed to load summary");
          return;
        }
        setSummary(data);
      })
      .catch(() => setError("Unable to reach the server. Check your connection and try again."));
  }, [accessToken]);

  if (error) {
    return (
      <p role="alert" className="text-sm text-red-600">
        {error}
      </p>
    );
  }

  const cards = [
    { label: "Open leads", value: summary?.open_leads },
    { label: "Active projects", value: summary?.active_projects },
    { label: "Tasks due this week", value: summary?.tasks_due_this_week },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 max-w-2xl">
      {cards.map((card) => (
        <div key={card.label} className="rounded-lg bg-slate-50 p-4">
          <p className="text-sm text-slate-600">{card.label}</p>
          <p className="text-2xl font-medium">{card.value ?? "—"}</p>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 2: Replace the dashboard page.** `frontend/app/(app)/dashboard/page.tsx` (full new content — keeps `Nav` + `decodeCompanyId` from the current file, adds role redirects and real content):

```tsx
"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Nav } from "@/components/app-shell/Nav";
import { SummaryCards } from "@/components/dashboard/SummaryCards";

export default function DashboardPage() {
  const router = useRouter();
  const { accessToken, role, isHydrating } = useAuth();

  // Role landing rules (spec Decision 3): field_crew's home is My Tasks;
  // a client's home is their project (or the sanitized projects list when
  // they have several). admin/PM/accountant stay here.
  React.useEffect(() => {
    if (isHydrating || !accessToken) return;
    if (role === "field_crew") {
      router.replace("/my-tasks");
      return;
    }
    if (role === "client") {
      (async () => {
        try {
          const response = await fetch("/api/projects", {
            headers: { Authorization: `Bearer ${accessToken}` },
          });
          const data = await response.json();
          if (response.ok && data.items.length === 1) {
            router.replace(`/projects/${data.items[0].id}`);
          } else {
            router.replace("/projects");
          }
        } catch {
          router.replace("/projects");
        }
      })();
    }
  }, [isHydrating, accessToken, role, router]);

  const isStaffDashboard = role === "admin" || role === "project_manager" || role === "accountant";

  return (
    <div>
      <Nav companyId={decodeCompanyId(accessToken)} />
      <main className="p-6 flex flex-col gap-6">
        <h1 className="text-xl font-semibold">Dashboard</h1>
        {!isStaffDashboard && <p className="text-sm text-slate-500">Loading your workspace…</p>}
        {isStaffDashboard && (
          <>
            {(role === "admin" || role === "project_manager") && <SummaryCards />}
            <div className="flex gap-4 text-sm">
              {(role === "admin" || role === "project_manager") && (
                <Link href="/leads" className="underline text-slate-700">
                  Go to leads
                </Link>
              )}
              <Link href="/projects" className="underline text-slate-700">
                Go to projects
              </Link>
            </div>
          </>
        )}
      </main>
    </div>
  );
}

function decodeCompanyId(accessToken: string | null): string {
  if (!accessToken) return "";
  try {
    const payload = JSON.parse(atob(accessToken.split(".")[1]));
    return payload.default_company_id ?? "";
  } catch {
    return "";
  }
}
```

- [ ] **Step 3: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add frontend/components/dashboard "frontend/app/(app)/dashboard/page.tsx"
git commit -m "feat: real dashboard with summary cards and role-based landing"
```

---

### Task 19: Frontend — My Tasks page

**Files:**
- Create: `frontend/app/(app)/my-tasks/page.tsx`

- [ ] **Step 1: The page.**

```tsx
"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Select } from "@/components/ui/select";
import { TASK_STATUSES, labelFor } from "@/lib/state-machines";
import { formatDate } from "@/lib/format";

interface MyTask {
  id: string;
  name: string;
  status: string;
  due_date: string | null;
  project_id: string;
  project_name: string;
}

export default function MyTasksPage() {
  const { accessToken } = useAuth();
  const [tasks, setTasks] = React.useState<MyTask[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    setLoading(true);
    try {
      const response = await fetch("/api/my-tasks", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load tasks");
        return;
      }
      setTasks(data.items);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  }, [accessToken]);

  React.useEffect(() => {
    load();
  }, [load]);

  async function setStatus(taskId: string, status: string) {
    if (!accessToken) return;
    setError(null);
    try {
      const response = await fetch(`/api/tasks/${taskId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ status }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to update task");
        return;
      }
      setTasks((prev) => prev.map((t) => (t.id === taskId ? { ...t, status } : t)));
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }

  return (
    <main className="p-6 flex flex-col gap-4 max-w-2xl">
      <h1 className="text-xl font-semibold">My tasks</h1>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && tasks.length === 0 && !error && (
        <p className="text-sm text-slate-600">No tasks assigned to you right now.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {tasks.map((task) => (
          <li key={task.id} className="flex items-center gap-4 px-4 py-3 text-sm">
            <span className="flex-1">
              <span className="block font-medium">{task.name}</span>
              <Link href={`/projects/${task.project_id}`} className="text-slate-600 hover:underline">
                {task.project_name}
              </Link>
            </span>
            <span className="text-slate-500">{formatDate(task.due_date)}</span>
            <Select
              aria-label={`Status for ${task.name}`}
              className="w-32 h-8"
              value={task.status}
              onChange={(e) => setStatus(task.id, e.target.value)}
            >
              {TASK_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {labelFor(s)}
                </option>
              ))}
            </Select>
          </li>
        ))}
      </ul>
    </main>
  );
}
```

- [ ] **Step 2: Type-check and commit**

Run: `npx tsc --noEmit` → exit 0.

```bash
git add "frontend/app/(app)/my-tasks"
git commit -m "feat: my tasks - flat cross-project assignment list with status control"
```

---

### Task 20: Playwright E2E — the CRM→PM arc

**Files:**
- Create: `frontend/e2e/crm-pm.spec.ts`

- [ ] **Step 1: The spec.**

```ts
import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";

test("lead to won to drafted project through documents and daily logs", async ({ page }) => {
  const suffix = randomUUID().slice(0, 8);
  const email = `e2e-crm-${suffix}@foundation.example`;
  const password = "correct-horse-battery-9";

  await test.step("register and land on dashboard", async () => {
    await page.goto("/register");
    await page.getByLabel("Company name").fill(`E2E CRM Co ${suffix}`);
    await page.getByLabel("Your name").fill("E2E CRM Tester");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 });
    await expect(page.getByText("Open leads")).toBeVisible({ timeout: 15_000 });
  });

  await test.step("create a lead and log a communication", async () => {
    await page.getByRole("link", { name: "Leads" }).click();
    await page.getByRole("link", { name: "New lead" }).click();
    await page.getByLabel("Contact name").fill("Ada Contact");
    await page.getByLabel("Project name").fill(`Kitchen ${suffix}`);
    await page.getByLabel("Email").fill(`ada-${suffix}@client.example`);
    await page.getByLabel("Project type").fill("Remodel");
    await page.getByRole("button", { name: "Create lead" }).click();
    await expect(page.getByRole("heading", { name: "Ada Contact" })).toBeVisible({ timeout: 15_000 });

    await page.getByLabel("Communication summary").fill("Discussed budget range");
    await page.getByRole("button", { name: "Add", exact: true }).click();
    await expect(page.getByText("Discussed budget range")).toBeVisible();
  });

  await test.step("walk the lead to won", async () => {
    for (const label of ["Mark contacted", "Mark estimating", "Mark qualified", "Mark won"]) {
      await page.getByRole("button", { name: label }).click();
      await expect(page.getByRole("button", { name: label })).toBeHidden();
    }
    await expect(page.getByText("a draft project was created automatically")).toBeVisible();
  });

  await test.step("open the drafted project and fill its site address", async () => {
    await page.getByRole("link", { name: "Open projects" }).click();
    await page.getByRole("link", { name: `Kitchen ${suffix}` }).click();
    await expect(page.getByRole("heading", { name: `Kitchen ${suffix}` })).toBeVisible({ timeout: 15_000 });
    await page.getByLabel("Site address").fill("412 Maple St");
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByText("Saved.")).toBeVisible();
  });

  await test.step("advance the project to active", async () => {
    await page.getByRole("button", { name: "Move to pre-construction" }).click();
    await expect(page.getByRole("button", { name: "Move to active" })).toBeVisible();
    await page.getByRole("button", { name: "Move to active" }).click();
    await expect(page.getByRole("button", { name: "Move to completed" })).toBeVisible();
  });

  await test.step("add a phase and a task, mark it done", async () => {
    await page.getByRole("tab", { name: "Phases & tasks" }).click();
    await page.getByLabel("New phase name").fill("Framing");
    await page.getByRole("button", { name: "Add phase" }).click();
    await expect(page.getByRole("button", { name: /Framing/ })).toBeVisible();

    await page.getByLabel("New task name").fill("Frame walls");
    await page.getByRole("button", { name: "Add task" }).click();
    await expect(page.getByText("Frame walls")).toBeVisible();

    await page.getByLabel("Status for Frame walls").selectOption("done");
    await expect(page.getByText("1 done")).toBeVisible();
  });

  await test.step("upload a document and download it back", async () => {
    await page.getByRole("tab", { name: "Documents" }).click();
    await page.getByLabel("Choose file").setInputFiles({
      name: "site-plan.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("blueprint bytes"),
    });
    await page.getByRole("button", { name: "Upload" }).click();
    await expect(page.getByText("site-plan.txt")).toBeVisible();

    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("site-plan.txt");
    // Spec Decision 8: assert the CONTENT round-trips, not just the name.
    const downloadPath = await download.path();
    expect(readFileSync(downloadPath, "utf-8")).toBe("blueprint bytes");
  });

  await test.step("add a daily log", async () => {
    await page.getByRole("tab", { name: "Daily logs" }).click();
    await page.getByLabel("Notes").fill("Poured foundation, clear skies.");
    await page.getByRole("button", { name: "Add log entry" }).click();
    await expect(page.getByText("Poured foundation, clear skies.")).toBeVisible();
  });

  await test.step("dashboard reflects the data", async () => {
    await page.goto("/dashboard");
    await expect(page.getByText("Active projects")).toBeVisible({ timeout: 15_000 });
    // The company has exactly one active project (created in this test).
    const activeCard = page.locator("div").filter({ hasText: /^Active projects/ }).last();
    await expect(activeCard).toContainText("1");
  });
});
```

- [ ] **Step 2: Run it live.** Same Docker convention as Foundation's Task 13: stop the main repo's stack if running, `docker compose up -d --build` in this worktree, wait for backend health, apply migrations if the volume is fresh, then:

```bash
cd frontend && E2E_BASE_URL=http://localhost:3001 npm run test:e2e
```

Expected: **2 passed** (the Foundation spec plus this one). First run pays `next dev` cold-compile per new route — the per-assertion 15s overrides cover it; if a first cold run still trips, re-run once warm before investigating. **Regardless of outcome**, tear down the worktree stack (`docker compose down`, no `-v`) and restart the main repo's stack.

- [ ] **Step 3: Commit**

```bash
git add frontend/e2e/crm-pm.spec.ts
git commit -m "test: E2E arc - lead to won to drafted project, documents, daily logs"
```

---

### Task 21: Lint/build, full regression, docs sync, closeout, PR

- [ ] **Step 1: Frontend lint + production build**

```bash
cd frontend && npm run lint && npm run build
```

Expected: both exit 0 (3 pre-existing marketing `<img>` warnings are fine; 0 errors). Fix any real errors surfaced by the production compile — never by disabling rules.

- [ ] **Step 2: Full backend regression**

```bash
cd backend && .venv\Scripts\python.exe -m pytest
```

Expected: full suite green (~790+ tests including this plan's additions). Never run two pytest invocations concurrently — they share the test database.

- [ ] **Step 3: Docs sync.** Add an **Implementation Status** paragraph directly under the title of `docs/superpowers/specs/2026-07-17-crm-pm-frontend-design.md` (the established convention — see the Foundation spec's for tone/format): completion statement, lint/build results, live E2E results (both specs), and deliberately-deferred items surfaced during implementation. Include the Task 10.5 spec-gap note (members endpoint added beyond the approved spec, and why).

- [ ] **Step 4: Commit the closeout**

```bash
git add docs/superpowers/specs/2026-07-17-crm-pm-frontend-design.md
git commit -m "docs: close out CRM+PM frontend implementation"
```

- [ ] **Step 5: Push + PR**

```bash
git push -u origin feature/crm-pm-frontend
```

Write the PR body to a scratch file first (embedded quotes break shell argument quoting — this exact failure has happened before in this project), then:

```bash
gh pr create --base main --head feature/crm-pm-frontend --title "feat: CRM + Project Management frontend - leads, projects, tasks, documents, daily logs" --body-file <scratch-file-path>
```

Confirm CI (backend-ci and frontend-ci) goes green. **Merging remains an explicit, separate user decision — not automatic**, matching every prior feature in this project.
