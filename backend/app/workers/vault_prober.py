"""The credential health prober (Group 70): are the Vault's keys and logins still good?

    vault.probe_credentials     every VAULT_PROBE_INTERVAL_MINUTES (6 h)
    vault.release_reservations  every 5 minutes: give back stake held by orders that settled or never landed

Only sanctioned APIs are called, each with the documented check that costs nothing:

* The Odds API key: ``GET /v4/sports`` (free; the response also refreshes the index of sports it offers,
  which keeps a mistyped sport in the user's file out of the polling);
* Betfair: the documented interactive login API (``identitysso /api/login`` with the app key), then
  ``/api/logout`` straight away. Accounts with 2FA are left to the user (the login would need a code);
* Pinnacle: ``GET /v1/client/balance`` (Basic auth), the API Pinnacle grants to approved accounts.

Parimatch, 1xBet, Stake and other books publish no account API: they are marked UNSUPPORTED (confirm by
hand) and nothing ever logs in to their websites. A credential that FAILED is not retried until it is
changed (or an admin forces a run), so a wrong password cannot lock the account by repetition.

Pacing: one run at a time cluster-wide; a random 3-7 s pause between two network checks so a run never
bursts; and at most one check per bookmaker or provider per minute (a run waits its turn rather than
skip). Nothing secret is logged: errors are recorded by type and status code, never with a URL.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.celery_app import celery_app
from app.core.config import Settings, get_settings
from app.core.omni_keys import OmniRedisKeys
from app.core.security_vault import VaultConfigurationError, VaultCrypto, VaultDecryptionError
from app.models.omni_vault import VaultBookmakerAccount, VaultProviderCredential, VerificationStatus
from app.services.vault import fleet_config
from app.services.vault.account_rotator import release_finished
from app.services.vault.markdown_importer import provider_context
from app.services.vault.registry import account_secrets

logger = logging.getLogger("betdoc.vault.prober")

MAX_PROBES_PER_RUN = 60
_random = secrets.SystemRandom()


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: VerificationStatus
    detail: str
    network: bool  # a request went out (the pacing applies)


def _prober_key(settings: Settings) -> str:
    return f"{settings.omni_redis_prefix}:vault:prober"


def _claim_key(settings: Settings, target: str) -> str:
    return f"{settings.omni_redis_prefix}:vault:probe:{target}"


# ------------------------------------------------------------------------------------------ checks
async def check_odds_api(http: httpx.AsyncClient, settings: Settings, redis: Redis | None, key: str, *, linked: bool) -> ProbeResult:
    try:
        response = await http.get(f"{settings.ODDS_API_BASE_URL.rstrip('/')}/sports", params={"apiKey": key, "all": "true"})
    except httpx.HTTPError as exc:
        return ProbeResult(VerificationStatus.UNVERIFIED, f"unreachable ({type(exc).__name__})", True)
    if response.status_code == 401:
        return ProbeResult(VerificationStatus.FAILED, "The Odds API refused the key (401)", True)
    if response.status_code != 200:
        return ProbeResult(VerificationStatus.UNVERIFIED, f"The Odds API answered HTTP {response.status_code}", True)
    remaining = response.headers.get("x-requests-remaining")
    if redis is not None:
        try:
            offered = {str(s["key"]) for s in response.json() if isinstance(s, dict) and s.get("key")}
        except ValueError:
            offered = set()
        await fleet_config.remember_offered_sports(redis, settings, offered)
        if linked and remaining is not None:
            used = response.headers.get("x-requests-used")
            mapping: dict[str, Any] = {"quota_remaining": remaining, "quota_checked_at": time.time()}
            try:
                if used is not None and float(remaining) + float(used) > 0:
                    mapping |= {"quota_used": used, "quota_limit": float(remaining) + float(used), "quota_fraction": round(float(remaining) / (float(remaining) + float(used)), 6)}
                await redis.hset(OmniRedisKeys(settings.omni_redis_prefix).fleet_metrics("odds_api"), mapping=mapping)
            except (ValueError, RedisError, OSError):
                pass
    return ProbeResult(VerificationStatus.OK, f"key accepted{f', {remaining} credits left' if remaining is not None else ''}", True)


async def check_betfair(http: httpx.AsyncClient, settings: Settings, creds: dict[str, str]) -> ProbeResult:
    if creds.get("totp_seed"):
        return ProbeResult(VerificationStatus.UNSUPPORTED, "2FA is on: Betfair's login would need a code, so confirm this account by hand", False)
    app_key, username, password = creds.get("api_key"), creds.get("username"), creds.get("password")
    if not (app_key and username and password):
        return ProbeResult(VerificationStatus.UNSUPPORTED, "needs the app key, username and password to check through Betfair's API", False)
    base = settings.BETFAIR_IDENTITY_URL.rstrip("/")
    headers = {"X-Application": app_key, "Accept": "application/json"}
    try:
        response = await http.post(f"{base}/login", data={"username": username, "password": password}, headers=headers)
    except httpx.HTTPError as exc:
        return ProbeResult(VerificationStatus.UNVERIFIED, f"Betfair unreachable ({type(exc).__name__})", True)
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 200 and body.get("status") == "SUCCESS" and body.get("token"):
        try:
            await http.post(f"{base}/logout", headers=headers | {"X-Authentication": str(body["token"])})
        except httpx.HTTPError:
            pass  # the session expires on its own
        return ProbeResult(VerificationStatus.OK, "Betfair login accepted (session closed again)", True)
    error = str(body.get("error") or f"HTTP {response.status_code}")[:64]
    transient = error.startswith(("TEMPORARY_BAN", "SERVICE_BUSY")) or response.status_code >= 500
    return ProbeResult(VerificationStatus.UNVERIFIED if transient else VerificationStatus.FAILED, f"Betfair: {error}", True)


async def check_pinnacle(http: httpx.AsyncClient, settings: Settings, creds: dict[str, str]) -> ProbeResult:
    username, password = creds.get("username"), creds.get("password")
    if not (username and password):
        return ProbeResult(VerificationStatus.UNSUPPORTED, "needs the username and password for Pinnacle's API", False)
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    try:
        response = await http.get(f"{settings.PINNACLE_API_BASE_URL.rstrip('/')}/v1/client/balance", headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
    except httpx.HTTPError as exc:
        return ProbeResult(VerificationStatus.UNVERIFIED, f"Pinnacle unreachable ({type(exc).__name__})", True)
    finally:
        token = ""
    if response.status_code == 200:
        return ProbeResult(VerificationStatus.OK, "Pinnacle API accepted the login", True)
    if response.status_code == 401:
        return ProbeResult(VerificationStatus.FAILED, "Pinnacle refused the login (401)", True)
    if response.status_code == 403:
        return ProbeResult(VerificationStatus.FAILED, "Pinnacle: API access is not enabled for this account (403)", True)
    return ProbeResult(VerificationStatus.UNVERIFIED, f"Pinnacle answered HTTP {response.status_code}", True)


def _no_api(book: str) -> ProbeResult:
    return ProbeResult(VerificationStatus.UNSUPPORTED, f"{book} publishes no account API: BetDoc never logs in to the site, so confirm it by hand", False)


# ------------------------------------------------------------------------------------------ the run
async def _wait_turn(redis: Redis | None, settings: Settings, target: str, sleep: Callable[[float], Awaitable[None]]) -> None:
    """At most one probe per target per VAULT_PROBE_MIN_INTERVAL_SECONDS, cluster-wide: wait for the slot."""
    if redis is None:
        return
    key = _claim_key(settings, target)
    for _ in range(10):
        try:
            if await redis.set(key, "1", nx=True, ex=settings.VAULT_PROBE_MIN_INTERVAL_SECONDS):
                return
            ttl = await redis.ttl(key)
        except (RedisError, OSError):
            return
        await sleep(max(1.0, float(ttl if ttl and ttl > 0 else 1)))


def _record(row: VaultBookmakerAccount | VaultProviderCredential, result: ProbeResult, now: datetime) -> None:
    row.verification_status = result.status.value
    row.verification_detail = result.detail[:255]
    if result.status is VerificationStatus.OK:
        row.last_verified_at = now


async def probe_all(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, vault: VaultCrypto, *, force: bool = False,
    http: httpx.AsyncClient | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    summary: dict[str, Any] = {"checked": 0, "ok": 0, "failed": 0, "unsupported": 0, "unverified": 0, "skipped": 0}
    lock = f"{_prober_key(settings)}:lock"
    if redis is not None:
        try:
            if not await redis.set(lock, "1", nx=True, ex=3600):
                return summary | {"skipped_run": "another probe run is in progress"}
        except (RedisError, OSError):
            pass
    own = http is None
    client = http or httpx.AsyncClient(timeout=15, follow_redirects=False)
    paced = False
    try:
        async with session_factory() as session:
            providers = list((await session.execute(select(VaultProviderCredential).where(VaultProviderCredential.is_active.is_(True)))).scalars())
            accounts = list((await session.execute(select(VaultBookmakerAccount).where(VaultBookmakerAccount.is_active.is_(True)).order_by(VaultBookmakerAccount.bookmaker_id, VaultBookmakerAccount.priority))).scalars())
            items: list[VaultProviderCredential | VaultBookmakerAccount] = [*providers, *accounts]
            for row in items[:MAX_PROBES_PER_RUN]:
                if row.verification_status == VerificationStatus.FAILED.value and not force:
                    summary["skipped"] += 1  # not retried until the credential changes
                    continue
                target = row.provider_id if isinstance(row, VaultProviderCredential) else row.bookmaker_id
                needs_network = (isinstance(row, VaultProviderCredential) and row.provider_id == "odds_api") or (isinstance(row, VaultBookmakerAccount) and row.bookmaker_id in ("betfair", "pinnacle"))
                if needs_network:
                    if paced:
                        await sleep(_random.uniform(*settings.VAULT_PROBE_SPACING_SECONDS))
                    await _wait_turn(redis, settings, target, sleep)
                try:
                    result = await _probe_one(client, settings, redis, vault, row)
                except VaultDecryptionError:
                    result = ProbeResult(VerificationStatus.FAILED, "the stored credential would not decrypt (MASTER_VAULT_KEY changed?)", False)
                paced = paced or result.network
                _record(row, result, now())
                await session.commit()
                summary["checked"] += 1
                summary[{"OK": "ok", "FAILED": "failed", "UNSUPPORTED": "unsupported"}.get(result.status.value, "unverified")] += 1
    finally:
        if own:
            await client.aclose()
        if redis is not None:
            try:
                await redis.hset(_prober_key(settings), mapping={"last_run_at": now().isoformat(), **{k: str(v) for k, v in summary.items()}})
                await redis.delete(lock)
            except (RedisError, OSError):
                pass
        if redis is not None:
            await fleet_config.refresh(redis, settings, session_factory, force=True)
    logger.info("Vault probe: %s", summary)
    return summary


async def _probe_one(client: httpx.AsyncClient, settings: Settings, redis: Redis | None, vault: VaultCrypto, row: VaultProviderCredential | VaultBookmakerAccount) -> ProbeResult:
    if isinstance(row, VaultProviderCredential):
        if row.provider_id != "odds_api":
            return ProbeResult(VerificationStatus.UNSUPPORTED, "no free documented check for this provider: confirm by hand", False)
        key = vault.decrypt_key(row.encrypted_api_key, context=provider_context(row.id, "api_key"))
        try:
            return await check_odds_api(client, settings, redis, key, linked=row.linked_source_id == "odds_api")
        finally:
            key = ""
    from app.services.vault import catalog  # noqa: PLC0415

    if row.bookmaker_id not in ("betfair", "pinnacle"):
        return _no_api(catalog.bookmaker_display(row.bookmaker_id))
    with account_secrets(vault, row) as creds:
        if row.bookmaker_id == "betfair":
            return await check_betfair(client, settings, creds)
        return await check_pinnacle(client, settings, creds)


# ------------------------------------------------------------------------------------------ Celery
@asynccontextmanager
async def _resources() -> AsyncIterator[tuple[Redis, async_sessionmaker[AsyncSession], Settings]]:
    settings = get_settings()
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        yield redis, async_sessionmaker(engine, expire_on_commit=False), settings
    finally:
        await redis.aclose()
        await engine.dispose()


async def _probe() -> dict[str, Any]:
    async with _resources() as (redis, sessions, settings):
        if not settings.VAULT_PROBE_ENABLED:
            return {"skipped_run": "VAULT_PROBE_ENABLED is off"}
        try:
            vault = VaultCrypto.from_settings(settings)
        except VaultConfigurationError:
            return {"skipped_run": "MASTER_VAULT_KEY is not configured"}
        return await probe_all(sessions, redis, settings, vault)


async def _release() -> dict[str, int]:
    async with _resources() as (_, sessions, settings):
        return await release_finished(sessions, timedelta(minutes=settings.VAULT_RESERVATION_TTL_MINUTES))


@celery_app.task(name="vault.probe_credentials", ignore_result=True)
def probe_credentials() -> dict[str, Any]:
    return asyncio.run(_probe())


@celery_app.task(name="vault.release_reservations", ignore_result=True)
def release_reservations() -> dict[str, int]:
    return asyncio.run(_release())


__all__ = ["ProbeResult", "check_betfair", "check_odds_api", "check_pinnacle", "probe_all"]
