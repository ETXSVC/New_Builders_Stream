import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest

from app.config import settings
from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    decode_access_token,
    InvalidTokenError,
)


def test_password_hash_roundtrip():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed) is True


def test_password_hash_rejects_wrong_password():
    hashed = hash_password("correct horse battery staple")
    assert verify_password("wrong password", hashed) is False


def test_password_hash_rejects_malformed_hash():
    """Regression test for a real gap found in Task 7's code-quality review:
    verify_password originally only caught VerifyMismatchError, so a
    corrupted/malformed password_hash value (e.g. from database corruption or
    a manual edit) raised an unhandled InvalidHashError instead of failing
    closed. InvalidHashError is not a VerificationError subclass — it's a
    separate ValueError branch — so it has to be caught explicitly."""
    assert verify_password("anything", "not-a-valid-argon2-hash") is False


def test_token_roundtrip():
    user_id = str(uuid.uuid4())
    company_id = str(uuid.uuid4())
    token = create_access_token(user_id=user_id, default_company_id=company_id)
    payload = decode_access_token(token)
    assert payload["sub"] == user_id
    assert payload["default_company_id"] == company_id


def test_token_rejects_tampering():
    token = create_access_token(user_id=str(uuid.uuid4()), default_company_id=str(uuid.uuid4()))
    # Tamper the SECOND-to-last character, not the last one. HMAC-SHA256
    # produces a 32-byte signature; base64url-encoding 32 bytes (not a
    # multiple of 3) leaves the final character with only 4 meaningful bits
    # (2 are fixed padding), so a substitution targeting position -1
    # specifically has a real chance of decoding to the exact same signature
    # byte, making the test nondeterministically flaky — empirically measured
    # at roughly an 8% failure-to-raise rate across repeated trials. Position
    # -2 is a fully meaningful base64 character (all 6 bits are real signature
    # data), so any substitution there deterministically changes the decoded
    # signature and reliably triggers InvalidSignatureError. Verified 0
    # failures across 500 trials after this fix, versus ~8% before it.
    tampered = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(InvalidTokenError):
        decode_access_token(tampered)


def test_token_rejects_expired_token():
    """Expiry is the core security property of a 60-minute access token —
    deserves its own explicit test, not just reliance on tampering coverage."""
    now = datetime.now(timezone.utc)
    expired_payload = {
        "sub": str(uuid.uuid4()),
        "default_company_id": str(uuid.uuid4()),
        "iat": now - timedelta(minutes=120),
        "exp": now - timedelta(minutes=60),
        "jti": str(uuid.uuid4()),
    }
    expired_token = pyjwt.encode(expired_payload, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        decode_access_token(expired_token)


def test_token_rejects_wrong_secret():
    """A token signed with a different secret must be rejected — this is the
    actual property that makes the JWT signature meaningful at all."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid.uuid4()),
        "default_company_id": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=60),
        "jti": str(uuid.uuid4()),
    }
    wrong_secret_token = pyjwt.encode(payload, "a-completely-different-secret", algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        decode_access_token(wrong_secret_token)
