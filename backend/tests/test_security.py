import time
import uuid

import pytest

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


def test_token_roundtrip():
    user_id = str(uuid.uuid4())
    company_id = str(uuid.uuid4())
    token = create_access_token(user_id=user_id, default_company_id=company_id)
    payload = decode_access_token(token)
    assert payload["sub"] == user_id
    assert payload["default_company_id"] == company_id


def test_token_rejects_tampering():
    token = create_access_token(user_id=str(uuid.uuid4()), default_company_id=str(uuid.uuid4()))
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(InvalidTokenError):
        decode_access_token(tampered)
