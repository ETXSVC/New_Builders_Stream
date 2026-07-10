import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class MarkupProfileCreateRequest(BaseModel):
    """Plain company-scoped create (Task 2.5's `POST /markup-profiles`) —
    no inheritance concept here, matching `MarkupProfile`'s own docstring
    (`app/models/markup_profile.py`)."""

    name: str = Field(..., min_length=1, max_length=255)
    overhead_pct: Decimal = Decimal("0")
    profit_pct: Decimal = Decimal("0")


class MarkupProfileResponse(BaseModel):
    """Full model. No `created_at`/`updated_at` fields — `markup_profiles`
    has neither column (docs/04-database-schema.md Section 5), matching
    `MarkupProfile`'s deliberate omission of both `TimestampMixin` and
    `UpdatedAtMixin`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    overhead_pct: Decimal
    profit_pct: Decimal


class MarkupProfileListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /markup-profiles`. See
    `app/routers/catalogs.py`'s module docstring for why this route's
    pagination cursor is a bare `id` (not a `(created_at, id)` composite the
    way `app/core/pagination.py`'s `paginate()` produces for every other list
    route) — `markup_profiles` has neither a `created_at` nor an `updated_at`
    column at all (docs/04-database-schema.md Section 5 / the `MarkupProfile`
    model's own docstring, Task 2.1's deliberate choice), so there is no
    timestamp column for `paginate()` to order on in the first place."""

    items: list[MarkupProfileResponse]
    next_cursor: str | None = None
