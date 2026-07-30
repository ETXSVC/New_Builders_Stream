import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import settings

_hasher = PasswordHasher()


class InvalidTokenError(Exception):
    pass


def hash_password(plain_password: str) -> str:
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    # VerificationError (parent of VerifyMismatchError) covers a genuine wrong
    # password. InvalidHashError covers a malformed/corrupted password_hash
    # value — it is NOT a VerificationError subclass (its hierarchy is
    # InvalidHashError -> ValueError, a completely separate branch from
    # VerificationError -> Argon2Error; confirmed by inspecting argon2-cffi's
    # actual exception classes, not assumed), so it must be caught explicitly
    # or a corrupted row surfaces as an unhandled 500 from the login endpoint
    # instead of a controlled auth failure. Not reachable via any normal write
    # path today (this schema only ever writes Argon2 hashes), but auth code
    # should fail closed on malformed input as a matter of course, not just
    # for inputs the current code happens to produce.
    try:
        return _hasher.verify(password_hash, plain_password)
    except (VerificationError, InvalidHashError):
        return False


# Token audiences. A token is usable on exactly one of the two surfaces,
# and the claim is what decides which — never the route's own guesswork.
#
# `TENANT_SCOPE` is the default for any token issued before this claim
# existed, which is why `get_current_user` reads it with that fallback: an
# access token minted by the previous deploy stays valid for its remaining
# lifetime instead of logging everyone out mid-session.
TENANT_SCOPE = "tenant"
PLATFORM_SCOPE = "platform"


def create_access_token(user_id: str, default_company_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "default_company_id": default_company_id,
        "scope": TENANT_SCOPE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def create_platform_token(user_id: str) -> str:
    """A session for the platform console, and for nothing else.

    Deliberately carries NO `default_company_id`. That claim is what
    `get_current_user` falls back to when no `X-Tenant-ID` header is
    present, so omitting it means this token cannot name a tenant even if
    the scope check below were somehow bypassed — two independent reasons
    it fails on the ordinary API rather than one.

    Its own lifetime is separate from `jwt_expire_minutes` because the two
    are different risk profiles: an ordinary access token grants one
    tenant's data to someone who already belongs there, while this one
    reaches every tenant's subscription state.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "scope": PLATFORM_SCOPE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.platform_jwt_expire_minutes),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
