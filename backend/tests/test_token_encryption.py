"""Task 4.3 (design spec Section 1): Fernet-based encryption for OAuth
tokens at rest."""
import pytest

from app.services.token_encryption import TokenDecryptionError, decrypt_token, encrypt_token


def test_encrypt_then_decrypt_round_trips_to_the_original_value():
    plaintext = "fake_access_token_abc123"
    ciphertext = encrypt_token(plaintext)

    assert ciphertext != plaintext
    assert decrypt_token(ciphertext) == plaintext


def test_ciphertext_is_not_human_readable():
    ciphertext = encrypt_token("a_real_looking_secret_value")
    assert "a_real_looking_secret_value" not in ciphertext


def test_decrypt_invalid_ciphertext_raises_token_decryption_error():
    with pytest.raises(TokenDecryptionError):
        decrypt_token("not-a-real-fernet-token")
