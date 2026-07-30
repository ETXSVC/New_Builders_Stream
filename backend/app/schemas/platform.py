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
