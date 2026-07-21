import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VendorCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    contact_email: str | None = Field(None, max_length=255)
    contact_phone: str | None = Field(None, max_length=50)
    notes: str | None = Field(None, max_length=2000)


class VendorPatchRequest(BaseModel):
    """All fields optional; `None` means "leave unchanged," same PATCH
    convention as CostCatalogItemPatchRequest/MarkupProfilePatchRequest."""

    name: str | None = Field(None, min_length=1, max_length=255)
    contact_email: str | None = Field(None, max_length=255)
    contact_phone: str | None = Field(None, max_length=50)
    notes: str | None = Field(None, max_length=2000)


class VendorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    contact_email: str | None
    contact_phone: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class VendorListResponse(BaseModel):
    items: list[VendorResponse]
    next_cursor: str | None = None
