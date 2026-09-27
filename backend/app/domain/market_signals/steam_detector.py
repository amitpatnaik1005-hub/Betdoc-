import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import utc_now
from app.models.market_signals import MarketTickModel
from app.schemas.market_signals import OddsTick, OddsType, SteamAlert

signals_log = logging.getLogger("betdoc.signals")

DEFAULT_FETCH_LIMIT = 100_000
_LINE_EPS = 1e-9

SteamKey = tuple[str, str, str]  # (match_id, selection_id, market_type)


# ---------- Repository ----------

class OddsTickRepository(Protocol):
    async def save_ticks(self, ticks: list[OddsTick]) -> int: ...

    async def fetch_ticks(
        self,
        since: datetime,
        until: datetime | None = None,
        *,
        match_ids: Sequence[str] | None = None,
        market_type: str | None = None,
        odds_type: OddsType | None = None,
        limit: int = DEFAULT_FETCH_LIMIT,
    ) -> list[OddsTick]: ...


class SQLOddsTickRepository(OddsTickRepository):
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def save_ticks(self, ticks: list[OddsTick]) -> int:
        """Adds rows to the session; the CALLER commits."""
        if not ticks:
            return 0
        now = utc_now()
        rows = [
            {
                "id": uuid.uuid4(),
                "bookmaker_id": t.bookmaker_id,
                "match_id": t.match_id,
                "selection_id": t.selection_id,
                "market_type": t.market_type,
                "odds_type": t.odds_type.value,
                "decimal_odds": Decimal(str(t.decimal_odds)),
                "line": None if t.line is None else Decimal(str(t.line)),
                "timestamp": t.timestamp,
                "is_sharp": t.is_sharp,
                "ingested_at": now,
            }
            for t in ticks
        ]
        await self.db.execute(insert(MarketTickModel), rows)
        return len(rows)

    async def fetch_ticks(
        self,
        since: datetime,
        until: datetime | None = None,
        *,
        match_ids: Sequence[str] | None = None,
        market_type: str | None = None,
        odds_type: OddsType | None = None,
        limit: int = DEFAULT_FETCH_LIMIT,
    ) -> list[OddsTick]:
        stmt = select(MarketTickModel).where(MarketTickModel.timestamp >= since)
        if until is not None:
            stmt = stmt.where(MarketTickModel.timestamp <= until)
        if match_ids:
            stmt = stmt.where(MarketTickModel.match_id.in_(list(match_ids)))
        if market_type is not None:
            stmt = stmt.where(MarketTickModel.market_type == market_type)
        if odds_type is not None:
            stmt = stmt.where(MarketTickModel.odds_type == odds_type.value)
        # Newest first so the limit keeps the most recent data, then restore chronological order.
        stmt = stmt.order_by(MarketTickModel.timestamp.desc()).limit(max(1, limit))
        rows = (await self.db.scalars(stmt)).all()
        return [
            OddsTick(
                bookmaker_id=r.bookmaker_id,
                match_id=r.match_id,
                selection_id=r.selection_id,
                market_type=r.market_type,
                odds_type=OddsType(r.odds_type),
                decimal_odds=float(r.decimal_odds),
                line=None if r.line is None else float(r.line),
                timestamp=r.timestamp,
                is_sharp=bool(r.is_sharp),
            )
            for r in reversed(rows)
        ]


# ---------- Engine ----------

def _line_shift(first: OddsTick, last: OddsTick) -> float:
    if first.line is None or last.line is None:
        return 0.0
    return abs(last.line - first.line)


def _same_line(first: OddsTick, last: OddsTick) -> bool:
    if first.line is None and last.line is None:
        return True
    if first.line is None or last.line is None:
        return False
    return abs(first.line - last.line) <= _LINE_EPS


class SteamDetectorEngine:
    @staticmethod
    def _validate(window_minutes: int, min_prob_delta: float, min_line_shift: float, min_time_delta_seconds: int) -> None:
        if window_minutes < 1:
            raise ValueError("window_minutes must be >= 1")
        if min_prob_delta <= 0:
            raise ValueError("min_prob_delta must be > 0")
        if min_line_shift < 0:
            raise ValueError("min_line_shift must be >= 0")
        if min_time_delta_seconds < 1:
            raise ValueError("min_time_delta_seconds must be >= 1")

    def detect(
        self,
        history: list[OddsTick],
        as_of: datetime,
        window_minutes: int,
        min_prob_delta: float,
        min_line_shift: float,
        min_time_delta_seconds: int,
        keys: set[SteamKey] | None = None,
    ) -> list[SteamAlert]:
        """Pure: evaluates BACK ticks inside [as_of - window, as_of]."""
        self._validate(window_minutes, min_prob_delta, min_line_shift, min_time_delta_seconds)
        since = as_of - timedelta(minutes=window_minutes)

        grouped: dict[SteamKey, dict[str, list[OddsTick]]] = defaultdict(lambda: defaultdict(list))
        for t in history:
            if t.odds_type != OddsType.BACK or t.timestamp < since or t.timestamp > as_of:
                continue
            key = (t.match_id, t.selection_id, t.market_type)
            if keys is not None and key not in keys:
                continue
            grouped[key][t.bookmaker_id].append(t)

        alerts: list[SteamAlert] = []
        for key in sorted(grouped):
            alert = self._evaluate(key, grouped[key], as_of, min_prob_delta, min_line_shift, min_time_delta_seconds)
            if alert is not None:
                alerts.append(alert)
        return sorted(alerts, key=lambda a: a.implied_prob_delta_pct, reverse=True)

    @staticmethod
    def _evaluate(
        key: SteamKey,
        by_book: dict[str, list[OddsTick]],
        as_of: datetime,
        min_prob_delta: float,
        min_line_shift: float,
        min_time_delta_seconds: int,
    ) -> SteamAlert | None:
        movers: list[tuple[str, OddsTick, OddsTick, float, float]] = []
        for book, series in by_book.items():
            if len(series) < 2:
                continue  # GHOST: single tick
            series.sort(key=lambda t: t.timestamp)
            first, last = series[0], series[-1]
            if (last.timestamp - first.timestamp).total_seconds() < min_time_delta_seconds:
                continue  # GHOST: instantaneous blip
            if first.decimal_odds <= 1.0 or last.decimal_odds <= 1.0:
                continue

            same_line = _same_line(first, last)
            # Odds on different lines are not comparable: report prob delta only on a stable line.
            prob_delta_pp = (
                (1.0 / last.decimal_odds - 1.0 / first.decimal_odds) * 100.0 if same_line else 0.0
            )
            shift = _line_shift(first, last)
            line_triggered = shift > _LINE_EPS and shift >= min_line_shift
            odds_triggered = same_line and prob_delta_pp >= min_prob_delta
            if line_triggered or odds_triggered:
                movers.append((book, first, last, prob_delta_pp, shift))

        if not movers:
            return None

        _book, first, last, delta, _shift = max(movers, key=lambda m: (m[4], m[3]))
        match_id, selection_id, market_type = key
        return SteamAlert(
            match_id=match_id,
            selection_id=selection_id,
            market_type=market_type,
            opening_odds=float(first.decimal_odds),
            current_odds=float(last.decimal_odds),
            opening_line=first.line,
            current_line=last.line,
            implied_prob_delta_pct=round(float(delta), 4),
            triggering_bookmakers=sorted({m[0] for m in movers}),
            detected_at=as_of,
        )

    async def process_tick_batch(
        self,
        ticks: list[OddsTick],
        repo: OddsTickRepository,
        window_minutes: int,
        min_prob_delta: float,
        min_line_shift: float,
        min_time_delta_seconds: int,
    ) -> list[SteamAlert]:
        back_ticks = [t for t in ticks if t.odds_type == OddsType.BACK]
        if not back_ticks:
            return []

        as_of = max(t.timestamp for t in back_ticks)
        since = as_of - timedelta(minutes=window_minutes)
        match_ids = sorted({t.match_id for t in back_ticks})
        history = await repo.fetch_ticks(since, as_of, match_ids=match_ids, odds_type=OddsType.BACK)

        # Stateless safety: include the batch even if the caller has not persisted it yet.
        seen = {(t.bookmaker_id, t.match_id, t.selection_id, t.market_type, t.line, t.timestamp) for t in history}
        merged = list(history)
        for t in back_ticks:
            ident = (t.bookmaker_id, t.match_id, t.selection_id, t.market_type, t.line, t.timestamp)
            if ident not in seen:
                merged.append(t)
                seen.add(ident)

        keys = {(t.match_id, t.selection_id, t.market_type) for t in back_ticks}
        return self.detect(
            merged, as_of, window_minutes, min_prob_delta, min_line_shift, min_time_delta_seconds, keys=keys
        )
