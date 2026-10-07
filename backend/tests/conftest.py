"""Root test bootstrap.

Nothing sensitive is hardcoded here:
* secrets are generated fresh for every test session and never written anywhere;
* non-secret endpoints come from ``backend/.env.test``;
* real environment variables always win (``setdefault``).
"""

import os
import secrets
from pathlib import Path

from dotenv import dotenv_values

_ENV_TEST_FILE = Path(__file__).resolve().parents[1] / ".env.test"

for _key, _value in dotenv_values(_ENV_TEST_FILE).items():
    if _value is not None:
        os.environ.setdefault(_key, _value)

_GENERATED_SECRETS = ("MASTER_VAULT_KEY", "OMNI_ADMIN_TOKEN")
_SECRET_BYTES = 48

for _name in _GENERATED_SECRETS:
    os.environ.setdefault(_name, secrets.token_urlsafe(_SECRET_BYTES))
