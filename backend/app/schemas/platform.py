"""Request/response shapes for the platform console (migration 0023)."""
import uuid
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, EmailStr, Field

from app.models.company_module_override import VALID_MODULES
from app.models.subscription import VALID_TIERS

ModuleName = Literal["estimation", "compliance", "accounting", "integrations", "child_branches"]
TierName = Literal["starter", "pro", "enterprise"]

# The Literals above are what FastAPI validates and publishes into the
# OpenAPI schema; these asserts are what stop them drifting from the model
# constants and the CHECK constraints built from them. A mismatch would
# otherwise surface as a 500 from Postgres on a value the API accepted.
#
# `get_args`, not `.__args__`: the attribute exists at runtime but is not
# part of the static type of a typing special form, so mypy rejects it.
assert set(VALID_MODULES) == set(get_args(ModuleName)), "ModuleName drifted from VALID_MODULES"
assert set(VALID_TIERS) == set(get_args(TierName)), "TierName drifted from VALID_TIERS"


class PlatformLoginRequest(BaseModel):
    email: EmailStr
    password: str
    # Optional in the SHAPE only. Platform login refuses to complete without
    # it — see the router; the field is nullable so the first request can
    # legitimately omit it and be told to prompt for one, exactly as
    # /auth/login does.
    totp_code: str | None = None


class PlatformTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    email: EmailStr


class PlatformMfaEnrollRequest(BaseModel):
    """Enrollment is password-gated rather than session-gated: a platform
    admin may hold no company membership at all, so the ordinary
    /auth/mfa/enroll route (which needs a tenant session) is unreachable for
    them."""

    email: EmailStr
    password: str


class PlatformMfaEnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str


class PlatformMfaActivateRequest(BaseModel):
    email: EmailStr
    password: str
    totp_code: str


class ModuleEntitlement(BaseModel):
    module: ModuleName
    # What the subscription tier alone would allow.
    allowed_by_tier: bool
    # None when no override row exists (defer to the tier).
    override: bool | None
    # What the gate will actually decide. The console shows this rather than
    # making the reader compute it from the two fields above.
    effective: bool
    note: str | None = None


class TenantSummary(BaseModel):
    company_id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None
    is_root: bool
    tier: TierName | None
    status: str | None
    included_seats: int | None
    manual_status_override: bool
    # What `block_if_read_only` will decide for this tenant right now,
    # including its fail-open on a missing subscription row — so the console
    # shows the effect rather than making the reader derive it from `status`.
    writes_enabled: bool
    user_count: int
    created_at: datetime
    # Set when the console has taken this tenant out of service (migration
    # 0024). NULL means live. Surfaced rather than filtered away so the
    # console can show and reverse it — a soft delete nobody can see is
    # indistinguishable from a hard one.
    deleted_at: datetime | None = None


class TenantDetail(TenantSummary):
    modules: list[ModuleEntitlement]
    child_company_ids: list[uuid.UUID]


class TenantPage(BaseModel):
    items: list[TenantSummary]
    next_cursor: str | None


class SubscriptionUpdateRequest(BaseModel):
    """Every field optional — a PATCH that sets only `tier` must not blank
    the rest. `None` means "leave alone"."""

    tier: TierName | None = None
    status: str | None = Field(default=None, max_length=20)
    included_seats: int | None = Field(default=None, ge=0)
    # Explicitly hands status control back to Stripe. Only meaningful
    # alongside (or after) a manual status change; see the router.
    clear_manual_status_override: bool = False


class ModuleOverrideRequest(BaseModel):
    enabled: bool
    note: str | None = Field(default=None, max_length=500)


class TenantCreateRequest(BaseModel):
    """Everything needed to bring a customer into existence.

    Mirrors `RegisterRequest`'s fields minus the password, which the
    operator does not choose — see `TenantCreateResponse`. `parent_id` is
    absent on purpose: this always creates a ROOT company. Branches are
    created by the tenant's own admins, and `companies.parent_id` is
    immutable (migration 0021), so an operator picking the wrong parent here
    would be a migration to undo rather than an edit.
    """

    company_name: str = Field(min_length=1, max_length=255)
    owner_email: EmailStr
    owner_full_name: str = Field(min_length=1, max_length=255)
    # The tier the trial starts on. Registration hardcodes "pro"; an
    # operator creating a tenant by hand usually knows what was sold.
    tier: TierName = "pro"


class TenantCreateResponse(BaseModel):
    """The new tenant, plus a credential shown exactly once.

    The operator does not choose the owner's password and the API does not
    store a recoverable copy — `temporary_password` is generated here,
    hashed like any other, and returned in this response and nowhere else.
    It is not in the audit log, not in the tenant detail, and not
    retrievable afterwards: losing it means the owner uses the ordinary
    password-reset path, which is the correct outcome.

    Chosen over letting the operator type one because an operator inventing
    passwords for customers converges on one weak password, and over the
    invitation flow only because that leaves no owner user (and so a tenant
    with `user_count: 0`) until the customer acts. Swapping to invitations
    later is a contained change — see `app/routers/invitations.py`.
    """

    tenant: TenantDetail
    owner_user_id: uuid.UUID
    owner_email: EmailStr
    temporary_password: str


class TenantUpdateRequest(BaseModel):
    """Rename only.

    Not `parent_id` (immutable, migration 0021), not `deleted_at` (its own
    routes, so that taking a customer out of service can never be something
    that happens as a side effect of fixing a typo), and not the
    subscription (its own route already).
    """

    name: str = Field(min_length=1, max_length=255)
