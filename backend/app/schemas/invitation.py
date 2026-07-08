import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, field_validator

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
    id: uuid.UUID
    company_id: uuid.UUID
    email: EmailStr
    role: str
    expires_at: datetime
    accepted_at: datetime | None

    class Config:
        from_attributes = True


class InvitationAcceptRequest(BaseModel):
    full_name: str
    password: str
