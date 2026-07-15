"""Task 4.5 (design spec Section 3): signed, short-lived OAuth `state`
tokens. Reuses settings.jwt_secret under a distinct signing context (an
`aud` claim this codebase's real access tokens — app/core/security.py's
create_access_token — never set), not a second secret or a
database-backed nonce table."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.config import settings
from app.services.integration_oauth_state import (
    InvalidOAuthStateError,
    sign_oauth_state,
    verify_oauth_state,
)


def test_sign_then_verify_round_trips_company_id_and_provider():
    token = sign_oauth_state(company_id="company-1", provider="quickbooks")
    company_id, provider = verify_oauth_state(token)
    assert company_id == "company-1"
    assert provider == "quickbooks"


def test_tampered_token_is_rejected():
    token = sign_oauth_state(company_id="company-1", provider="quickbooks")
    # Tamper position -2, not -1 — see test_security.py's
    # test_token_rejects_tampering for why: base64url-encoding a 32-byte
    # HMAC-SHA256 signature leaves the final character with only 4
    # meaningful bits, so substituting it has a real chance of decoding to
    # the same signature byte (nondeterministic flake). Position -2 is
    # fully meaningful, so tampering it reliably changes the signature.
    tampered = token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1]
    with pytest.raises(InvalidOAuthStateError):
        verify_oauth_state(tampered)


def test_expired_token_is_rejected():
    now = datetime.now(timezone.utc)
    expired_payload = {
        "company_id": "company-1",
        "provider": "quickbooks",
        "aud": "integration_oauth_state",
        "iat": now - timedelta(minutes=20),
        "exp": now - timedelta(minutes=10),
    }
    expired_token = jwt.encode(expired_payload, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(InvalidOAuthStateError):
        verify_oauth_state(expired_token)


def test_a_real_access_token_is_rejected_as_a_state_token():
    """A real login JWT (app/core/security.py's create_access_token) must
    NOT be accepted as a valid OAuth state — proves the distinct `aud`
    claim genuinely separates the two token purposes, not just by
    convention."""
    from app.core.security import create_access_token

    login_token = create_access_token(user_id="user-1", default_company_id="company-1")
    with pytest.raises(InvalidOAuthStateError):
        verify_oauth_state(login_token)
