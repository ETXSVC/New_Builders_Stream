import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.user import VALID_ROLES


class InvitationCreateRequest(BaseModel):
    email: EmailStr
    role: str

    @field_validator("role")
    @classmethod
    def role_must_be_valid(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        return v


class InvitationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    email: EmailStr
    role: str
    expires_at: datetime
    accepted_at: datetime | None


class InvitationAcceptRequest(BaseModel):
    """Both fields are optional because the route has two callers.

    A brand-new user must supply them — the route enforces that, and still
    applies the same length rules to what they send. An EXISTING account
    joining a second company (migration 0031) has a name and a password
    already, and the route ignores both fields for that path on purpose:
    honouring a password there would be an account-takeover primitive.

    Making them required in the schema would mean that caller had to invent
    a throwaway ≥8-character password to have it discarded — validation
    demanding something the handler refuses to use.
    """

    full_name: str | None = Field(None, min_length=2, max_length=255)
    password: str | None = Field(None, min_length=8)


class InvitationListResponse(BaseModel):
    """Not paginated: outstanding invitations are bounded by how many people
    a company is onboarding at once, and they expire in 7 days."""

    items: list[InvitationResponse]
