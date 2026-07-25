"""Request/response shapes for granting and revoking a client's access to a
Project or Lead (migration 0019, `app/services/client_scope.py`)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ClientAccessGrantRequest(BaseModel):
    """`user_id`, not an email: the grant targets an existing account in
    this company, and resolving by email here would let a caller probe
    which addresses have accounts. Admins get the ids from
    `GET /companies/members`."""

    user_id: uuid.UUID


class ClientAccessResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    # The member's own details, so a UI listing a project's clients doesn't
    # have to cross-reference every id against /companies/members.
    email: str
    # Nullable to match `users.full_name` — an invited account that hasn't
    # completed acceptance yet has none.
    full_name: str | None
    created_at: datetime


class ClientAccessListResponse(BaseModel):
    """No cursor: the number of clients on one project or lead is bounded by
    how many people signed the contract — a handful, not a growing list."""

    items: list[ClientAccessResponse]
