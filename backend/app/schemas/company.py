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
    # From the team directory (migration 0026), through
    # app/services/team_directory.py. Both are null for somebody with no
    # profile row yet, which is every member until an admin or they
    # themselves first fill one in — callers fall back to `full_name`.
    #
    # `filed_name` is what THIS COMPANY calls them, which is not necessarily
    # the name on their account: a picker offering "R. Okafor (Electrician)"
    # answers "who should do this?" in a way a login never did. Deliberately
    # only these two fields — an address in a dropdown payload is
    # over-fetching, and `notes` is withheld even from its own subject.
    filed_name: str | None = None
    profession: str | None = None


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
