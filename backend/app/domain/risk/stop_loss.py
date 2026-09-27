import logging
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.risk.common import SETTLED_STATUSES, bet_profit, safe_float
from app.domain.risk.drawdown import calculate_current_drawdown
from app.models import BetLedger, ExchangeAccount, utc_now
from app.models.risk import StopLossConfigModel, StopLossEventModel
from app.schemas.risk import StopLossConfig, StopLossStatus

kumbha = logging.getLogger("betdoc.kumbha")

TRIGGER_DAILY_LOSS = "DAILY_LOSS"
TRIGGER_CONSECUTIVE = "CONSECUTIVE_LOSSES"
TRIGGER_TRAILING = "TRAILING_STOP"
TRIGGER_SESSION_LOSS = "SESSION_LOSS"


@dataclass(frozen=True, slots=True)
class _SettledBet:
    status: str
    profit: float
    resolved_at: datetime


def utc_day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _net_loss(bets: list[_SettledBet]) -> float:
    """Net loss (wins offset losses), floored at 0."""
    return safe_float(max(0.0, -sum(b.profit for b in bets)))


def _loss_streak(bets_ascending: list[_SettledBet]) -> int:
    """Consecutive LOST from the most recent bet backwards. VOID is skipped, WON breaks."""
    streak = 0
    for bet in reversed(bets_ascending):
        if bet.status == "VOID":
            continue
        if bet.status == "LOST":
            streak += 1
            continue
        break
    return streak


def _already_recorded(events: list[StopLossEventModel], trigger_type: str, since: datetime) -> bool:
    """True if this trigger type was already logged after the latest settlement (no new info)."""
    return any(
        e.trigger_type == trigger_type and (_as_utc(e.triggered_at) or since) >= since
        for e in events
    )


def _idle_status(config: StopLossConfigModel | None) -> StopLossStatus:
    return StopLossStatus(
        is_triggered=False,
        trigger_reason=None,
        daily_loss_current=0.0,
        consecutive_losses_current=0,
        cooldown_until=None,
        session_loss_current=0.0,
        session_started_at=_as_utc(config.session_started_at) if config else None,
    )


class StopLossEngine:
    # ---------- Public API ----------

    async def check(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        bankroll: float | None = None,
    ) -> StopLossStatus:
        """
        Evaluates all 5 mechanisms. Newly fired triggers are added to the session and
        flushed; the CALLER owns the commit.
        """
        config = await self._load_config(db, user_id)
        if config is None or not config.enabled:
            return _idle_status(config)

        now = utc_now()
        today_start = utc_day_start(now)
        session_start = _as_utc(config.session_started_at) or today_start
        window_start = min(today_start, session_start)
        cooldown_delta = (
            timedelta(minutes=config.cooldown_minutes) if config.cooldown_minutes else None
        )

        bets = await self._load_settled_bets(db, user_id, window_start)
        daily_bets = [b for b in bets if b.resolved_at >= today_start]
        session_bets = [b for b in bets if b.resolved_at >= session_start]

        event_floor = window_start if cooldown_delta is None else min(window_start, now - cooldown_delta)
        events = await self._load_events(db, user_id, event_floor)

        daily_loss = _net_loss(daily_bets)
        session_loss = _net_loss(session_bets)
        display_streak = _loss_streak(session_bets)

        reasons: list[str] = []
        fired: list[tuple[str, float, float]] = []

        # 1. Daily loss (UTC day)
        if config.daily_loss_limit is not None and daily_loss >= config.daily_loss_limit:
            fired.append((TRIGGER_DAILY_LOSS, daily_loss, float(config.daily_loss_limit)))

        # 2. Consecutive losses
        if config.consecutive_loss_limit is not None:
            last_consec = next(
                (
                    e for e in events
                    if e.trigger_type == TRIGGER_CONSECUTIVE
                    and (_as_utc(e.triggered_at) or now) >= session_start
                ),
                None,
            )
            if last_consec is not None and cooldown_delta is None:
                # No cooldown configured: the lockout holds until an explicit session reset.
                reasons.append(f"{TRIGGER_CONSECUTIVE}: lockout active until session reset")
            else:
                floor = _as_utc(last_consec.triggered_at) if last_consec is not None else None
                streak_bets = [b for b in session_bets if floor is None or b.resolved_at > floor]
                streak = _loss_streak(streak_bets)
                if streak >= config.consecutive_loss_limit:
                    fired.append((TRIGGER_CONSECUTIVE, float(streak), float(config.consecutive_loss_limit)))

        # 3. Trailing stop (session equity curve)
        if config.trailing_stop_pct is not None and session_bets:
            base = bankroll if bankroll is not None and math.isfinite(bankroll) and bankroll > 0 else 0.0
            curve = [base]
            for bet in session_bets:
                curve.append(curve[-1] + bet.profit)
            drawdown_pct = safe_float(calculate_current_drawdown(curve) * 100.0)
            if drawdown_pct >= config.trailing_stop_pct:
                fired.append((TRIGGER_TRAILING, drawdown_pct, float(config.trailing_stop_pct)))

        # 4. Session loss
        if config.session_loss_limit is not None and session_loss >= config.session_loss_limit:
            fired.append((TRIGGER_SESSION_LOSS, session_loss, float(config.session_loss_limit)))

        # Persist new triggers (deduplicated per settlement)
        latest_resolved = bets[-1].resolved_at if bets else None
        new_events: list[StopLossEventModel] = []
        for trigger_type, value, threshold in fired:
            reasons.append(f"{trigger_type}: {value:.2f} >= {threshold:.2f}")
            if latest_resolved is not None and not _already_recorded(events, trigger_type, latest_resolved):
                new_events.append(
                    StopLossEventModel(
                        user_id=user_id,
                        trigger_type=trigger_type,
                        trigger_value=safe_float(value),
                        threshold=safe_float(threshold),
                        triggered_at=now,
                    )
                )
        if new_events:
            db.add_all(new_events)
            await db.flush()
            kumbha.warning(
                "Stop-loss fired for user=%s: %s",
                user_id, ", ".join(e.trigger_type for e in new_events),
            )

        # 5. Cooldown (any event, including ones just recorded)
        cooldown_until: datetime | None = None
        if cooldown_delta is not None:
            stamps = [s for s in (_as_utc(e.triggered_at) for e in events) if s is not None]
            if new_events:
                stamps.append(now)
            if stamps:
                candidate = max(stamps) + cooldown_delta
                if candidate > now:
                    cooldown_until = candidate
                    if not fired:
                        reasons.append(f"COOLDOWN: active until {candidate.isoformat()}")

        return StopLossStatus(
            is_triggered=bool(reasons),
            trigger_reason="; ".join(reasons) if reasons else None,
            daily_loss_current=daily_loss,
            consecutive_losses_current=display_streak,
            cooldown_until=cooldown_until,
            session_loss_current=session_loss,
            session_started_at=session_start,
        )

    async def get_config(self, db: AsyncSession, user_id: uuid.UUID) -> StopLossConfig:
        config = await self._load_config(db, user_id)
        if config is None:
            return StopLossConfig()
        return StopLossConfig.model_validate(config)

    async def update_config(
        self, db: AsyncSession, user_id: uuid.UUID, config: StopLossConfig
    ) -> StopLossConfigModel:
        """Atomic upsert. Never touches session_started_at on update."""
        now = utc_now()
        values = config.model_dump()
        stmt = (
            pg_insert(StopLossConfigModel)
            .values(id=uuid.uuid4(), user_id=user_id, session_started_at=now, updated_at=now, **values)
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={**values, "updated_at": now},
            )
            .returning(StopLossConfigModel)
        )
        result = await db.scalars(stmt, execution_options={"populate_existing": True})
        model = result.one()
        kumbha.info("Stop-loss config upserted for user=%s", user_id)
        return model

    async def reset_session(self, db: AsyncSession, user_id: uuid.UUID) -> StopLossConfigModel:
        """Starts a new session now. Creates a default config if none exists."""
        now = utc_now()
        defaults = StopLossConfig().model_dump()
        stmt = (
            pg_insert(StopLossConfigModel)
            .values(id=uuid.uuid4(), user_id=user_id, session_started_at=now, updated_at=now, **defaults)
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={"session_started_at": now, "updated_at": now},
            )
            .returning(StopLossConfigModel)
        )
        result = await db.scalars(stmt, execution_options={"populate_existing": True})
        model = result.one()
        kumbha.info("Session reset for user=%s at %s", user_id, now.isoformat())
        return model

    # ---------- Queries ----------

    @staticmethod
    async def _load_config(db: AsyncSession, user_id: uuid.UUID) -> StopLossConfigModel | None:
        return await db.scalar(
            select(StopLossConfigModel).where(StopLossConfigModel.user_id == user_id)
        )

    @staticmethod
    async def _load_settled_bets(
        db: AsyncSession, user_id: uuid.UUID, since: datetime
    ) -> list[_SettledBet]:
        stmt = (
            select(BetLedger.status, BetLedger.stake, BetLedger.payout, BetLedger.resolved_at)
            .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
            .where(
                ExchangeAccount.user_id == user_id,
                BetLedger.status.in_(SETTLED_STATUSES),
                BetLedger.resolved_at.is_not(None),
                BetLedger.resolved_at >= since,
            )
            .order_by(BetLedger.resolved_at.asc(), BetLedger.id.asc())
        )
        rows = (await db.execute(stmt)).all()
        return [
            _SettledBet(
                status=str(row.status).upper(),
                profit=bet_profit(row.status, row.stake, row.payout),
                resolved_at=_as_utc(row.resolved_at),  # type: ignore[arg-type]
            )
            for row in rows
        ]

    @staticmethod
    async def _load_events(
        db: AsyncSession, user_id: uuid.UUID, since: datetime
    ) -> list[StopLossEventModel]:
        stmt = (
            select(StopLossEventModel)
            .where(
                StopLossEventModel.user_id == user_id,
                StopLossEventModel.triggered_at >= since,
            )
            .order_by(StopLossEventModel.triggered_at.desc())
        )
        return list((await db.scalars(stmt)).all())
