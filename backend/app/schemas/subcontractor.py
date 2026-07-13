import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SubcontractorCreateRequest(BaseModel):
    """Body for `POST /subcontractors` (future task). Per
    docs/05-api-specification.md Section 8 and `Subcontractor`'s own
    docstring (Task 3.1, `app/models/subcontractor.py`).

    `company_id` is deliberately not a field here: the router derives it
    from `current.company_id`, never from client input — same
    "server owns the tenant scoping column" pattern every other
    `*CreateRequest` in this codebase follows (e.g. `DailyLogCreateRequest`,
    `ChangeOrderCreateRequest`).

    `name`/`trade` carry `max_length` matching `Subcontractor`'s own
    `String(255)`/`String(100)` columns (`app/models/subcontractor.py`),
    same "schema length bound mirrors the DB column" convention every other
    bounded-string `*CreateRequest` in this codebase already follows (e.g.
    `MarkupProfileCreateRequest.name`, `ProjectCreateRequest.name`) — without
    it, an oversized value passes validation and only fails at the DB layer
    as an unhandled `DataError` (500), not a clean 422.
    """

    name: str = Field(..., min_length=1, max_length=255)
    trade: str | None = Field(None, max_length=100)
    contact_email: EmailStr | None = None


class SubcontractorResponse(BaseModel):
    """Full model. No `updated_at` field — `Subcontractor` (Task 3.1) has no
    such column at all (TimestampMixin only, no UpdatedAtMixin), matching
    Phase's own precedent."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    trade: str | None
    contact_email: EmailStr | None
    created_at: datetime


class SubcontractorListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /subcontractors` (future
    task), following the exact pattern of `DailyLogListResponse`
    (app/schemas/daily_log.py). `next_cursor` is `None` once the caller has
    reached the last page."""

    items: list[SubcontractorResponse]
    next_cursor: str | None = None
