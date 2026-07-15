"""Task 4.5 (design spec Section 3): signs and verifies the short-lived
`state` parameter GET /integrations/{provider}/connect hands to the fake
authorization URL, and GET /integrations/{provider}/callback later
receives back. The signed state IS callback's entire authentication —
callback has no CurrentUser (an external redirect cannot carry a bearer
token, the same structural reality /invitations/{id}/accept already
established) — so a forged or expired state must be rejected, not merely
logged.

Reuses settings.jwt_secret (not a second secret) under a distinct `aud`
claim ("integration_oauth_state") that app/core/security.py's real login
tokens never set — this is what stops a real, valid login JWT (which a
malicious caller could already possess for their own account) from also
being accepted as a valid OAuth state for some OTHER company_id. verify_
oauth_state explicitly requires and checks this claim, it does not just
happen to ignore it.
"""
from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings

_AUDIENCE = "integration_oauth_state"
_TTL_MINUTES = 10


class InvalidOAuthStateError(Exception):
    """Raised for a missing, tampered, expired, or wrong-audience state
    token. The caller (GET /integrations/{provider}/callback, Task 4.9) is
    responsible for turning this into an HTTP 400, not this module."""


def sign_oauth_state(*, company_id: str, provider: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "company_id": company_id,
        "provider": provider,
        "aud": _AUDIENCE,
        "iat": now,
        "exp": now + timedelta(minutes=_TTL_MINUTES),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_oauth_state(token: str) -> tuple[str, str]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], audience=_AUDIENCE)
    except jwt.PyJWTError as exc:
        raise InvalidOAuthStateError(str(exc)) from exc

    return payload["company_id"], payload["provider"]
