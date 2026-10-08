"""Provider API keys loaded at runtime by variable name, so adding a provider needs no Settings field.

A config provider's spec names the variable holding its key (``auth.secret_env``, defaulting to
``OMNI_<PROVIDER_ID>_API_KEY``). ``ProviderSecrets`` is a pydantic-settings model that accepts every
variable in ``backend/.env`` as an extra field; real environment variables win over the file.
Values are ``SecretStr`` and never logged. Fleet Command's vault-encrypted key, when set, wins
over both (see ``app.services.omni_fleet.resolve_api_key``).
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderSecrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="allow", case_sensitive=False)

    def get(self, name: str) -> SecretStr | None:
        raw = (self.model_extra or {}).get(name.lower())
        return SecretStr(str(raw)) if raw not in (None, "") else None


@lru_cache(maxsize=1)
def _from_dotenv() -> ProviderSecrets:
    return ProviderSecrets()


def provider_secret(name: str) -> SecretStr | None:
    """The secret in variable ``name``: process environment first, then backend/.env."""
    value = os.environ.get(name) or os.environ.get(name.upper())
    if value:
        return SecretStr(value)
    return _from_dotenv().get(name)


def has_provider_secret(name: str) -> bool:
    return provider_secret(name) is not None
