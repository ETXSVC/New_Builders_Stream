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
