from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from cryptography.fernet import Fernet, InvalidToken
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# bcrypt only reads the first 72 bytes.
BCRYPT_MAX_BYTES = 72


class PasswordTooLongError(ValueError):
    pass


class CredentialDecryptionError(Exception):
    """The ciphertext is corrupt, was tampered with, or was encrypted with another key."""


def verify_password(plain_password: str, hashed_password: str) -> bool:
    if len(plain_password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        return False
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except (ValueError, TypeError):
        # Malformed or unknown hash format: treat as a failed login, never a 500
        return False


def get_password_hash(password: str) -> str:
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise PasswordTooLongError(f"Password must not exceed {BCRYPT_MAX_BYTES} bytes")
    return pwd_context.hash(password)


def create_access_token(subject: str | Any, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),  # PyJWT 2.10+ rejects a non-string "sub"
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.SECRET_KEY.get_secret_value(), algorithm=settings.ALGORITHM)


# --- Fernet credential encryption ---
fernet_cipher = Fernet(
    settings.ENCRYPTION_KEY.get_secret_value()
    if hasattr(settings.ENCRYPTION_KEY, "get_secret_value")
    else settings.ENCRYPTION_KEY
)


def encrypt_api_key(plain_text: str) -> str:
    if not plain_text:
        raise ValueError("Cannot encrypt an empty credential")
    return fernet_cipher.encrypt(plain_text.encode("utf-8")).decode("utf-8")


def decrypt_api_key(cipher_text: str) -> str:
    try:
        return fernet_cipher.decrypt(cipher_text.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        # `from None` drops the original exception so the ciphertext never ends up in tracebacks
        raise CredentialDecryptionError("Unable to decrypt exchange credential") from None
