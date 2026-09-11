
"""Async client for a remote KV-v2 configuration server (HashiCorp Vault style).



Credentials are held in memory only, with a bounded TTL. When the remote

server is unavailable (HTTP 503, connection failure, or timeout) the provider

degrades to an encrypted local cache decoded through the injected fallback

encoder. Authorisation failures are never masked by the fallback path: a 403

is a hard :class:`ConfigError`.

"""



from __future__ import annotations



import asyncio

import logging

import time

from dataclasses import dataclass, field

from pathlib import Path

from typing import TYPE_CHECKING, Any, Final, Mapping, Protocol, runtime_checkable



import aiohttp



if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle

    from betdoc.infrastructure.config.encoder import DataEncoder



__all__ = ["ConfigError", "ConfigProvider", "FallbackEncoder", "ServiceCredentials"]



_LOG: Final[logging.Logger] = logging.getLogger(__name__)



DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0

DEFAULT_CREDENTIAL_TTL_SECONDS: Final[float] = 900.0

DEFAULT_MAX_ATTEMPTS: Final[int] = 3

DEFAULT_BACKOFF_SECONDS: Final[float] = 0.5

DEFAULT_MOUNT: Final[str] = "secret"



_RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})





class ConfigError(RuntimeError):

    """Raised when configuration cannot be resolved from any source.



    Parameters

    ----------

    message:

        Human readable description, safe for logs. Never contains secrets.

    code:

        Stable machine readable code, e.g. ``FORBIDDEN``, ``NOT_FOUND``,

        ``UNAVAILABLE``, ``DECODE_FAILED``, ``MALFORMED``.

    service:

        The logical service whose configuration failed to resolve, if known.

    """



    __slots__ = ("code", "service")



    def __init__(

        self,

        message: str,

        *,

        code: str = "CONFIG_ERROR",

        service: str | None = None,

    ) -> None:

        super().__init__(message)

        self.code: str = code

        self.service: str | None = service



    def __str__(self) -> str:

        if self.service is not None:

            return f"[{self.code}] {self.service}: {super().__str__()}"

        return f"[{self.code}] {super().__str__()}"





@runtime_checkable

class FallbackEncoder(Protocol):

    """Structural contract required of the local-cache fallback encoder."""



    def save_encoded(self, path: str | Path, data: Mapping[str, Any]) -> None:

        """Serialise, encrypt and atomically persist ``data`` at ``path``."""



    def load_encoded(self, path: str | Path) -> dict[str, Any]:

        """Read and decrypt the payload previously written to ``path``."""





@dataclass(frozen=True, slots=True)

class ServiceCredentials:

    """Immutable credential bundle for a single downstream service.



    ``secret`` values are deliberately excluded from ``__repr__`` so that

    accidental logging of the dataclass cannot leak key material.

    """



    service: str

    api_key: str = field(repr=False, default="")

    app_key: str = field(repr=False, default="")

    username: str = field(repr=False, default="")

    password: str = field(repr=False, default="")

    extra: Mapping[str, str] = field(repr=False, default_factory=dict)

    version: int = 0

    fetched_at: float = 0.0

    from_cache: bool = False



    @classmethod

    def from_mapping(

        cls,

        service: str,

        payload: Mapping[str, Any],

        *,

        version: int = 0,

        fetched_at: float | None = None,

        from_cache: bool = False,

    ) -> "ServiceCredentials":

        """Build credentials from a raw KV-v2 ``data.data`` mapping.



        Unrecognised keys are preserved in :attr:`extra` so that adapters can

        read bookmaker-specific fields without a provider change.

        """

        if not isinstance(payload, Mapping):

            raise ConfigError(

                "credential payload is not a mapping",

                code="MALFORMED",

                service=service,

            )



        known: Final[frozenset[str]] = frozenset(

            {"api_key", "app_key", "username", "password"}

        )

        extra: dict[str, str] = {

            str(key): str(value)

            for key, value in payload.items()

            if str(key) not in known and value is not None

        }

        return cls(

            service=service,

            api_key=str(payload.get("api_key", "")),

            app_key=str(payload.get("app_key", "")),

            username=str(payload.get("username", "")),

            password=str(payload.get("password", "")),

            extra=extra,

            version=version,

            fetched_at=time.monotonic() if fetched_at is None else fetched_at,

            from_cache=from_cache,

        )



    def to_mapping(self) -> dict[str, str]:

        """Flatten back to a plain mapping suitable for encrypted caching."""

        payload: dict[str, str] = {

            "api_key": self.api_key,

            "app_key": self.app_key,

            "username": self.username,

            "password": self.password,

        }

        payload.update({str(k): str(v) for k, v in self.extra.items()})

        return {key: value for key, value in payload.items() if value != ""}



    def require(self, *fields: str) -> None:

        """Assert that the named fields are populated.



        Raises

        ------

        ConfigError

            If any requested field is empty, with code ``INCOMPLETE``.

        """

        missing: list[str] = []

        for name in fields:

            value = getattr(self, name, None)

            if value in (None, ""):

                value = self.extra.get(name, "")

            if value in (None, ""):

                missing.append(name)

        if missing:

            raise ConfigError(

                f"missing required credential field(s): {', '.join(sorted(missing))}",

                code="INCOMPLETE",

                service=self.service,

            )



    def is_expired(self, ttl_seconds: float, *, now: float | None = None) -> bool:

        """Return whether this bundle has outlived ``ttl_seconds``."""

        if ttl_seconds <= 0:

            return False

        current = time.monotonic() if now is None else now

        return (current - self.fetched_at) >= ttl_seconds





class ConfigProvider:

    """Resolve service credentials from a remote KV-v2 store with local fallback.



    The provider owns its :class:`aiohttp.ClientSession` unless one is injected,

    and is safe for concurrent use: per-service locking collapses a thundering

    herd of callers into a single upstream fetch.

    """



    __slots__ = (

        "_backoff_seconds",

        "_cache",

        "_cache_dir",

        "_fallback_encoder",

        "_global_lock",

        "_locks",

        "_max_attempts",

        "_mount",

        "_owns_session",

        "_persist_cache",

        "_session",

        "_timeout",

        "_token",

        "_ttl_seconds",

        "_url",

        "_verify_ssl",

    )



    def __init__(

        self,

        base_url: str,

        token: str,

        *,

        mount: str = DEFAULT_MOUNT,

        fallback_encoder: "DataEncoder | FallbackEncoder | None" = None,

        cache_dir: str | Path = Path("var/cache/betdoc/config"),

        persist_cache: bool = True,

        ttl_seconds: float = DEFAULT_CREDENTIAL_TTL_SECONDS,

        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,

        max_attempts: int = DEFAULT_MAX_ATTEMPTS,

        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,

        session: aiohttp.ClientSession | None = None,

        verify_ssl: bool = True,

    ) -> None:

        if not base_url:

            raise ConfigError("base_url must not be empty", code="MALFORMED")

        if not token:

            raise ConfigError("token must not be empty", code="MALFORMED")

        if max_attempts < 1:

            raise ConfigError("max_attempts must be >= 1", code="MALFORMED")



        self._url: str = base_url.rstrip("/")

        self._token: str = token

        self._mount: str = mount.strip("/")

        self._fallback_encoder: "DataEncoder | FallbackEncoder | None" = fallback_encoder

        self._cache_dir: Path = Path(cache_dir)

        self._persist_cache: bool = persist_cache

        self._ttl_seconds: float = float(ttl_seconds)

        self._timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(

            total=float(timeout_seconds)

        )

        self._max_attempts: int = int(max_attempts)

        self._backoff_seconds: float = float(backoff_seconds)

        self._session: aiohttp.ClientSession | None = session

        self._owns_session: bool = session is None

        self._verify_ssl: bool = verify_ssl

        self._cache: dict[str, ServiceCredentials] = {}

        self._locks: dict[str, asyncio.Lock] = {}

        self._global_lock: asyncio.Lock = asyncio.Lock()



    async def __aenter__(self) -> "ConfigProvider":

        await self._ensure_session()

        return self



    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:

        await self.close()



    @property

    def mount(self) -> str:

        """The KV-v2 mount path this provider reads from."""

        return self._mount



    async def get_credentials(

        self, service: str, *, force_refresh: bool = False

    ) -> ServiceCredentials:

        """Return credentials for ``service``, preferring the in-memory cache.



        Resolution order is: live in-memory entry, remote KV-v2 read, then the

        encrypted local cache. A 403 from the remote store short-circuits the

        fallback because a rejected token is an operator error, not an outage.

        """

        if not service:

            raise ConfigError("service must not be empty", code="MALFORMED")



        if not force_refresh:

            cached = self._cache.get(service)

            if cached is not None and not cached.is_expired(self._ttl_seconds):

                return cached



        lock = await self._lock_for(service)

        async with lock:

            # Re-check under the lock: a concurrent caller may have refreshed.

            if not force_refresh:

                cached = self._cache.get(service)

                if cached is not None and not cached.is_expired(self._ttl_seconds):

                    return cached



            try:

                credentials = await self._fetch_remote(service)

            except ConfigError as error:

                if error.code == "UNAVAILABLE":

                    _LOG.warning(

                        "config server unavailable for %s, using encrypted local cache",

                        service,

                    )

                    credentials = self._load_fallback(service)

                else:

                    raise

            else:

                self._persist(service, credentials)



            self._cache[service] = credentials

            return credentials



    async def get_api_key(self, service: str) -> str:

        """Convenience accessor returning only the API key for ``service``."""

        credentials = await self.get_credentials(service)

        credentials.require("api_key")

        return credentials.api_key



    async def refresh(self, service: str | None = None) -> None:

        """Invalidate cached credentials and re-read them from the remote store.



        Passing ``None`` refreshes every service seen so far.

        """

        targets: list[str] = [service] if service else list(self._cache.keys())

        for target in targets:

            self._cache.pop(target, None)

        for target in targets:

            await self.get_credentials(target, force_refresh=True)



    def invalidate(self, service: str | None = None) -> None:

        """Drop in-memory credentials without contacting the remote store."""

        if service is None:

            self._cache.clear()

        else:

            self._cache.pop(service, None)



    async def close(self) -> None:

        """Release the HTTP session and purge in-memory secrets."""

        self._cache.clear()

        if self._owns_session and self._session is not None and not self._session.closed:

            await self._session.close()

        self._session = None



    async def _lock_for(self, service: str) -> asyncio.Lock:

        async with self._global_lock:

            lock = self._locks.get(service)

            if lock is None:

                lock = asyncio.Lock()

                self._locks[service] = lock

            return lock



    async def _ensure_session(self) -> aiohttp.ClientSession:

        if self._session is None or self._session.closed:

            connector = aiohttp.TCPConnector(ssl=None if self._verify_ssl else False)

            self._session = aiohttp.ClientSession(

                timeout=self._timeout, connector=connector

            )

            self._owns_session = True

        return self._session



    def _secret_url(self, service: str) -> str:

        return f"{self._url}/v1/{self._mount}/data/{service}"



    async def _fetch_remote(self, service: str) -> ServiceCredentials:

        """Read ``service`` from the KV-v2 endpoint with bounded retries.



        Raises

        ------

        ConfigError

            ``FORBIDDEN`` on 403, ``NOT_FOUND`` on 404, ``MALFORMED`` on an

            unparseable body, or ``UNAVAILABLE`` once retries are exhausted.

        """

        session = await self._ensure_session()

        headers: dict[str, str] = {

            "X-Vault-Token": self._token,

            "Accept": "application/json",

        }

        last_detail: str = "no attempt completed"



        for attempt in range(1, self._max_attempts + 1):

            try:

                async with session.get(

                    self._secret_url(service),

                    headers=headers,

                    timeout=self._timeout,

                ) as response:

                    if response.status == 403:

                        raise ConfigError(

                            "config server rejected the token (403)",

                            code="FORBIDDEN",

                            service=service,

                        )

                    if response.status == 404:

                        raise ConfigError(

                            "no configuration stored for this service (404)",

                            code="NOT_FOUND",

                            service=service,

                        )

                    if response.status in _RETRYABLE_STATUSES:

                        last_detail = f"HTTP {response.status}"

                        await self._sleep_backoff(attempt)

                        continue

                    if response.status >= 400:

                        raise ConfigError(

                            f"config server returned HTTP {response.status}",

                            code="REMOTE_ERROR",

                            service=service,

                        )



                    try:

                        body: Any = await response.json(content_type=None)

                    except (ValueError, aiohttp.ContentTypeError) as error:

                        raise ConfigError(

                            f"config server returned a non-JSON body: {error}",

                            code="MALFORMED",

                            service=service,

                        ) from error



                    return self._parse_kv_v2(service, body)



            except asyncio.TimeoutError:

                last_detail = "request timed out"

                await self._sleep_backoff(attempt)

            except aiohttp.ClientError as error:

                last_detail = f"transport failure: {error}"

                await self._sleep_backoff(attempt)



        raise ConfigError(

            f"config server unreachable after {self._max_attempts} attempt(s): {last_detail}",

            code="UNAVAILABLE",

            service=service,

        )



    async def _sleep_backoff(self, attempt: int) -> None:

        if attempt >= self._max_attempts or self._backoff_seconds <= 0:

            return

        await asyncio.sleep(self._backoff_seconds * (2 ** (attempt - 1)))



    @staticmethod

    def _parse_kv_v2(service: str, body: Any) -> ServiceCredentials:

        """Unwrap the ``{"data": {"data": {...}, "metadata": {...}}}`` envelope."""

        if not isinstance(body, Mapping):

            raise ConfigError(

                "unexpected response envelope from config server",

                code="MALFORMED",

                service=service,

            )

        outer = body.get("data")

        if not isinstance(outer, Mapping):

            raise ConfigError(

                "response envelope is missing the 'data' object",

                code="MALFORMED",

                service=service,

            )

        payload = outer.get("data", outer)

        if not isinstance(payload, Mapping):

            raise ConfigError(

                "response envelope is missing the 'data.data' object",

                code="MALFORMED",

                service=service,

            )

        metadata = outer.get("metadata")

        version: int = 0

        if isinstance(metadata, Mapping):

            try:

                version = int(metadata.get("version", 0))

            except (TypeError, ValueError):

                version = 0

        return ServiceCredentials.from_mapping(service, payload, version=version)



    def _cache_path(self, service: str) -> Path:

        safe = service.replace("/", "_").replace("..", "_")

        return self._cache_dir / f"{safe}.cfg.enc"



    def _persist(self, service: str, credentials: ServiceCredentials) -> None:

        """Mirror a successful remote read into the encrypted local cache."""

        if not self._persist_cache or self._fallback_encoder is None:

            return

        try:

            self._fallback_encoder.save_encoded(

                self._cache_path(service),

                {"version": credentials.version, "data": credentials.to_mapping()},

            )

        except Exception as error:  # noqa: BLE001 - caching must never break reads

            _LOG.warning("failed to refresh local config cache for %s: %s", service, error)



    def _load_fallback(self, service: str) -> ServiceCredentials:

        """Decode the encrypted local cache for ``service``.



        Raises

        ------

        ConfigError

            ``NO_FALLBACK`` when no encoder is configured, or ``DECODE_FAILED``

            when the cache is absent, corrupt, or undecryptable.

        """

        if self._fallback_encoder is None:

            raise ConfigError(

                "config server is unreachable and no fallback encoder is configured",

                code="NO_FALLBACK",

                service=service,

            )



        path = self._cache_path(service)

        try:

            decoded = self._fallback_encoder.load_encoded(path)

        except ConfigError:

            raise

        except Exception as error:  # noqa: BLE001 - normalised below

            raise ConfigError(

                f"unable to decode local config cache: {error}",

                code="DECODE_FAILED",

                service=service,

            ) from error



        payload = decoded.get("data", decoded)

        if not isinstance(payload, Mapping):

            raise ConfigError(

                "local config cache has an unexpected shape",

                code="DECODE_FAILED",

                service=service,

            )

        try:

            version = int(decoded.get("version", 0))

        except (TypeError, ValueError):

            version = 0

        return ServiceCredentials.from_mapping(

            service, payload, version=version, from_cache=True

        )


