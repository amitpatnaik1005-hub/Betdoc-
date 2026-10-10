"""Venue sessions: bearer tokens that are always valid when a shot fires.

* Tokens live in Redis, encrypted with the vault (MASTER_VAULT_KEY), with a TTL equal to their
  remaining life. A token with less than ``SNIPER_SESSION_REFRESH_MARGIN_SECONDS`` (5 min) left is
  refreshed before use, and ``sniper.refresh_sessions`` refreshes them proactively every minute, so
  an order never waits on a login and never meets a stale token.
* Refresh is single-flight: an asyncio lock per venue in this process and a Redis lock across
  workers; whoever wins refreshes, everyone else re-reads the fresh token.
* A refresh uses the refresh token when the venue issued one (``refresh_path``), and falls back to
  a full client-credentials login if that is refused.
* After a ``401`` the adapter asks again with ``stale=<the rejected token>``: the cache is bypassed
  unless another caller has already replaced that token.
* Venue credentials are one encrypted JSON blob, decrypted into a wipeable buffer only for the
  token request and zeroed straight after. (CPython cannot wipe the ``str`` copies a request needs;
  they are dropped immediately and never stored.)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import LockError, RedisError

from app.adapters.execution.venue import VenueConfig
from app.core.config import Settings
from app.core.security_vault import VaultCrypto, VaultDecryptionError

logger = logging.getLogger("betdoc.sniper")

DEFAULT_TOKEN_SECONDS = 3600


class SessionError(RuntimeError):
    """No usable session. ``reason`` is the audit code: AUTH_FAILED, AUTH_UNREACHABLE, NO_CREDENTIALS, ..."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SessionToken:
    access_token: str
    expires_at: datetime
    refresh_token: str | None = None

    def seconds_left(self, now: datetime) -> float:
        return (self.expires_at - now).total_seconds()

    def encode(self) -> str:
        return json.dumps({"a": self.access_token, "e": self.expires_at.timestamp(), "r": self.refresh_token}, separators=(",", ":"))

    @classmethod
    def decode(cls, raw: str) -> SessionToken | None:
        try:
            data = json.loads(raw)
            return cls(str(data["a"]), datetime.fromtimestamp(float(data["e"]), UTC), data.get("r"))
        except (ValueError, KeyError, TypeError):
            return None


def wipe(buffer: bytearray) -> None:
    buffer[:] = bytes(len(buffer))


@contextlib.contextmanager
def revealed(vault: VaultCrypto | None, cipher_text: str | None) -> Iterator[dict[str, str]]:
    """Decrypt a venue's credential blob for the duration of one ``with`` block."""
    if vault is None:
        raise SessionError("NO_VAULT", "MASTER_VAULT_KEY is not configured: venue credentials cannot be decrypted")
    if not cipher_text:
        raise SessionError("NO_CREDENTIALS", "This venue has no credentials stored")
    try:
        buffer = vault.decrypt_into(cipher_text)
    except VaultDecryptionError as exc:
        raise SessionError("CREDENTIALS_UNREADABLE", "Stored venue credentials cannot be decrypted (MASTER_VAULT_KEY changed?)") from exc
    try:
        data = json.loads(buffer)
        if not isinstance(data, dict):
            raise SessionError("CREDENTIALS_UNREADABLE", "Venue credentials are not a JSON object")
        creds = {str(k): str(v) for k, v in data.items()}
        try:
            yield creds
        finally:
            creds.clear()
    except json.JSONDecodeError as exc:
        raise SessionError("CREDENTIALS_UNREADABLE", "Venue credentials are not JSON") from exc
    finally:
        wipe(buffer)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SessionManager:
    def __init__(
        self,
        redis: Redis | None,
        settings: Settings,
        vault: VaultCrypto | None,
        http_for: Callable[[VenueConfig], httpx.AsyncClient],
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.redis = redis
        self.settings = settings
        self.vault = vault
        self.http_for = http_for
        self.clock = clock
        self.margin = timedelta(seconds=settings.SNIPER_SESSION_REFRESH_MARGIN_SECONDS)
        self._locks: dict[str, asyncio.Lock] = {}
        self._memory: dict[str, SessionToken] = {}  # Redis down: sessions still work per process

    def key(self, venue_id: str) -> str:
        return f"{self.settings.SNIPER_PREFIX}:session:{venue_id}"

    # -------------------------------------------------------------- public
    async def bearer(self, venue: VenueConfig, *, stale: str | None = None) -> str:
        """A token valid for at least the refresh margin. ``stale``: a token the venue just refused."""
        if venue.auth_type == "static_bearer":
            with revealed(self.vault, venue.encrypted_credentials) as creds:
                token = creds.get("api_key", "")
            if not token:
                raise SessionError("NO_CREDENTIALS", "static_bearer venue credentials need an api_key")
            return token
        cached = await self._load(venue.session_key)
        if cached is not None and cached.access_token != stale and cached.seconds_left(self.clock()) > self.margin.total_seconds():
            return cached.access_token
        return (await self._refresh(venue, stale=stale)).access_token

    async def refresh_due(self, venues: list[VenueConfig]) -> dict[str, str]:
        """Proactive pass (beat): refresh every OAuth venue inside the margin. Returns venue -> outcome."""
        outcomes: dict[str, str] = {}
        for venue in venues:
            if venue.auth_type == "static_bearer" or not venue.is_enabled:
                continue
            cached = await self._load(venue.session_key)
            if cached is not None and cached.seconds_left(self.clock()) > self.margin.total_seconds():
                outcomes[venue.id] = "fresh"
                continue
            try:
                await self._refresh(venue, stale=cached.access_token if cached else None)
                outcomes[venue.id] = "refreshed"
            except SessionError as exc:
                outcomes[venue.id] = exc.reason
                logger.warning("Session refresh for %s failed: %s", venue.id, exc.reason)
        return outcomes

    async def describe(self, venue: VenueConfig) -> dict[str, Any]:
        """For the terminal: is there a session, and how long has it got (never the token itself)."""
        if venue.auth_type == "static_bearer":
            return {"authenticated": bool(venue.encrypted_credentials), "expires_at": None, "seconds_left": None}
        cached = await self._load(venue.session_key)
        if cached is None:
            return {"authenticated": False, "expires_at": None, "seconds_left": None}
        return {"authenticated": True, "expires_at": cached.expires_at.isoformat(), "seconds_left": int(cached.seconds_left(self.clock()))}

    # -------------------------------------------------------------- refresh (single-flight)
    async def _refresh(self, venue: VenueConfig, *, stale: str | None) -> SessionToken:
        lock = self._locks.setdefault(venue.session_key, asyncio.Lock())
        async with lock, self._cluster_lock(venue.session_key):
            cached = await self._load(venue.session_key)
            if cached is not None and cached.access_token != stale and cached.seconds_left(self.clock()) > self.margin.total_seconds():
                return cached  # another caller refreshed while we waited
            token: SessionToken | None = None
            if cached is not None and cached.refresh_token and venue.refresh_path:
                try:
                    token = await self._grant(venue, venue.refresh_path, {"grant_type": "refresh_token", "refresh_token": cached.refresh_token})
                except SessionError as exc:
                    logger.info("Refresh token for %s refused (%s); logging in again", venue.id, exc.reason)
            if token is None:
                if not venue.token_path:
                    raise SessionError("NO_TOKEN_ENDPOINT", f"{venue.display_name} has no token endpoint")
                with revealed(self.vault, venue.encrypted_credentials) as creds:
                    form = {"grant_type": "client_credentials", "client_id": creds.get("client_id", ""), "client_secret": creds.get("client_secret", "")}
                    try:
                        token = await self._grant(venue, venue.token_path, form)
                    finally:
                        form.clear()
            await self._store(venue.session_key, token)
            logger.info("Session for %s refreshed; valid until %s", venue.id, token.expires_at.isoformat())
            return token

    @contextlib.asynccontextmanager
    async def _cluster_lock(self, venue_id: str):  # type: ignore[no-untyped-def]
        if self.redis is None:
            yield
            return
        lock = self.redis.lock(f"{self.key(venue_id)}:refresh", timeout=self.settings.SNIPER_AUTH_TIMEOUT_SECONDS * 3, blocking_timeout=self.settings.SNIPER_AUTH_TIMEOUT_SECONDS * 2)
        acquired = False
        try:
            acquired = await lock.acquire()
        except (RedisError, OSError):
            acquired = False  # Redis down: the per-process lock still prevents a stampede here
        try:
            yield
        finally:
            if acquired:
                with contextlib.suppress(LockError, RedisError, OSError):
                    await lock.release()

    async def _grant(self, venue: VenueConfig, path: str, form: dict[str, str]) -> SessionToken:
        try:
            response = await self.http_for(venue).post(venue.url(path), data=form, timeout=httpx.Timeout(self.settings.SNIPER_AUTH_TIMEOUT_SECONDS))
        except httpx.HTTPError as exc:
            raise SessionError("AUTH_UNREACHABLE", f"{venue.display_name} token endpoint unreachable ({type(exc).__name__})") from exc
        if response.status_code in (400, 401, 403):
            raise SessionError("AUTH_FAILED", f"{venue.display_name} refused the credentials ({response.status_code})")
        if response.status_code != 200:
            raise SessionError("AUTH_UNAVAILABLE", f"{venue.display_name} token endpoint answered {response.status_code}")
        try:
            body = response.json()
            access = str(body["access_token"])
            lifetime = int(body.get("expires_in") or DEFAULT_TOKEN_SECONDS)
        except (ValueError, KeyError, TypeError) as exc:
            raise SessionError("AUTH_BAD_RESPONSE", f"{venue.display_name} token response is not usable") from exc
        if not access or lifetime <= 0:
            raise SessionError("AUTH_BAD_RESPONSE", f"{venue.display_name} issued an empty or expired token")
        refresh = body.get("refresh_token")
        return SessionToken(access, self.clock() + timedelta(seconds=lifetime), str(refresh) if refresh else None)

    # -------------------------------------------------------------- storage (encrypted)
    async def _store(self, venue_id: str, token: SessionToken) -> None:
        self._memory[venue_id] = token
        ttl = int(token.seconds_left(self.clock()))
        if self.redis is None or self.vault is None or ttl <= 0:
            return
        with contextlib.suppress(RedisError, OSError):
            await self.redis.set(self.key(venue_id), self.vault.encrypt_key(token.encode()), ex=ttl)

    async def _load(self, venue_id: str) -> SessionToken | None:
        if self.redis is not None and self.vault is not None:
            try:
                raw = await self.redis.get(self.key(venue_id))
            except (RedisError, OSError):
                raw = None
            if raw:
                try:
                    buffer = self.vault.decrypt_into(raw)
                except VaultDecryptionError:
                    buffer = None
                if buffer is not None:
                    try:
                        token = SessionToken.decode(buffer.decode("utf-8"))
                    finally:
                        wipe(buffer)
                    if token is not None:
                        self._memory[venue_id] = token
                        return token
        token = self._memory.get(venue_id)
        return token if token is not None and token.seconds_left(self.clock()) > 0 else None
