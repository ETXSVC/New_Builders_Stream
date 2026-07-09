import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class DailyLogCreateRequest(BaseModel):
    """Body for `POST /projects/{id}/daily-logs` (Task 1.16).

    Deliberately has NO photo field. US-3.3 mentions photos, but the
    `daily_logs` table (docs/04-database-schema.md Section 4) has no
    photo/document linkage column at all, and `documents` is the only
    file-storage mechanism Phase 1 builds. Per the plan's explicit
    recommendation: photo attachment is deferred rather than added as
    schema-doc scope creep for a P1-nice-to-have — Daily Logs ship
    text-only in Phase 1. `author_id` is likewise not a field here; the
    router sets it from `current.user.id`, never from client input.
    """

    log_date: date
    # VARCHAR(100) column.
    weather: str | None = Field(None, max_length=100)
    # TEXT column, no DB-level max length.
    notes: str | None = None


class DailyLogResponse(BaseModel):
    """Full model. Immutable once created — no update/delete route exists
    (Task 1.16), and Task 1.10's migration additionally `REVOKE`s
    UPDATE/DELETE on this table from `app_user` at the DB level (design
    decision #6), so there is no `DailyLogUpdateRequest`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    company_id: uuid.UUID
    author_id: uuid.UUID
    log_date: date
    weather: str | None
    notes: str | None
    created_at: datetime


class DailyLogListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /projects/{id}/daily-logs`
    (Task 1.16), following the exact pattern of `LeadListResponse`
    (app/schemas/lead.py). `next_cursor` is `None` once the caller has
    reached the last page."""

    items: list[DailyLogResponse]
    next_cursor: str | None = None
