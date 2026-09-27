from typing import Literal, List

from cryptography.fernet import Fernet
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    PROJECT_NAME: str = "BetDoc"

    # SecretStr keeps the DB password out of repr(), logs and tracebacks
    DATABASE_URL: SecretStr
    SECRET_KEY: SecretStr
    ENCRYPTION_KEY: SecretStr
    INGESTION_API_KEY: SecretStr

    # FIX: Moved CORS origins to settings to prevent hardcoding in production
    BACKEND_CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    ODDS_API_KEY: SecretStr | None = None
    ODDS_POLLING_INTERVAL_SEC: int = 60
    ODDS_API_BASE_URL: str = "https://api.the-odds-api.com/v4"
    ODDS_SPORT_KEYS: str = "soccer_epl"
    ODDS_QUOTA_FLOOR: int = 10

    @property
    def odds_sport_keys(self) -> List[str]:
        return [s.strip() for s in self.ODDS_SPORT_KEYS.split(",") if s.strip()]

    # Literal type: an env var can't downgrade the algorithm (e.g. to "none")
    ALGORITHM: Literal["HS256"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    @field_validator("DATABASE_URL")
    @classmethod
    def validate_async_postgres(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value().startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the async driver: postgresql+asyncpg://")
        return v

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters for HS256")
        return v

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def validate_fernet_key(cls, v: SecretStr) -> SecretStr:
        # Fail at startup, not at the first encrypt/decrypt call
        try:
            Fernet(v.get_secret_value())
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "ENCRYPTION_KEY must be a 32-byte url-safe base64 key "
                "(generate with Fernet.generate_key())"
            ) from exc
        return v

    @field_validator("INGESTION_API_KEY")
    @classmethod
    def validate_ingestion_key(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("INGESTION_API_KEY must be at least 32 characters")
        return v

    @field_validator("ACCESS_TOKEN_EXPIRE_MINUTES")
    @classmethod
    def validate_expiry(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("ACCESS_TOKEN_EXPIRE_MINUTES must be positive")
        return v


settings = Settings()  # type: ignore[call-arg]
