import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.project import VALID_STATUSES


class ProjectCreateRequest(BaseModel):
    """Manual `POST /projects` initialization path (independent of the
    `LEAD_WON` auto-draft path — Task 1.18 wires that one separately)."""

    name: str = Field(..., min_length=1, max_length=255)
    # TEXT column (docs/04-database-schema.md Section 4), no DB-level max
    # length — only a non-empty-string floor, matching the column's own
    # NOT NULL constraint.
    site_address: str = Field(..., min_length=1)
    lead_id: uuid.UUID | None = None
    projected_start_date: date | None = None


class ProjectResponse(BaseModel):
    """Full model, returned to every role except `client`
    (design decision #8) — see `ProjectClientDashboardResponse` below."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    lead_id: uuid.UUID | None
    name: str
    site_address: str
    status: str
    projected_start_date: date | None
    created_at: datetime
    updated_at: datetime


class ProjectClientDashboardResponse(BaseModel):
    """Sanitized shape returned to the `client` role from the *same*
    `GET /projects/{id}` route (design decision #8) — the response schema
    is selected by the caller's role inside the handler, this is not a
    separate endpoint.

    Deliberately excludes `lead_id` (internal CRM linkage — which lead this
    project was drafted from is not the client's business) and
    `company_id` (internal tenant identifier). Phase 1 has no financial
    fields on `projects` yet (those arrive with Estimation in Phase 2), so
    "sanitized" here means "no internal operational detail," not "no
    budget data" — see design decision #8's own framing.

    `phase_count`/`task_count`/`completed_task_count` are NOT model fields
    (no such columns exist on `projects`) — a Task 1.11 spec-review found
    the original design decision #8 promised "phase/task progress" with no
    concrete fields and no route anywhere in the plan that ever supplied
    it, since clients have no other route to query phases/tasks
    independently. The router (Task 1.12) is expected to compute these via
    `COUNT` queries against `phases`/`tasks` scoped to this project and
    construct this response explicitly — NOT via
    `ProjectClientDashboardResponse.model_validate(project)` on the bare
    ORM row, which would raise a validation error citing these 3 missing
    fields. No `from_attributes` config here (deliberately, unlike every
    other Response class in this codebase) as a further signal that this
    one isn't meant to be built directly from an ORM instance. Construct
    it explicitly instead, e.g.:

        ProjectClientDashboardResponse(
            id=project.id, name=project.name, status=project.status,
            site_address=project.site_address,
            projected_start_date=project.projected_start_date,
            phase_count=phase_count, task_count=task_count,
            completed_task_count=completed_task_count,
        )
    """

    id: uuid.UUID
    name: str
    status: str
    site_address: str
    projected_start_date: date | None
    phase_count: int
    task_count: int
    completed_task_count: int


class ProjectStatusUpdateRequest(BaseModel):
    """Body for `PATCH /projects/{id}/status` (Task 1.13) — a route
    entirely separate from `PATCH /projects/{id}` (Task 1.12,
    `ProjectPatchRequest` below). Unlike Lead, which combines field edits
    and status transitions into a single `PATCH /leads/{id}` route/schema,
    Project splits them into two routes/schemas (design decision #3), so
    there's no `LeadUpdateRequest`-style "must keep `status` on the general
    PATCH schema to avoid Pydantic's extra=ignore footgun" concern here —
    `status` only ever arrives via this schema.

    `status` is validated here only for *value* legality (is this a known
    status at all) via `VALID_STATUSES`, not *transition* legality (is this
    a legal move from the project's current state) — that check requires a
    DB read of the current row and is exclusively the state-machine
    service's job (Task 1.13's `app/services/project_transitions.py`), same
    division of responsibility as Lead's state machine (Task 1.5).
    """

    status: str
    # Not a DB column — passed through to the `project.status_changed`
    # audit log entry's metadata (`{from, to, reason}`, Task 1.13). No
    # length cap for the same reason `Lead.notes` has none: free-text
    # audit context, not a bounded VARCHAR column.
    reason: str | None = None

    @field_validator("status")
    @classmethod
    def status_must_be_a_known_value(cls, v: str) -> str:
        if v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        return v


class ProjectPatchRequest(BaseModel):
    """Body for `PATCH /projects/{id}` (Task 1.12), added beyond the
    literal API spec route table per design decision #3 — a
    `LEAD_WON`-drafted Project has `site_address=''` and this is the only
    way to ever fill it in. PATCH semantics: every field optional,
    only-set fields are applied.

    Deliberately has NO `status` field: Project's status changes go
    through the entirely separate `ProjectStatusUpdateRequest` /
    `PATCH /projects/{id}/status` route (Task 1.13), unlike Lead's single
    combined PATCH route. There is no extra=ignore footgun to guard
    against here the way `LeadUpdateRequest` needed, because a caller
    sending `status` on this route simply has no schema field to land in —
    it's silently ignored, but ignoring it is fully correct, not a risk,
    since this route was never the sanctioned way to change status.

    Also has no `lead_id` field: which Lead a Project was drafted from is
    fixed at creation (manual `POST /projects` or the `LEAD_WON` auto-draft,
    Task 1.18) and isn't meant to be editable after the fact.
    """

    name: str | None = Field(None, min_length=1, max_length=255)
    site_address: str | None = Field(None, min_length=1)
    projected_start_date: date | None = None


class ProjectListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /projects`
    (app/core/pagination.py), following the exact pattern of
    `LeadListResponse` (app/schemas/lead.py). `next_cursor` is `None` once
    the caller has reached the last page."""

    items: list[ProjectResponse]
    next_cursor: str | None = None
