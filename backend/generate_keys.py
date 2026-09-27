"""Generate fresh secrets for .env. Never commit the output."""

import secrets

from cryptography.fernet import Fernet


def main() -> None:
    # 32 random bytes as 64 hex characters, for JWT HS256 signing
    print(f"SECRET_KEY={secrets.token_hex(32)}")
    # 32 random bytes, url-safe base64, for Fernet credential encryption
    print(f"ENCRYPTION_KEY={Fernet.generate_key().decode('utf-8')}")
    # Service-to-service key for the Devraya scraper fleet (X-Ingestion-Key header)
    print(f"INGESTION_API_KEY={secrets.token_urlsafe(48)}")


if __name__ == "__main__":
    main()
