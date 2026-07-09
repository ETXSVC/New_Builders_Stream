import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CompanyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    is_active: bool
    created_at: datetime


class CreateChildCompanyRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
