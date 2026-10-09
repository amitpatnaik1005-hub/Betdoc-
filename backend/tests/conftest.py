"""Root test bootstrap.

Nothing sensitive is hardcoded here:
* secrets are generated fresh for every test session and never written anywhere;
* non-secret endpoints come from ``backend/.env.test``;
* real environment variables win (``setdefault``), with one exception: Redis.

Redis isolation (Group 68): every Redis client a test can reach, directly (``TEST_REDIS_URL``) or
through the app (``REDIS_URL``, ``CELERY_BROKER_URL``, read by ``get_settings()``), is forced onto
one dedicated database index. A real ``REDIS_URL`` exported in the shell no longer wins, and an
index that is 0 or the one the developer's own ``backend/.env`` uses refuses to start the session,
so a test can never write to the Redis the running stack reads.
"""

import os
import secrets
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from dotenv import dotenv_values

_BACKEND = Path(__file__).resolve().parents[1]
_ENV_TEST_FILE = _BACKEND / ".env.test"
_ENV_DEV_FILE = _BACKEND / ".env"

# 127.0.0.1, not localhost: on some Windows hosts localhost resolves to an IPv6 relay that hangs.
DEFAULT_TEST_REDIS_URL = "redis://127.0.0.1:6379/15"
_REDIS_VARIABLES = ("REDIS_URL", "CELERY_BROKER_URL", "TEST_REDIS_URL")

for _key, _value in dotenv_values(_ENV_TEST_FILE).items():
    if _value is not None and _key not in _REDIS_VARIABLES:
        os.environ.setdefault(_key, _value)

_GENERATED_SECRETS = ("MASTER_VAULT_KEY", "OMNI_ADMIN_TOKEN")
_SECRET_BYTES = 48

for _name in _GENERATED_SECRETS:
    os.environ.setdefault(_name, secrets.token_urlsafe(_SECRET_BYTES))


def _redis_index(url: str) -> int:
    """The database index a redis:// URL selects (``/n`` path or ``?db=n``; none means 0)."""
    parts = urlsplit(url)
    path = parts.path.strip("/")
    if path:
        return int(path)
    for pair in parts.query.split("&"):
        name, _, value = pair.partition("=")
        if name == "db" and value:
            return int(value)
    return 0


def _endpoint(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    host = (parts.hostname or "localhost").lower()
    return ("127.0.0.1" if host in ("localhost", "::1") else host), parts.port or 6379


def isolated_redis_url() -> str:
    """The one Redis URL the whole test session may use; raises UsageError when it is unsafe."""
    url = os.environ.get("TEST_REDIS_URL") or DEFAULT_TEST_REDIS_URL
    try:
        index = _redis_index(url)
    except ValueError as exc:
        raise pytest.UsageError("TEST_REDIS_URL must select a numeric database index") from exc
    if index == 0:
        raise pytest.UsageError("TEST_REDIS_URL selects Redis database 0, the application default; use a dedicated index (15)")
    dev_url = dotenv_values(_ENV_DEV_FILE).get("REDIS_URL") if _ENV_DEV_FILE.exists() else None
    if dev_url:
        try:
            clash = _endpoint(dev_url) == _endpoint(url) and _redis_index(dev_url) == index
        except ValueError:
            clash = False
        if clash:
            raise pytest.UsageError("TEST_REDIS_URL is the same Redis database as backend/.env's REDIS_URL; tests must never share it")
    return url


TEST_REDIS_URL = isolated_redis_url()
for _name in _REDIS_VARIABLES:
    os.environ[_name] = TEST_REDIS_URL  # forced, not setdefault: nothing may point the app elsewhere


@pytest.fixture(scope="session", autouse=True)
def _redis_isolation_guard() -> None:
    """Fails the session if anything imported Settings before the URLs above were forced."""
    from app.core.config import get_settings  # noqa: PLC0415 - after the environment is set

    settings = get_settings()
    for name, value in (("REDIS_URL", settings.REDIS_URL), ("CELERY_BROKER_URL", settings.celery_broker_url)):
        if value.get_secret_value() != TEST_REDIS_URL:
            raise pytest.UsageError(f"{name} escaped the isolated test Redis database")


# Every marker a test module sets on the index it is about to use starts with this.
SUITE_MARKER_PREFIX = "betdoc:test"
SUITE_MARKER = "betdoc:test-suite"
CLAIMED_ENV = "BETDOC_TEST_REDIS_CLAIMED"


@pytest.fixture(scope="session", autouse=True)
def _claim_test_redis(_redis_isolation_guard: None) -> Iterator[None]:
    """Claim the isolated index once per session. Empty, or holding only what an earlier run of this
    suite left behind (a marker key is there), it is flushed and ours; anything else stops the session.

    Once claimed, everything that appears in it was written by this session, including the app's own
    fire-and-forget writes that land after a test's fixture has flushed (a Sentinel once-key, a Nalanda
    record), so the per-module fixtures may flush it without a marker of their own (``CLAIMED_ENV``).
    The index is flushed again at the end, leaving only the suite's marker. Redis down: nothing to claim
    (the Redis-backed tests skip themselves)."""
    import redis as redis_sync  # noqa: PLC0415

    client = redis_sync.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=2, socket_timeout=5)
    try:
        client.ping()
    except (redis_sync.RedisError, OSError):
        client.close()
        yield
        return
    try:
        if client.dbsize() and not any(True for _ in client.scan_iter(match=f"{SUITE_MARKER_PREFIX}*", count=1000)):
            raise pytest.UsageError(
                f"Redis database {_redis_index(TEST_REDIS_URL)} holds data that is not the test suite's; clear it or point TEST_REDIS_URL at an empty index"
            )
        client.flushdb()
        client.set(SUITE_MARKER, "1")
        os.environ[CLAIMED_ENV] = "1"
        yield
        client.flushdb()
        client.set(SUITE_MARKER, "1")
    finally:
        os.environ.pop(CLAIMED_ENV, None)
        client.close()
