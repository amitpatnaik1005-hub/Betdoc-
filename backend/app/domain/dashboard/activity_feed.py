from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, TypeVar
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.domain.dashboard._common import align_to_column, ensure_aware_utc, json_safe, to_float, utc_now
from app.domain.dashboard.summary_builder import SETTLED_STATUSES
from app.domain.market_signals.steam_detector import SteamDetectorEngine
from app.models import BetLedger, ExchangeAccount
from app.models.market_signals import MarketTickModel
from app.models.risk import StopLossEventModel
from app.schemas.dashboard import ActivityEvent
from app.schemas.market_signals import OddsTick

logger = logging.getLogger("betdoc.dashboard")

T = TypeVar("T")

STEAM_WINDOW_MINUTES = 15
STEAM_MIN_PROB_DELTA = 2.0
STEAM_MIN_LINE_SHIFT = 0.25
STEAM_MIN_TIME_DELTA_SECONDS = 60
STEAM_TICK_LIMIT = 2000
ODDS_TYPE_BACK = "BACK"


def _bet_timestamp_expr():
    """Prefer resolved_at, falling back to a placement column if the model defines one."""
    for name in ("placed_at", "created_at"):
        column = getattr(BetLedger, name, None)
        if column is not None:
            return func.coalesce(BetLedger.resolved_at, column)
    return BetLedger.resolved_at


async def _fetch_bet_events(session: AsyncSession, user_id: UUID, limit: int) -> list[ActivityEvent]:
    ts_expr = _bet_timestamp_expr()
    stmt = (
        select(BetLedger, ts_expr.label("event_ts"))
        .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
        .where(ExchangeAccount.user_id == user_id, ts_expr.is_not(None))
        .order_by(ts_expr.desc(), BetLedger.id)
        .limit(limit)
    )
    events: list[ActivityEvent] = []
    for bet, event_ts in (await session.execute(stmt)).all():
        stake = to_float(bet.stake)
        status = str(bet.status)
        pnl = None
        if status == "PENDING":
            event_type = "BET_PLACED"
            message = f"Bet placed | {bet.market_type} | stake {stake:.2f}"
        elif status in SETTLED_STATUSES:
            event_type = f"BET_{status}"
            pnl = to_float(bet.payout) - stake
            message = f"Bet {status} | {bet.market_type} | stake {stake:.2f} | P&L {pnl:+.2f}"
        else:
            # Open (ACCEPTED, PENDING_NETWORK, UNKNOWN) or never-filled: no P&L yet.
            event_type = f"BET_{status}"
            message = f"Bet {status} | {bet.market_type} | {bet.selection} @ {to_float(bet.odds):.2f} | stake {stake:.2f}"
        events.append(
            ActivityEvent(
                event_type=event_type,
                timestamp=ensure_aware_utc(event_ts),
                message=message,
                metadata=json_safe(
                    {
                        "bet_id": bet.id,
                        "exchange_account_id": bet.exchange_account_id,
                        "status": status,
                        "market_type": bet.market_type,
                        "stake": stake,
                        "payout": None if bet.payout is None else to_float(bet.payout),
                        "pnl": pnl,
                    }
                ),
            )
        )
    return events


async def _fetch_stop_loss_events(session: AsyncSession, user_id: UUID, limit: int) -> list[ActivityEvent]:
    stmt = (
        select(StopLossEventModel)
        .where(StopLossEventModel.user_id == user_id)
        .order_by(StopLossEventModel.triggered_at.desc(), StopLossEventModel.id)
        .limit(limit)
    )
    events: list[ActivityEvent] = []
    for ev in (await session.execute(stmt)).scalars().all():
        details = ev.details if isinstance(ev.details, dict) else {}
        message = str(details.get("message") or f"Stop-loss event: {ev.event_type}")
        events.append(
            ActivityEvent(
                event_type=f"STOP_LOSS_{str(ev.event_type).upper()}",
                timestamp=ensure_aware_utc(ev.triggered_at),
                message=message,
                metadata=json_safe({"stop_loss_event_id": ev.id, "details": details}),
            )
        )
    return events


def _to_odds_tick(tick: MarketTickModel) -> OddsTick | None:
    try:
        return OddsTick.model_validate(
            {
                "bookmaker_id": tick.bookmaker_id,
                "match_id": tick.match_id,
                "selection_id": tick.selection_id,
                "market_type": tick.market_type,
                "odds_type": tick.odds_type,
                "decimal_odds": tick.decimal_odds,
                "line": tick.line,
                "timestamp": tick.timestamp,
            }
        )
    except ValidationError:
        logger.warning("Skipping unmappable market tick %s/%s", tick.match_id, tick.selection_id)
        return None


def _alert_timestamp(alert: Any, fallback: datetime) -> datetime:
    for attr in ("detected_at", "timestamp", "triggered_at", "as_of"):
        value = getattr(alert, attr, None)
        if isinstance(value, datetime):
            return ensure_aware_utc(value)
    return ensure_aware_utc(fallback)


def _steam_to_event(alert: Any, fallback_ts: datetime) -> ActivityEvent:
    payload = alert.model_dump(mode="json") if hasattr(alert, "model_dump") else json_safe(vars(alert))
    match_id = getattr(alert, "match_id", "?")
    selection_id = getattr(alert, "selection_id", "?")
    market_type = getattr(alert, "market_type", "?")
    return ActivityEvent(
        event_type="STEAM_ALERT",
        timestamp=_alert_timestamp(alert, fallback_ts),
        message=f"Steam move | match {match_id} | {market_type} | selection {selection_id}",
        metadata=json_safe(payload),
    )


async def _fetch_steam_events(session: AsyncSession, limit: int) -> list[ActivityEvent]:
    """Global steam alerts. Best-effort: failures are logged and yield an empty list."""
    try:
        now = utc_now()
        as_of = align_to_column(now, MarketTickModel.timestamp)
        window_start = align_to_column(now - timedelta(minutes=STEAM_WINDOW_MINUTES), MarketTickModel.timestamp)
        stmt = (
            select(MarketTickModel)
            .where(MarketTickModel.odds_type == ODDS_TYPE_BACK, MarketTickModel.timestamp >= window_start)
            .order_by(MarketTickModel.timestamp.desc())
            .limit(STEAM_TICK_LIMIT)
        )
        rows = (await session.execute(stmt)).scalars().all()
        history = [t for t in (_to_odds_tick(r) for r in reversed(rows)) if t is not None]
        if not history:
            return []
        alerts = await asyncio.to_thread(
            SteamDetectorEngine().detect,
            history=history,
            as_of=as_of,
            window_minutes=STEAM_WINDOW_MINUTES,
            min_prob_delta=STEAM_MIN_PROB_DELTA,
            min_line_shift=STEAM_MIN_LINE_SHIFT,
            min_time_delta_seconds=STEAM_MIN_TIME_DELTA_SECONDS,
        )
        events = [_steam_to_event(a, now) for a in alerts]
        events.sort(key=lambda e: e.timestamp, reverse=True)
        return events[:limit]
    except Exception:  # noqa: BLE001 - market intel must never break the feed
        logger.exception("Steam detection failed; continuing without steam alerts")
        return []


async def fetch_activity_feed(user_id: UUID, db: AsyncSession, limit: int = 50) -> list[ActivityEvent]:
    if limit <= 0:
        return []

    bind = db.bind
    if isinstance(bind, AsyncEngine):
        # One AsyncSession cannot run concurrent queries: isolate each task.
        factory = async_sessionmaker(bind, expire_on_commit=False)

        async def isolated(fn: Callable[..., Awaitable[T]], *args: Any) -> T:
            async with factory() as session:
                return await fn(session, *args)

        bets, stop_losses, steam = await asyncio.gather(
            isolated(_fetch_bet_events, user_id, limit),
            isolated(_fetch_stop_loss_events, user_id, limit),
            isolated(_fetch_steam_events, limit),
        )
    else:
        # Connection-bound session (e.g. transactional tests): run sequentially.
        bets = await _fetch_bet_events(db, user_id, limit)
        stop_losses = await _fetch_stop_loss_events(db, user_id, limit)
        steam = await _fetch_steam_events(db, limit)

    events = [*bets, *stop_losses, *steam]
    events.sort(key=lambda e: ensure_aware_utc(e.timestamp), reverse=True)
    return events[:limit]
