import uuid

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    company_name: str = Field(..., min_length=2, max_length=255)
    admin_full_name: str = Field(..., min_length=2, max_length=255)
    admin_email: EmailStr
    admin_password: str = Field(..., min_length=8)


class RegisterResponse(BaseModel):
    company_id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp_code: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class SwitchCompanyRequest(BaseModel):
    """Carries the refresh token, not just the target company: switching
    re-mints the whole session (see POST /auth/switch-company), so it
    rotates the refresh chain exactly as /auth/refresh does."""

    refresh_token: str
    company_id: uuid.UUID


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)
    totp_code: str | None = None


class PasswordResetRequest(BaseModel):
    """Asking for a reset link. Just the address — deliberately nothing
    else, so there is nothing here whose validation could differ between a
    registered address and an unregistered one."""

    email: EmailStr


class PasswordResetConfirmRequest(BaseModel):
    """Spending the link.

    `totp_code` is required when the account has MFA active, and checked in
    the route rather than here, matching `ChangePasswordRequest`. Without
    it, a reset by email would quietly downgrade a two-factor account to
    one factor — the inbox — which is precisely the thing the second factor
    exists to survive.
    """

    token: str
    new_password: str = Field(..., min_length=8)
    totp_code: str | None = None


class MfaEnrollResponse(BaseModel):
    secret: str
    otpauth_uri: str


class MfaActivateRequest(BaseModel):
    totp_code: str


class MfaDisableRequest(BaseModel):
    current_password: str
    totp_code: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    default_company_id: uuid.UUID
    # Defaulted so any other TokenResponse constructor site compiles before
    # being updated — but login and refresh MUST wire this explicitly per
    # spec Decision 3, never rely on the default.
    mfa_enrollment_required: bool = False
    # The user's role in their default company, from the same membership row
    # login/refresh already resolve via _default_membership (CRM+PM frontend
    # spec, Decision 1): the frontend needs it to choose which UI to render,
    # and the JWT deliberately carries no role claim. Display/routing signal
    # only — the backend's require_role checks remain the sole authorization
    # boundary. No default: both mint sites must wire it explicitly.
    role: str
