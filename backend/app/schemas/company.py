import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class CompanyResponse(BaseModel):
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class CreateChildCompanyRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
