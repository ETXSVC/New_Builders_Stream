"""Task 4.6 (design spec Sections 3, 6): request/response schemas for the
Integrations routes."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuthorizationUrlResponse(BaseModel):
    authorization_url: str


class IntegrationConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    connected_at: datetime


class SyncRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    status: str
    attempt_count: int
    last_error: str | None
    last_attempted_at: datetime | None


class SyncStatusResponse(BaseModel):
    provider: str
    connected_at: datetime
    records: list[SyncRecordResponse]
    next_cursor: str | None = None
