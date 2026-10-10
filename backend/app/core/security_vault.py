"""Vault cryptography and egress (SSRF) protection.

Every new secret is sealed with AES-256-GCM (Group 70): a fresh 96-bit nonce per record, the 128-bit
tag over the ciphertext and an optional *context* (associated data, e.g. ``vault-account:<id>:password``)
so a ciphertext copied into another row or field no longer opens. The AES key is HKDF-SHA256 from the
MASTER_VAULT_KEY (never the key itself), and every token names the key it was sealed under:

    v2.<base64url(key id (4) | nonce (12) | ciphertext | tag (16))>

Fernet tokens written before Group 70 still open (``gAAAAA…``); ``rotate`` re-seals any token, Fernet
or GCM, under the current primary key. ``blind_index`` is a keyed HMAC for equality lookups (an
account's username) without storing the plaintext: one digest per key, so a lookup matches rows
written under a previous key too.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import socket
from collections.abc import Sequence
from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

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
GCM_PREFIX = "v2."  # a Fernet token always starts "gAAAAA": the two can never be confused
_KEY_ID_BYTES = 4
_NONCE_BYTES = 12
_TAG_BYTES = 16
_AAD_PREFIX = b"betdoc-vault-v2|"


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


def _derive(master: bytes, purpose: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"betdoc-vault-v2", info=purpose).derive(master)


def _b64decode(token: str) -> bytes:
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))


class _GcmKey:
    __slots__ = ("aead", "index_key", "key_id")

    def __init__(self, fernet_key: str) -> None:
        master = base64.urlsafe_b64decode(fernet_key.encode("ascii"))  # the Fernet key's 32 raw bytes
        aes_key = _derive(master, b"aes-256-gcm/v1")
        self.aead = AESGCM(aes_key)
        self.key_id = hashlib.sha256(aes_key).digest()[:_KEY_ID_BYTES]  # names the key, reveals nothing of it
        self.index_key = _derive(master, b"blind-index/v1")


class VaultCrypto:
    """Seals secrets at rest (AES-256-GCM) and still opens the Fernet tokens written before.
    Previous keys allow zero-downtime rotation."""

    __slots__ = ("_cipher", "_gcm", "_gcm_by_id")

    def __init__(self, primary_key: str, previous_keys: Sequence[str] = ()) -> None:
        fernets = [_build_fernet(primary_key, "MASTER_VAULT_KEY")]
        fernets.extend(_build_fernet(k, f"MASTER_VAULT_PREVIOUS_KEYS[{i}]") for i, k in enumerate(previous_keys))
        self._cipher = MultiFernet(fernets)
        keys = [_GcmKey(primary_key), *(_GcmKey(k) for k in previous_keys)]
        self._gcm = keys[0]
        self._gcm_by_id = {k.key_id: k for k in reversed(keys)}  # the primary wins a (vanishingly unlikely) id clash

    @classmethod
    def from_settings(cls, settings: Settings) -> VaultCrypto:
        if settings.master_vault_key is None:
            raise VaultConfigurationError("MASTER_VAULT_KEY is not set.")
        return cls(
            settings.master_vault_key.get_secret_value(),
            [k.get_secret_value() for k in settings.master_vault_previous_keys],
        )

    # ------------------------------------------------------------ AES-256-GCM
    def _seal(self, plain: bytes, context: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)  # unique per record: a nonce is never reused under one key
        sealed = self._gcm.aead.encrypt(nonce, plain, _AAD_PREFIX + context.encode("utf-8"))
        return GCM_PREFIX + base64.urlsafe_b64encode(self._gcm.key_id + nonce + sealed).decode("ascii").rstrip("=")

    def _open(self, cipher_text: str, context: str) -> bytes:
        if not isinstance(cipher_text, str) or not cipher_text:
            raise VaultDecryptionError("Ciphertext could not be decrypted.")
        if not cipher_text.startswith(GCM_PREFIX):
            try:
                return self._cipher.decrypt(cipher_text.encode("ascii"))  # a Fernet token from before Group 70
            except (InvalidToken, UnicodeError) as exc:
                raise VaultDecryptionError("Ciphertext could not be decrypted.") from exc
        try:
            raw = _b64decode(cipher_text[len(GCM_PREFIX):])
        except (ValueError, UnicodeError) as exc:
            raise VaultDecryptionError("Ciphertext is not valid base64.") from exc
        if len(raw) < _KEY_ID_BYTES + _NONCE_BYTES + _TAG_BYTES:
            raise VaultDecryptionError("Ciphertext is truncated.")
        key = self._gcm_by_id.get(raw[:_KEY_ID_BYTES])
        if key is None:
            raise VaultDecryptionError("Ciphertext was sealed under a key this vault does not hold.")
        nonce, sealed = raw[_KEY_ID_BYTES:_KEY_ID_BYTES + _NONCE_BYTES], raw[_KEY_ID_BYTES + _NONCE_BYTES:]
        try:
            return key.aead.decrypt(nonce, sealed, _AAD_PREFIX + context.encode("utf-8"))
        except InvalidTag as exc:
            raise VaultDecryptionError("Ciphertext was tampered with, or belongs to another record or field.") from exc

    def encrypt_key(self, plain_key: str, *, context: str = "") -> str:
        if not isinstance(plain_key, str) or not plain_key:
            raise ValueError("plain_key must be a non-empty string.")
        return self._seal(plain_key.encode("utf-8"), context)

    def decrypt_key(self, cipher_text: str, *, context: str = "") -> str:
        try:
            return self._open(cipher_text, context).decode("utf-8")
        except UnicodeError as exc:
            raise VaultDecryptionError("API key ciphertext could not be decrypted.") from exc

    def decrypt_into(self, cipher_text: str, *, context: str = "") -> bytearray:
        """Plaintext as a mutable buffer the caller can wipe (``wipe``) as soon as it is used."""
        return bytearray(self._open(cipher_text, context))

    def rotate(self, cipher_text: str, *, context: str = "") -> str:
        """Re-seal under the current primary key (use after promoting a new MASTER_VAULT_KEY)."""
        buffer = bytearray(self._open(cipher_text, context))
        try:
            return self._seal(bytes(buffer), context)
        finally:
            buffer[:] = bytes(len(buffer))

    def is_current(self, cipher_text: str) -> bool:
        """Sealed with AES-256-GCM under the current primary key (else ``rotate`` it)."""
        if not cipher_text.startswith(GCM_PREFIX):
            return False
        try:
            return _b64decode(cipher_text[len(GCM_PREFIX):])[:_KEY_ID_BYTES] == self._gcm.key_id
        except (ValueError, UnicodeError):
            return False

    # ------------------------------------------------------------ blind index
    def blind_index(self, value: str) -> str:
        """HMAC-SHA256 under the current key: store it to find the row again without the plaintext."""
        return hmac.new(self._gcm.index_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def blind_indexes(self, value: str) -> list[str]:
        """The digest under every key the vault holds, current first: rows indexed before a rotation still match."""
        digests = [self.blind_index(value)]
        for key in self._gcm_by_id.values():
            digest = hmac.new(key.index_key, value.encode("utf-8"), hashlib.sha256).hexdigest()
            if digest not in digests:
                digests.append(digest)
        return digests


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
