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

    Deliberately excludes `status`. Per Task 1.5, status changes go through
    a state-machine validator (app/services/lead_transitions.py) in the
    router, not blind attribute assignment from a request body — a status
    value accepted here could bypass the legal-transition table entirely.
    The router combines this schema with a separate status-transition input
    so a single PATCH request can still update fields and status together,
    validated as one transaction (see Task 1.5's plan notes).
    """

    contact_name: str | None = Field(None, min_length=1, max_length=255)
    project_name: str | None = Field(None, min_length=1, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=20)
    project_type: str | None = Field(None, min_length=1, max_length=100)
    estimated_value: Decimal | None = None
    notes: str | None = None


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
