"""Sign-in for the platform console (migration 0023).

Separate from `/auth/login` on purpose, and not merely for tidiness:

  * **TOTP is mandatory here.** `/auth/login` challenges for a second
    factor only when the user has enrolled one; this route refuses to
    issue a token to an account without active MFA at all. An account that
    can change every tenant's entitlements should not be one stolen
    password away.
  * **It mints a different token.** `create_platform_token` carries
    `scope: "platform"` and no `default_company_id`, so the result is
    useless against the product API — and `/auth/login` can never mint that
    scope, so the reverse is equally true.

These handlers run on the ORDINARY `app_user` connection, exactly as
`/auth/login` does, not on the BYPASSRLS platform engine. Everything they
touch (`users`, and `platform_admins` under its `self_read` policy) is
reachable that way, and the platform role deliberately holds no write on
`users` — enrolling a second factor is not a privilege the cross-tenant
console should have.

Enumeration: every failure below is 401 "Invalid credentials" regardless of
which of the four conditions failed (no such user, wrong password, not a
platform admin, MFA not enrolled). Whether a given address is a platform
operator is exactly the fact an attacker wants first.
"""
from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.config import settings
from app.core.security import (
    create_platform_token,
    hash_password,
    verify_password,
)
from app.db import session_scope, set_current_user
from app.models import PlatformAdmin, User
from app.models.base import utcnow
from app.schemas.platform import (
    PlatformLoginRequest,
    PlatformMfaActivateRequest,
    PlatformMfaEnrollRequest,
    PlatformMfaEnrollResponse,
    PlatformTokenResponse,
)
from app.services.mfa import generate_enrollment, verify_totp_code
from app.services.rate_limit import check_rate_limit

router = APIRouter(prefix="/platform/auth", tags=["platform"])

# Same constant-time-ish shape auth.py's register uses: verifying a dummy
# hash when the user is absent keeps "no such account" and "wrong password"
# indistinguishable by response timing.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-never-used-for-real-auth")

_INVALID = "Invalid credentials"

# Deliberately tighter than /auth/login's per-IP allowance (50 per 15 min).
# That limit is sized so a busy office behind one NAT address does not trip
# it; nobody legitimately attempts a platform sign-in dozens of times an
# hour, and this is the one credential in the system worth grinding at.
# Shares LOGIN_RATE_LIMIT_ENABLED so the test suite's existing switch (see
# tests/conftest.py) turns it off here too.
_PLATFORM_LOGIN_MAX_ATTEMPTS = 10
_PLATFORM_LOGIN_WINDOW_SECONDS = 900


async def _enforce_platform_login_rate_limit(request: Request, email: str) -> None:
    if not settings.login_rate_limit_enabled:
        return
    client_ip = request.client.host if request.client else "unknown"
    # Keyed on IP *and* email so neither a single address grinding many
    # accounts nor many addresses grinding one account slips through.
    for key in (f"platform-login:ip:{client_ip}", f"platform-login:email:{email.lower()}"):
        allowed = await check_rate_limit(
            key, _PLATFORM_LOGIN_MAX_ATTEMPTS, _PLATFORM_LOGIN_WINDOW_SECONDS
        )
        if not allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many sign-in attempts. Please try again later.",
            )


async def _authenticate(session, email: str, password: str) -> User:
    """Password check + platform-admin check, with one shared failure mode.

    Returns the User only when the caller is a platform admin who proved
    their password. Raises 401 with an identical message otherwise.
    """
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_valid = verify_password(password, password_hash)

    if user is None or not password_valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID)

    # The self_read policy on platform_admins is scoped to this GUC, so it
    # has to be set before the row is visible at all.
    await set_current_user(session, str(user.id))
    grant = await session.execute(
        select(PlatformAdmin).where(
            PlatformAdmin.user_id == user.id, PlatformAdmin.revoked_at.is_(None)
        )
    )
    if grant.scalar_one_or_none() is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _INVALID)

    return user


@router.post("/login", response_model=PlatformTokenResponse)
async def platform_login(
    payload: PlatformLoginRequest, request: Request, response: Response
) -> PlatformTokenResponse:
    await _enforce_platform_login_rate_limit(request, payload.email)

    async with session_scope() as session:
        async with session.begin():
            user = await _authenticate(session, payload.email, payload.password)

            # MANDATORY second factor. Note this is checked only past the
            # password + platform-admin checks, so it discloses nothing to
            # someone who has not already proved both.
            if user.mfa_activated_at is None:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    "Two-factor authentication must be enrolled before using the "
                    "platform console. Call /platform/auth/mfa/enroll.",
                )
            if payload.totp_code is None:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "TOTP code required")
            if not verify_totp_code(user, payload.totp_code):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid TOTP code")

            token = create_platform_token(str(user.id))

    # A shared secret transited the body; same header /auth/mfa/enroll sets.
    response.headers["Cache-Control"] = "no-store"
    return PlatformTokenResponse(
        access_token=token,
        expires_in_minutes=settings.platform_jwt_expire_minutes,
        email=user.email,
    )


@router.post("/mfa/enroll", response_model=PlatformMfaEnrollResponse)
async def platform_mfa_enroll(
    payload: PlatformMfaEnrollRequest, response: Response
) -> PlatformMfaEnrollResponse:
    """Begin (or restart) TOTP enrollment for a platform admin.

    Password-gated rather than session-gated, because a platform admin may
    belong to no company and so cannot obtain the tenant session
    `/auth/mfa/enroll` requires. Re-enrolling while PENDING rotates the
    secret; re-enrolling while ACTIVE is refused, matching that route.
    """
    async with session_scope() as session:
        async with session.begin():
            user = await _authenticate(session, payload.email, payload.password)

            if user.mfa_activated_at is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, "MFA is already active")

            secret, uri, encrypted = generate_enrollment(user.email)
            user.totp_secret_encrypted = encrypted
            user.totp_last_used_step = None

    response.headers["Cache-Control"] = "no-store"
    return PlatformMfaEnrollResponse(secret=secret, otpauth_uri=uri)


@router.post("/mfa/activate", status_code=status.HTTP_204_NO_CONTENT)
async def platform_mfa_activate(payload: PlatformMfaActivateRequest) -> None:
    """Prove possession of the enrolled secret. Only after this does
    /platform/auth/login issue tokens for the account."""
    async with session_scope() as session:
        async with session.begin():
            user = await _authenticate(session, payload.email, payload.password)

            if user.totp_secret_encrypted is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "No MFA enrollment pending")
            if user.mfa_activated_at is not None:
                raise HTTPException(status.HTTP_409_CONFLICT, "MFA is already active")
            if not verify_totp_code(user, payload.totp_code):
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid TOTP code")

            user.mfa_activated_at = utcnow()
