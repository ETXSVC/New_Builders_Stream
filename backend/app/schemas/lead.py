import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LeadCreateRequest(BaseModel):
    contact_name: str = Field(..., min_length=1, max_length=255)
    project_name: str = Field(..., min_length=1, max_length=255)
    email: EmailStr
    phone: str | None = Field(None, max_length=20)
    project_type: str = Field(..., min_length=1, max_length=100)
    estimated_value: Decimal | None = None
    notes: str | None = None


class LeadUpdateRequest(BaseModel):
    """PATCH semantics: every field optional, only-set fields are applied.

    `status` IS present on this schema, matching the API spec's single
    combined `PATCH /leads/{id}` route ("Update Lead fields / status," one
    body, not two) — deliberately not excluded. Pydantic v2's default
    extra="ignore" means a field absent from a request schema doesn't
    error, it silently vanishes; excluding `status` here would make a
    router that forgets to special-case it silently drop the caller's
    status transition with zero error, which is worse than what this
    field's presence risks. The safety property ("not blind field
    assignment," Task 1.5) is enforced by the ROUTER validating
    `status` against the legal-transition table before persisting it,
    not by the schema omitting the field. `status` is intentionally left
    unconstrained here (not validated against VALID_STATUSES) — the
    state-machine validator in app/services/lead_transitions.py (Task 1.5)
    is the single source of truth for which values/transitions are legal,
    including for a fresh value with no valid prior state; duplicating
    that check at the schema layer would just be a second place to keep
    in sync with the transition table.
    """

    contact_name: str | None = Field(None, min_length=1, max_length=255)
    project_name: str | None = Field(None, min_length=1, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=20)
    project_type: str | None = Field(None, min_length=1, max_length=100)
    estimated_value: Decimal | None = None
    notes: str | None = None
    status: str | None = None


class LeadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    contact_name: str
    project_name: str
    email: EmailStr
    phone: str | None
    status: str
    estimated_value: Decimal | None
    project_type: str
    notes: str | None
    created_at: datetime
    updated_at: datetime
