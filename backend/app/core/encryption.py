"""Fernet-based API key vault: authenticated symmetric encryption + display masking."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

__all__ = ["DecryptionError", "EncryptionConfigurationError", "EncryptionService", "mask_api_key"]

# Fernet key format: 32 bytes, URL-safe Base64 encoded => exactly 44 ASCII characters.
_FERNET_KEY_LENGTH = 44


class EncryptionConfigurationError(ValueError):
    """Raised at startup when the configured key is missing or not a valid Fernet key."""


class DecryptionError(ValueError):
    """Raised when a ciphertext is tampered with, expired, or encrypted under another key."""


def mask_api_key(key: str, visible_chars: int = 4, mask_char: str = "*") -> str:
    """Mask everything after the last ``-`` except the trailing ``visible_chars``.

    ``sk-live-1234567890abcdef`` -> ``sk-live-************cdef``. Keys without a ``-`` are
    masked from the start. If the secret body is not longer than ``visible_chars``, it is fully masked.
    """
    if visible_chars < 0:
        raise ValueError("visible_chars must be >= 0.")
    if len(mask_char) != 1:
        raise ValueError("mask_char must be a single character.")
    prefix, separator, body = key.rpartition("-")
    head = f"{prefix}{separator}"
    if len(body) <= visible_chars:
        return head + mask_char * len(body)
    return head + mask_char * (len(body) - visible_chars) + body[len(body) - visible_chars :]


class EncryptionService:
    """Thread-safe wrapper around :class:`cryptography.fernet.Fernet`.

    Fernet output is non-deterministic (random IV and timestamp), so ciphertexts must never be
    used as lookup keys. Query by provider or id, then decrypt.
    """

    __slots__ = ("_fernet",)

    def __init__(self, secret_key: str) -> None:
        if not isinstance(secret_key, str) or len(secret_key) != _FERNET_KEY_LENGTH:
            raise EncryptionConfigurationError(
                f"Encryption key must be a {_FERNET_KEY_LENGTH}-character URL-safe Base64 Fernet key."
            )
        try:
            self._fernet = Fernet(secret_key.encode("ascii"))
        except (ValueError, TypeError) as exc:  # binascii.Error and UnicodeEncodeError subclass ValueError
            raise EncryptionConfigurationError("Encryption key is not a valid Fernet key.") from exc

    def encrypt_data(self, plain_text: str) -> str:
        if not isinstance(plain_text, str):
            raise TypeError("plain_text must be a str.")
        return self._fernet.encrypt(plain_text.encode("utf-8")).decode("ascii")

    def decrypt_data(self, cipher_text: str, ttl_seconds: int | None = None) -> str:
        if not isinstance(cipher_text, str):
            raise TypeError("cipher_text must be a str.")
        try:
            return self._fernet.decrypt(cipher_text.encode("ascii"), ttl=ttl_seconds).decode("utf-8")
        except (InvalidToken, UnicodeError) as exc:
            raise DecryptionError("Ciphertext is invalid, expired, or was encrypted with a different key.") from exc

    @staticmethod
    def mask_api_key(key: str, visible_chars: int = 4) -> str:
        return mask_api_key(key, visible_chars)
