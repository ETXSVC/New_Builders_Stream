"""Request/response shapes for the team directory (migration 0026)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class PhoneEntry(BaseModel):
    """One phone number. No `id`: phones are addressed as a set, not
    individually — see `MemberProfileUpdateRequest.phones`."""

    label: str | None = Field(default=None, max_length=50)
    number: str = Field(min_length=1, max_length=40)


class ProfessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str


class ProfessionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TeamMemberResponse(BaseModel):
    """A person in this company: their membership, their account, and this
    company's own record of them.

    `email`, `full_name` and `role` are not on `member_profiles` — the first
    two belong to the global `users` row (the login) and the third to
    `company_users`. They are joined in here because a directory that made
    you fetch three things to show one person would be a poor API.
    """

    user_id: uuid.UUID
    email: EmailStr
    # The name the account holder set for themselves, which is not
    # necessarily what their employer files them under.
    account_name: str | None
    role: str
    joined_at: datetime

    first_name: str | None = None
    last_name: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    notes: str | None = None
    profession: ProfessionResponse | None = None
    phones: list[PhoneEntry] = []
    # True when a photo has been uploaded; the bytes come from
    # GET /team/{user_id}/photo rather than being inlined here.
    has_photo: bool = False
    # Send this back in a PATCH to get optimistic concurrency. None for a
    # member who has no profile row yet — there is nothing to conflict with.
    updated_at: datetime | None = None


class TeamPage(BaseModel):
    items: list[TeamMemberResponse]
    next_cursor: str | None


class MemberProfileUpdateRequest(BaseModel):
    """Every field optional — a PATCH setting only `city` must not blank the
    rest. An OMITTED key means "leave alone"; an explicit `null` CLEARS the
    field. The router tells the two apart through `model_fields_set`, because
    Pydantic gives both the same value and collapsing them would leave no way
    to erase an address somebody entered by mistake.

    EXCEPT `phones`, which is a REPLACED SET when present. Sending
    `[{"label": "mobile", "number": "..."}]` makes that the complete list;
    sending `[]` removes every number; omitting the key changes nothing.
    Per-phone add/remove endpoints would be three more routes and a
    client-side id to track, for an edit that is always "here is the list
    now" in practice.
    """

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=20)
    notes: str | None = None
    # Explicitly nullable-with-meaning: see the router, which distinguishes
    # "absent" from "null" via model_fields_set so a profession can actually
    # be cleared.
    profession_id: uuid.UUID | None = None
    phones: list[PhoneEntry] | None = None
    # Optimistic concurrency, opt-in, in the BODY rather than an If-Match
    # header — the Next BFF forwards a fixed header allowlist, so a custom
    # header would be silently dropped on the hop and every stale write
    # would sail through. Omit it and you get last-write-wins, deliberately,
    # so existing callers keep working.
    #
    # Two admins editing one person's details at once is the exact race this
    # exists for: without it the second save silently discards the first's
    # phone numbers with no 409 and no trace.
    expected_updated_at: datetime | None = None


class MemberSelfUpdateRequest(BaseModel):
    """What a person may change about themselves: how to reach them.

    A separate model rather than the one above with a role check around it,
    and the difference is the safety. `notes` is the company's private record
    ABOUT somebody and `profession_id` is how the company classifies them for
    assignment — neither is theirs to state. Here they are not optional-and-
    ignored, they are absent, so `PATCH /team/me` cannot write them however
    it is called.

    `extra="forbid"` closes the other half: without it, Pydantic drops an
    unknown `notes` key silently and the caller is told their edit succeeded.
    A 422 that names the field is the honest answer, and it is also what
    stops a future field added to `MemberProfileUpdateRequest` from quietly
    becoming self-writable if somebody ever merges the two.
    """

    model_config = ConfigDict(extra="forbid")

    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    address_line1: str | None = Field(default=None, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=20)
    phones: list[PhoneEntry] | None = None
    expected_updated_at: datetime | None = None
