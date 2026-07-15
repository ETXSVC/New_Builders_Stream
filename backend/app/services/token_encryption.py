"""Task 4.3 (design spec Section 1): Fernet symmetric encryption for OAuth
tokens at rest, per docs/07-security-compliance.md Section 4's existing
requirement. Fernet (not raw AES) because it bundles authenticated
encryption (AES-128-CBC + HMAC) and timestamp/versioning in one
self-contained token format — the standard, low-footprint choice for
"encrypt this string, decrypt it back later" in Python, matching this
codebase's own "use the standard library/battle-tested primitive, don't
hand-roll crypto" discipline already established for password hashing
(argon2-cffi) and JWT signing (pyjwt).
"""
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class TokenDecryptionError(Exception):
    pass


def _fernet() -> Fernet:
    # Reads settings.integration_token_encryption_key fresh on every call,
    # matching app.core.security's create_access_token/decode_access_token
    # convention of reading settings.jwt_secret at call time rather than
    # baking a settings-derived secret into a module-level object at import
    # time — keeps this testable via monkeypatching settings and avoids an
    # import-order-dependent fail-fast point for a malformed key.
    return Fernet(settings.integration_token_encryption_key.encode())


def encrypt_token(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise TokenDecryptionError(str(exc)) from exc
