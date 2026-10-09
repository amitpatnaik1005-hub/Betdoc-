"""Open positions for the live portfolio, cached in Redis so the 5 Hz loop never queries PostgreSQL.

    <PORTFOLIO_CHANNEL_PREFIX>:positions:<user_id>   string  {"at": unix, "bets": [...]}

The cache is rebuilt from the ledger when it is missing (an execution or a settlement deletes it:
``mark_positions_dirty``) or older than ``PORTFOLIO_POSITIONS_REFRESH_SECONDS``, a safety net for
any writer that does not mark it. Between rebuilds every tick is Redis only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.cfo_vault import OPEN_STATUSES, LedgerStatus, PhantomLedger

logger = logging.getLogger("betdoc.portfolio")


def positions_key(settings: Settings, user_id: uuid.UUID | str) -> str:
    return f"{settings.PORTFOLIO_CHANNEL_PREFIX}:positions:{user_id}"


async def mark_positions_dirty(redis: Redis | None, settings: Settings, user_id: uuid.UUID) -> None:
    """The user's open positions changed: the next portfolio tick re-reads them from the ledger."""
    if redis is None:
        return
    with contextlib.suppress(RedisError, OSError):
        await redis.delete(positions_key(settings, user_id))


@dataclass(frozen=True, slots=True)
class OpenBet:
    id: str
    fixture_id: str
    market: str
    selection: str
    bookmaker_id: str
    stake_inr: Decimal
    odds: Decimal
    status: str
    unconfirmed: bool  # placement never confirmed, or dead-lettered: not hedged until a person resolves it
    strategy: str | None
    group_id: str | None
    requested_stake_inr: Decimal | None
    currency: str
    stake_ccy: Decimal | None
    commence_time: str | None
    created_at: str

    @property
    def market_key(self) -> str:
        return f"{self.fixture_id}|{self.market}"

    @classmethod
    def from_row(cls, row: PhantomLedger) -> OpenBet:
        return cls(
            id=str(row.id),
            fixture_id=row.fixture_id,
            market=row.market,
            selection=row.selection,
            bookmaker_id=row.bookmaker_id,
            stake_inr=Decimal(row.stake_inr),
            odds=Decimal(row.odds),
            status=str(row.status),
            unconfirmed=bool(row.reconcile_required) or row.status is LedgerStatus.REQUIRES_MANUAL_INTERVENTION,
            strategy=row.strategy,
            group_id=str(row.group_id) if row.group_id else None,
            requested_stake_inr=None if row.requested_stake_inr is None else Decimal(row.requested_stake_inr),
            currency=row.currency or "INR",
            stake_ccy=None if row.stake_ccy is None else Decimal(row.stake_ccy),
            commence_time=_iso(row.commence_time),
            created_at=_iso(row.created_at) or "",
        )

    def to_json(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OpenBet:
        def dec(key: str) -> Decimal | None:
            return None if data.get(key) is None else Decimal(str(data[key]))

        return cls(
            id=str(data["id"]),
            fixture_id=str(data["fixture_id"]),
            market=str(data["market"]),
            selection=str(data["selection"]),
            bookmaker_id=str(data["bookmaker_id"]),
            stake_inr=Decimal(str(data["stake_inr"])),
            odds=Decimal(str(data["odds"])),
            status=str(data["status"]),
            unconfirmed=bool(data["unconfirmed"]),
            strategy=data.get("strategy"),
            group_id=data.get("group_id"),
            requested_stake_inr=dec("requested_stake_inr"),
            currency=str(data.get("currency") or "INR"),
            stake_ccy=dec("stake_ccy"),
            commence_time=data.get("commence_time"),
            created_at=str(data.get("created_at", "")),
        )


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


async def load_open_bets(session: AsyncSession, user_id: uuid.UUID, *, fixture_id: str | None = None, market: str | None = None) -> list[OpenBet]:
    query = select(PhantomLedger).where(PhantomLedger.user_id == user_id, PhantomLedger.status.in_(OPEN_STATUSES))
    if fixture_id is not None:
        query = query.where(PhantomLedger.fixture_id == fixture_id)
    if market is not None:
        query = query.where(PhantomLedger.market == market)
    rows = (await session.execute(query.order_by(PhantomLedger.created_at, PhantomLedger.id))).scalars().all()
    return [OpenBet.from_row(row) for row in rows]


async def cached_open_bets(
    redis: Redis | None,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    user_id: uuid.UUID,
    *,
    now: float | None = None,
) -> list[OpenBet]:
    now = time.time() if now is None else now
    key = positions_key(settings, user_id)
    if redis is not None:
        try:
            raw = await redis.get(key)
        except (RedisError, OSError):
            raw = None
        if raw:
            try:
                data = json.loads(raw)
                if now - float(data["at"]) <= settings.PORTFOLIO_POSITIONS_REFRESH_SECONDS:
                    return [OpenBet.from_json(item) for item in data["bets"]]
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, ArithmeticError):
                pass  # unreadable: rebuild it
    async with session_factory() as session:
        bets = await load_open_bets(session, user_id)
    if redis is not None:
        payload = json.dumps({"at": now, "bets": [bet.to_json() for bet in bets]}, separators=(",", ":"))
        with contextlib.suppress(RedisError, OSError):
            await redis.set(key, payload, ex=int(settings.PORTFOLIO_POSITIONS_REFRESH_SECONDS * 4) + 1)
    return bets
