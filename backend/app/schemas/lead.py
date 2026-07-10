import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.lead import VALID_STATUSES


class LeadCreateRequest(BaseModel):
    contact_name: str = Field(..., min_length=2, max_length=255)
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
    assignment," Task 1.5) is enforced by the ROUTER validating `status`
    against the legal-TRANSITION table before persisting it, not by the
    schema. This schema only validates that the value is one of the known
    statuses at all (same VALID_STATUSES tuple channel/role validation
    already references elsewhere in this codebase) — that's a value-set
    check, not a transition check, so it belongs here for the same reason
    channel/role validation does: a clean 422 instead of an unhandled DB
    CheckConstraint IntegrityError for a client typo like status="banana".
    Whether e.g. "won" is a LEGAL transition from the lead's current state
    still requires a DB read and stays exclusively the router's job.
    """

    contact_name: str | None = Field(None, min_length=2, max_length=255)
    project_name: str | None = Field(None, min_length=1, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=20)
    project_type: str | None = Field(None, min_length=1, max_length=100)
    estimated_value: Decimal | None = None
    notes: str | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def status_must_be_a_known_value(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_STATUSES:
            raise ValueError(f"status must be one of {VALID_STATUSES}")
        return v


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


class LeadListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /leads` (app/core/pagination.py).
    `next_cursor` is `None` once the caller has reached the last page."""

    items: list[LeadResponse]
    next_cursor: str | None = None
