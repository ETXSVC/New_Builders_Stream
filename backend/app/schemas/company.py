import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.user import VALID_ROLES


class CompanyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    is_active: bool
    created_at: datetime


class CreateChildCompanyRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)


class CompanyMemberResponse(BaseModel):
    """One row of GET /companies/members — the task assignee picker's data
    source. user_id (not `id`) deliberately: this is a membership view, and
    the value callers need is exactly what tasks.assignee_id stores."""

    user_id: uuid.UUID
    full_name: str
    email: str
    role: str


class CompanyMemberListResponse(BaseModel):
    """Not paginated: a company's member count is seat-bounded (billing's
    included_seats model), far below any size needing cursors."""

    items: list[CompanyMemberResponse]


class CompanyRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class MemberRoleUpdateRequest(BaseModel):
    """Change an existing member's role within the caller's active tenant."""

    role: str

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        return v
