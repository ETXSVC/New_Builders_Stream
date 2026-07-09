import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.communication_log import VALID_CHANNELS


class CommunicationLogCreateRequest(BaseModel):
    channel: str
    body: str = Field(..., min_length=1)

    @field_validator("channel")
    @classmethod
    def channel_must_be_valid(cls, v: str) -> str:
        if v not in VALID_CHANNELS:
            raise ValueError(f"channel must be one of {VALID_CHANNELS}")
        return v


class CommunicationLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    lead_id: uuid.UUID
    company_id: uuid.UUID
    author_id: uuid.UUID
    channel: str
    body: str
    created_at: datetime


class CommunicationLogListResponse(BaseModel):
    """Cursor-paginated list envelope for `GET /leads/{lead_id}/communications`
    (app/core/pagination.py), following the exact pattern of
    `LeadListResponse` (app/schemas/lead.py). `next_cursor` is `None` once
    the caller has reached the last page."""

    items: list[CommunicationLogResponse]
    next_cursor: str | None = None
