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


def create_access_token(user_id: str, default_company_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "default_company_id": default_company_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
