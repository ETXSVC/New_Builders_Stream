"""TOTP MFA (docs/superpowers/specs/2026-07-16-mfa-totp-design.md).

Owns secret generation, otpauth URI construction, and replay-guarded
verification. Routes stay thin (the refresh_tokens.py convention). The
base32 secret is presentable exactly once, at enrollment; storage is
Fernet ciphertext via the existing token_encryption service (Decision 4's
deliberate key reuse — same application-secrets-at-rest threat class).
"""
import time

import pyotp

from app.models import User
from app.services.token_encryption import decrypt_token, encrypt_token

MFA_ISSUER = "Builders Stream"
TOTP_PERIOD_SECONDS = 30


def generate_enrollment(email: str) -> tuple[str, str, str]:
    """Returns (presentable_secret, otpauth_uri, encrypted_secret)."""
    secret = pyotp.random_base32()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=MFA_ISSUER)
    return secret, uri, encrypt_token(secret)


def verify_totp_code(user: User, code: str) -> bool:
    """Replay-guarded verification against the user's stored secret.

    Accepts the current 30s timestep plus one step of skew either side
    (valid_window=1 equivalent), but NEVER a timestep at or before the
    last successfully used one — an intercepted code is single-use even
    inside its validity window (spec Decision 5). On success, mutates
    user.totp_last_used_step in the CALLER's session; the caller owns the
    commit (login commits explicitly; the get_current_user routes commit
    after the handler returns).
    """
    secret = decrypt_token(user.totp_secret_encrypted)
    totp = pyotp.TOTP(secret)
    current_step = int(time.time()) // TOTP_PERIOD_SECONDS
    for offset in (0, -1, 1):
        candidate_step = current_step + offset
        expected = totp.at(candidate_step * TOTP_PERIOD_SECONDS)
        if pyotp.utils.strings_equal(code, expected):
            if user.totp_last_used_step is not None and candidate_step <= user.totp_last_used_step:
                return False  # replay of an already-spent (or older) code
            user.totp_last_used_step = candidate_step
            return True
    return False
