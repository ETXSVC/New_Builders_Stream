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
    uri = pyotp.TOTP(secret, interval=TOTP_PERIOD_SECONDS).provisioning_uri(
        name=email, issuer_name=MFA_ISSUER
    )
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
    # An undecryptable stored secret (operator rotated
    # integration_token_encryption_key with MFA-active users) propagates
    # TokenDecryptionError -> 500. Deliberate: a 401 would tell the user
    # their CODE is wrong, which they cannot fix; the 500 is the honest
    # signal for an operator-caused state (the block_if_read_only
    # documented-non-handling precedent).
    secret = decrypt_token(user.totp_secret_encrypted)
    totp = pyotp.TOTP(secret, interval=TOTP_PERIOD_SECONDS)
    current_step = int(time.time()) // TOTP_PERIOD_SECONDS
    for offset in (0, -1, 1):
        candidate_step = current_step + offset
        expected = totp.at(candidate_step * TOTP_PERIOD_SECONDS)
        if pyotp.utils.strings_equal(code, expected):
            # Check-then-set with no row lock: two concurrent requests
            # presenting the SAME code can both read a stale
            # totp_last_used_step and both pass (last-writer-wins on the
            # column). Spec Decision 5 promises one column + one
            # comparison, and the attacker needs the password AND the
            # intercepted code inside a milliseconds window — accepted;
            # strict RFC 6238 single-use would need FOR UPDATE or a
            # guarded-UPDATE rowcount check here.
            if user.totp_last_used_step is not None and candidate_step <= user.totp_last_used_step:
                return False  # replay of an already-spent (or older) code
            user.totp_last_used_step = candidate_step
            return True
    return False
