"""The runtime fleet configuration: database row -> Redis mirror -> every process's overlay (Group 70).

The ``vault_fleet_config`` row is the truth. ``publish`` derives the overlay from it (plus each
bookmaker's account currency: the primary active account's), installs it in this process and writes it
to Redis under a version number; ``refresh`` (cheap: one GET at most every few seconds) installs a newer
version in any other process. With Redis down a process keeps the overlay it has; with Redis flushed the
next ``refresh`` that has a session factory reloads it from the database.

Sports the Odds API does not list are kept out of the overlay once its free ``/sports`` index has been
read (``remember_offered_sports``): a typo in the user's file costs nothing and never trips the fleet's
breaker. Until the index is known every activated sport is tried.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import time as dtime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import fleet_overlay
from app.core.config import Settings
from app.core.fleet_overlay import FleetOverlay
from app.models.omni_vault import VaultBookmakerAccount, VaultFleetConfig

logger = logging.getLogger("betdoc.vault.fleet")

REFRESH_EVERY_SECONDS = 5.0
OFFERED_TTL_SECONDS = 24 * 3600
_last_check = 0.0


def overlay_key(settings: Settings) -> str:
    return f"{settings.omni_redis_prefix}:vault:fleet"


def offered_key(settings: Settings) -> str:
    return f"{settings.omni_redis_prefix}:vault:odds_sports"


def _clock(value: str | None) -> dtime | None:
    if not value:
        return None
    try:
        hours, minutes = value.split(":")
        return dtime(int(hours), int(minutes))
    except ValueError:
        return None


async def get_config(session: AsyncSession) -> VaultFleetConfig:
    row = await session.get(VaultFleetConfig, 1)
    if row is None:
        row = VaultFleetConfig(id=1, sports=[], markets_by_sport={}, timezone="Asia/Kolkata", account_routing=False, version=1)
        session.add(row)
        await session.flush()
    return row


async def bump(session: AsyncSession, actor_id: uuid.UUID | None) -> VaultFleetConfig:
    """Mark the configuration changed (the caller commits, then ``publish``es)."""
    row = await get_config(session)
    row.version = (row.version or 0) + 1
    row.updated_by = actor_id
    return row


async def account_currencies(session: AsyncSession) -> dict[str, str]:
    """Each bookmaker's currency: its primary active account's (priority, then age)."""
    rows = (await session.execute(
        select(VaultBookmakerAccount.bookmaker_id, VaultBookmakerAccount.currency)
        .where(VaultBookmakerAccount.is_active.is_(True))
        .order_by(VaultBookmakerAccount.bookmaker_id, VaultBookmakerAccount.priority, VaultBookmakerAccount.created_at)
    )).all()
    out: dict[str, str] = {}
    for book, currency in rows:
        out.setdefault(book, currency.upper())
    return out


def _payload(row: VaultFleetConfig, currencies: dict[str, str]) -> dict[str, Any]:
    return {
        "version": int(row.version or 0), "sports": list(row.sports or []), "markets_by_sport": dict(row.markets_by_sport or {}),
        "currencies": currencies, "quiet_start": row.quiet_start, "quiet_end": row.quiet_end, "timezone": row.timezone or "Asia/Kolkata",
        "account_routing": bool(row.account_routing),
    }


def _overlay(payload: dict[str, Any], offered: set[str] | None) -> FleetOverlay:
    sports = tuple(s for s in payload.get("sports") or [] if offered is None or s in offered)
    return FleetOverlay(
        version=int(payload.get("version") or 0), sports=sports, markets_by_sport=dict(payload.get("markets_by_sport") or {}),
        currencies={str(k): str(v).upper() for k, v in (payload.get("currencies") or {}).items()},
        quiet_start=_clock(payload.get("quiet_start")), quiet_end=_clock(payload.get("quiet_end")),
        timezone=str(payload.get("timezone") or "Asia/Kolkata"), account_routing=bool(payload.get("account_routing")),
    )


async def _offered(redis: Redis | None, settings: Settings) -> set[str] | None:
    if redis is None:
        return None
    try:
        members = await redis.smembers(offered_key(settings))
    except (RedisError, OSError):
        return None
    return {m.decode() if isinstance(m, bytes) else str(m) for m in members} or None


async def remember_offered_sports(redis: Redis, settings: Settings, keys: set[str]) -> None:
    """The Odds API's free ``/sports`` index (every key it offers), for a day."""
    if not keys:
        return
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.delete(offered_key(settings))
            pipe.sadd(offered_key(settings), *sorted(keys))
            pipe.expire(offered_key(settings), OFFERED_TTL_SECONDS)
            await pipe.execute()
    except (RedisError, OSError):
        logger.warning("Vault: the Odds API sport index was not cached")


async def publish(session: AsyncSession, redis: Redis | None, settings: Settings) -> FleetOverlay:
    """Install the configuration here and mirror it for every other process."""
    global _last_check
    row = await get_config(session)
    payload = _payload(row, await account_currencies(session))
    overlay = _overlay(payload, await _offered(redis, settings))
    fleet_overlay.install(overlay)
    _last_check = time.monotonic()
    if redis is not None:
        try:
            await redis.set(overlay_key(settings), json.dumps(payload, separators=(",", ":")))
        except (RedisError, OSError):
            logger.warning("Vault: fleet configuration v%s not mirrored to Redis; other processes keep theirs", overlay.version)
    return overlay


async def refresh(redis: Redis | None, settings: Settings, session_factory: async_sessionmaker[AsyncSession] | None = None, *, force: bool = False) -> FleetOverlay:
    """Install a newer published configuration, at most every ``REFRESH_EVERY_SECONDS``. Never raises."""
    global _last_check
    now = time.monotonic()
    if not force and now - _last_check < REFRESH_EVERY_SECONDS:
        return fleet_overlay.current()
    _last_check = now
    raw: str | bytes | None = None
    if redis is not None:
        try:
            raw = await redis.get(overlay_key(settings))
        except (RedisError, OSError):
            return fleet_overlay.current()
    if raw is None:
        if session_factory is None:
            return fleet_overlay.current()
        try:
            async with session_factory() as session:
                if await session.get(VaultFleetConfig, 1) is None and not await account_currencies(session):
                    return fleet_overlay.current()  # nothing configured yet
                return await publish(session, redis, settings)
        except Exception:  # noqa: BLE001 - a worker without its DB keeps the overlay it has
            logger.warning("Vault: fleet configuration could not be loaded", exc_info=True)
            return fleet_overlay.current()
    try:
        payload = json.loads(raw)
    except ValueError:
        return fleet_overlay.current()
    if int(payload.get("version") or 0) == fleet_overlay.current().version and not force:
        return fleet_overlay.current()
    overlay = _overlay(payload, await _offered(redis, settings))
    fleet_overlay.install(overlay)
    return overlay
