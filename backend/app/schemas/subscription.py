import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    tier: str
    status: str
    included_seats: int
    current_period_end: datetime | None


class PortalSessionResponse(BaseModel):
    url: str
