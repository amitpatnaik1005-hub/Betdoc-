"""Vault cryptography (Fernet/MultiFernet with rotation) and egress (SSRF) protection."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Sequence
from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import Settings, get_settings

__all__ = [
    "UnsafeTargetError",
    "VaultConfigurationError",
    "VaultCrypto",
    "VaultDecryptionError",
    "assert_public_target",
    "get_vault_crypto",
]

_FERNET_KEY_LENGTH = 44  # 32 bytes, URL-safe base64


class VaultConfigurationError(RuntimeError):
    """MASTER_VAULT_KEY (or a previous key) is missing or malformed. Raised at startup."""


class VaultDecryptionError(ValueError):
    """Ciphertext was tampered with or encrypted under an unknown key."""


class UnsafeTargetError(ValueError):
    """Outbound URL resolves to a forbidden scheme or a non-public address."""


def _build_fernet(key: str, label: str) -> Fernet:
    if len(key) != _FERNET_KEY_LENGTH:
        raise VaultConfigurationError(f"{label} must be a {_FERNET_KEY_LENGTH}-character Fernet key.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise VaultConfigurationError(f"{label} is not a valid Fernet key.") from exc


class VaultCrypto:
    """Encrypts provider API keys at rest. Previous keys allow zero-downtime rotation."""

    __slots__ = ("_cipher",)

    def __init__(self, primary_key: str, previous_keys: Sequence[str] = ()) -> None:
        fernets = [_build_fernet(primary_key, "MASTER_VAULT_KEY")]
        fernets.extend(_build_fernet(k, f"MASTER_VAULT_PREVIOUS_KEYS[{i}]") for i, k in enumerate(previous_keys))
        self._cipher = MultiFernet(fernets)

    @classmethod
    def from_settings(cls, settings: Settings) -> VaultCrypto:
        if settings.master_vault_key is None:
            raise VaultConfigurationError("MASTER_VAULT_KEY is not set.")
        return cls(
            settings.master_vault_key.get_secret_value(),
            [k.get_secret_value() for k in settings.master_vault_previous_keys],
        )

    def encrypt_key(self, plain_key: str) -> str:
        if not isinstance(plain_key, str) or not plain_key:
            raise ValueError("plain_key must be a non-empty string.")
        return self._cipher.encrypt(plain_key.encode("utf-8")).decode("ascii")

    def decrypt_key(self, cipher_text: str) -> str:
        try:
            return self._cipher.decrypt(cipher_text.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, AttributeError) as exc:
            raise VaultDecryptionError("API key ciphertext could not be decrypted.") from exc

    def rotate(self, cipher_text: str) -> str:
        """Re-encrypt under the current primary key (use after promoting a new MASTER_VAULT_KEY)."""
        try:
            return self._cipher.rotate(cipher_text.encode("ascii")).decode("ascii")
        except InvalidToken as exc:
            raise VaultDecryptionError("API key ciphertext could not be rotated.") from exc


@lru_cache(maxsize=1)
def get_vault_crypto() -> VaultCrypto:
    """Process-wide instance for non-FastAPI processes (Celery, dispatcher, WS translator)."""
    return VaultCrypto.from_settings(get_settings())


def assert_public_target(url: str, *, allowed_schemes: frozenset[str], allow_private: bool) -> None:
    """Reject non-allowed schemes and hosts resolving to private/loopback/link-local/reserved IPs.

    Blocking (DNS); call via ``asyncio.to_thread`` from async code.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in allowed_schemes:
        raise UnsafeTargetError(f"Scheme '{parts.scheme}' is not permitted.")
    host = parts.hostname
    if not host:
        raise UnsafeTargetError("URL has no host.")
    if allow_private:
        return
    default_port = {"https": 443, "wss": 443, "http": 80, "ws": 80}.get(parts.scheme.lower(), 443)
    try:
        infos = socket.getaddrinfo(host, parts.port or default_port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeTargetError(f"Host '{host}' could not be resolved.") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise UnsafeTargetError(f"Host '{host}' resolves to a non-public address.")
