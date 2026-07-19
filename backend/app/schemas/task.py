import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.task import VALID_STATUSES


class TaskCreateRequest(BaseModel):
    """Body for `POST /projects/{id}/tasks` (Task 1.14). `phase_id` must
    belong to the same project — an application-layer check the router
    performs (same pattern as Phase 0's `create_child_company`'s
    `company_id != current.company_id` guard), not something this schema
    can validate on its own without a DB read."""

    name: str = Field(..., min_length=1, max_length=255)
    due_date: date | None = None
    assignee_id: uuid.UUID | None = None
    phase_id: uuid.UUID


class TaskUpdateRequest(BaseModel):
    """Body for `PATCH /tasks/{id}` (Task 1.14). PATCH semantics: both
    fields optional, only-set fields are applied.

    `status` is validated here only for *value* legality (is this a known
    status at all) via `VALID_STATUSES`, not any transition legality —
    `tasks.status` (open/in_progress/done) has no documented state-machine
    transition table the way Lead/Project status does, so there's no
    separate transition-checking service for Task status the way there is
    for Lead (Task 1.5) and Project (Task 1.13).

    Role+ownership enforcement (`admin`/`project_manager` can update
    anything; `field_crew` can only update `status` on tasks assigned to
    them) is the router's job, not this schema's — same division of
    responsibility as every other RBAC check in this codebase.
    """

    status: str | None = None
    assignee_id: uuid.UUID | None = None

    @field_validator("status")
    @classmethod
    def status_must_be_a_known_value(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        return v


class TaskResponse(BaseModel):
    """Full model. `tasks` has `created_at` but no `updated_at`
    (docs/04-database-schema.md Section 4) — status/assignee changes are
    ordinary UPDATEs, the schema doc simply doesn't track a last-modified
    timestamp for this table."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    phase_id: uuid.UUID
    company_id: uuid.UUID
    name: str
    assignee_id: uuid.UUID | None
    due_date: date | None
    status: str
    created_at: datetime


class MyTaskResponse(TaskResponse):
    """`GET /tasks?assignee=me` item shape: TaskResponse enriched with
    project context (tasks reference only their phase directly, but the My
    Tasks view renders "task · project · due date" rows)."""

    project_id: uuid.UUID
    project_name: str


class MyTaskListResponse(BaseModel):
    """NOT cursor-paginated: one user's open assignment list is bounded
    small in practice; capped at 200 in the route."""

    items: list[MyTaskResponse]
