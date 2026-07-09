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
    full_name: str = Field(..., min_length=2, max_length=255)
    password: str = Field(..., min_length=8)
