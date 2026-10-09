"""The replay engine: historical ticks, in order, into the stream of signals the Hive would have seen.

``HistoricalStore.load`` reads the market history through a time-locked session whose clock is
frozen at the run's horizon, so a store built for an in-sample window holds no row from after it.

``ReplayEngine.build`` replays the store frame by frame (a frame: one book's ticks for one market at
one instant). After each frame the market's live books go through Aryabhata's own scorer
(``evaluate_market``, net of each venue's commission) exactly as the live consumer runs it; each
committed edge becomes a ``SimulatedSignal``: the ``EdgeSignal`` the Hive's stream would carry, with
the ``LiveMarket`` a bot's pipeline reads (the quoting books, the selection's consensus-probability
history, the reported liquidity in INR). The engine also records each market shock the flash-crash
breaker would have tripped on (a consensus swing above the threshold within the window).

The tape is a change log: a book's price persists until its next tick (or a suspension), so a
book that has not moved is still quoting at the frame's instant. Only pre-match markets are priced,
and a fixture whose postponement is already known is no longer quoted.

``MarketView`` is the only way the simulator reads the tape: every lookup is checked against the
simulation clock (``time_lock.SimulationClock``) and raises ``DataLeakageError`` for the future.
"""

from __future__ import annotations

import bisect
import hashlib
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.math.arbitrage_calc import to_inr
from app.models.lab_quant import LabFixture, LabFixtureResult, LabFxRate, LabOddsTick, ResultStatus
from app.schemas.aryabhata import EdgeSignal
from app.services.aryabhata_engine import BookLine, EmaState, MarketState, evaluate_market
from app.services.backtesting.fx_router import HistoricalFxRouter
from app.services.backtesting.time_lock import AS_OF, DataLeakageError, SimulationClock, install_time_lock
from app.services.fx_rates import FxUnavailableError
from app.services.hive_pipeline import LiveMarket, MarketReadings

LABELS: dict[str, tuple[str, ...]] = {"Match Odds": ("HOME", "DRAW", "AWAY"), "Over/Under 2.5": ("OVER", "UNDER")}
SOURCE = "lab"
WARMUP = timedelta(days=8)  # history a bot already holds when its window opens (a fixture opens 7 days out)


# ---------------------------------------------------------------- the tape
@dataclass(frozen=True, slots=True)
class FixtureInfo:
    id: str
    sport_key: str
    league: str
    home_team: str
    away_team: str
    commence_time: datetime
    listed_at: datetime


@dataclass(frozen=True, slots=True)
class ResultInfo:
    fixture_id: str
    status: str
    home_goals: int | None
    away_goals: int | None
    known_at: datetime

    @property
    def postponed(self) -> bool:
        return self.status == ResultStatus.POSTPONED

    def winners(self, market: str) -> frozenset[str]:
        """The winning selection(s) of a market; empty for a postponed fixture."""
        if self.home_goals is None or self.away_goals is None:
            return frozenset()
        h, a = self.home_goals, self.away_goals
        if market == "Match Odds":
            return frozenset({"HOME" if h > a else "AWAY" if a > h else "DRAW"})
        if market == "Over/Under 2.5":
            return frozenset({"OVER" if h + a > 2 else "UNDER"})
        return frozenset()


@dataclass(frozen=True, slots=True)
class TickRow:
    fixture_id: str
    market: str
    selection: str
    bookmaker_id: str
    odds: Decimal
    liquidity: Decimal | None
    currency: str
    suspended: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class Frame:
    at: datetime
    fixture_id: str
    market: str
    ticks: tuple[TickRow, ...]


class HistoricalStore:
    """Everything a run may know, read once up to ``horizon`` and indexed for point-in-time lookups."""

    def __init__(
        self,
        fixtures: Sequence[FixtureInfo],
        results: Sequence[ResultInfo],
        ticks: Sequence[TickRow],
        fx: Mapping[str, Sequence[tuple[datetime, Decimal]]],
        horizon: datetime,
        datasets: Sequence[str] = (),
    ) -> None:
        self.horizon = horizon
        self.fixtures = {f.id: f for f in fixtures}
        self.results = {r.fixture_id: r for r in results}
        self.results_timeline = sorted(results, key=lambda r: (r.known_at, r.fixture_id))
        self.ticks = sorted(ticks, key=lambda t: (t.at, t.fixture_id, t.market, t.bookmaker_id, t.selection))
        self.fx = {ccy: sorted(rows) for ccy, rows in fx.items()}
        self.datasets = tuple(sorted(set(datasets)))
        for row in (*self.ticks, *self.results_timeline):
            moment = row.at if isinstance(row, TickRow) else row.known_at
            if moment > horizon:
                raise DataLeakageError(f"a row stamped {moment.isoformat()} is past the store's horizon {horizon.isoformat()}", requested=moment, now=horizon)
        self.frames: list[Frame] = []
        for tick in self.ticks:
            last = self.frames[-1] if self.frames else None
            if last is not None and (last.at, last.fixture_id, last.market) == (tick.at, tick.fixture_id, tick.market):
                self.frames[-1] = Frame(last.at, last.fixture_id, last.market, (*last.ticks, tick))
            else:
                self.frames.append(Frame(tick.at, tick.fixture_id, tick.market, (tick,)))
        self._quotes: dict[tuple[str, str, str, str], tuple[list[datetime], list[TickRow]]] = {}
        for tick in self.ticks:
            times, rows = self._quotes.setdefault((tick.fixture_id, tick.market, tick.bookmaker_id, tick.selection), ([], []))
            times.append(tick.at)
            rows.append(tick)

    @classmethod
    async def load(cls, session: AsyncSession, *, horizon: datetime) -> HistoricalStore:
        """Read the history up to ``horizon`` through the time lock (a clock frozen at the horizon)."""
        clock = SimulationClock(horizon)
        remove = install_time_lock(session, clock)
        opts = {AS_OF: horizon}
        try:
            fixtures = (await session.execute(select(LabFixture).execution_options(**opts))).scalars().all()
            results = (await session.execute(select(LabFixtureResult).execution_options(**opts))).scalars().all()
            ticks = (
                await session.execute(
                    select(
                        LabOddsTick.fixture_id, LabOddsTick.market, LabOddsTick.selection, LabOddsTick.bookmaker_id, LabOddsTick.odds,
                        LabOddsTick.liquidity, LabOddsTick.currency, LabOddsTick.is_suspended, LabOddsTick.created_at,
                    ).order_by(LabOddsTick.created_at, LabOddsTick.id).execution_options(**opts)
                )
            ).all()
            fx_rows = (await session.execute(select(LabFxRate.currency, LabFxRate.inr_per_unit, LabFxRate.created_at).order_by(LabFxRate.created_at).execution_options(**opts))).all()
        finally:
            remove()
        fx: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
        for ccy, rate, at in fx_rows:
            fx[ccy.upper()].append((_aware(at), Decimal(rate)))
        return cls(
            [FixtureInfo(f.id, f.sport_key, f.league, f.home_team, f.away_team, _aware(f.commence_time), _aware(f.created_at)) for f in fixtures],
            [ResultInfo(r.fixture_id, r.status, r.home_goals, r.away_goals, _aware(r.created_at)) for r in results],
            [TickRow(t[0], t[1], t[2], t[3], Decimal(t[4]), None if t[5] is None else Decimal(t[5]), t[6].upper(), bool(t[7]), _aware(t[8])) for t in ticks],
            fx,
            horizon,
            [f.dataset for f in fixtures],
        )

    @classmethod
    def from_dataset(cls, dataset: Any, *, horizon: datetime) -> HistoricalStore:
        """A store straight from a generated dataset (``seed_historical_ticks.generate_dataset``),
        holding only what a store loaded at ``horizon`` would: rows stamped at or before it."""
        return cls(
            [FixtureInfo(f.id, f.sport_key, f.league, f.home_team, f.away_team, f.commence_time, f.listed_at) for f in dataset.fixtures if f.listed_at <= horizon],
            [ResultInfo(f.id, f.status, f.home_goals, f.away_goals, f.result_at) for f in dataset.fixtures if f.result_at is not None and f.result_at <= horizon],
            [TickRow(t.fixture_id, t.market, t.selection, t.bookmaker_id, t.odds, t.liquidity, t.currency, t.is_suspended, t.created_at) for t in dataset.ticks if t.created_at <= horizon],
            {ccy: [(x.created_at, x.inr_per_unit) for x in dataset.fx if x.currency == ccy and x.created_at <= horizon] for ccy in {x.currency for x in dataset.fx}},
            horizon,
            [dataset.name],
        )

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        return (self.ticks[0].at, self.ticks[-1].at) if self.ticks else None

    def fingerprint(self) -> str:
        span = self.span
        raw = f"{len(self.ticks)}|{len(self.results)}|{sum(len(v) for v in self.fx.values())}|{span}|{self.datasets}|{self.horizon.isoformat()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def series(self, fixture_id: str, market: str, bookmaker_id: str, selection: str) -> tuple[list[datetime], list[TickRow]]:
        """One quote's full history in the store (for after-the-fact metrics only: CLV, MAE)."""
        return self._quotes.get((fixture_id, market, bookmaker_id, selection), ([], []))


class MarketView:
    """The tape as of the clock: the only way the simulator sees a price or a result."""

    def __init__(self, store: HistoricalStore, clock: SimulationClock) -> None:
        self.store = store
        self.clock = clock

    def quote(self, fixture_id: str, market: str, bookmaker_id: str, selection: str, at: datetime | None = None) -> TickRow | None:
        moment = self.clock.now if at is None else at
        self.clock.check(moment, "a quote")
        times, rows = self.store.series(fixture_id, market, bookmaker_id, selection)
        i = bisect.bisect_right(times, moment) - 1
        if i < 0:
            return None
        self.clock.check_row(rows[i].at, "a quote")
        return rows[i]

    def result(self, fixture_id: str, at: datetime | None = None) -> ResultInfo | None:
        moment = self.clock.now if at is None else at
        self.clock.check(moment, "a result")
        result = self.store.results.get(fixture_id)
        return result if result is not None and result.known_at <= moment else None


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# ---------------------------------------------------------------- the signal stream
@dataclass(frozen=True, slots=True)
class SimulatedSignal:
    at: datetime
    edge: EdgeSignal
    readings: MarketReadings  # each model's estimate, computed once and shared by every run of the backtest
    liquidity: Decimal | None  # the edge book's money at the price, in its currency
    currency: str
    cell: str  # fixture|market|selection

    @property
    def market(self) -> LiveMarket:
        return self.readings.market


@dataclass(frozen=True, slots=True)
class MarketShock:
    at: datetime
    cell: str
    swing_pct: float
    low: float
    high: float
    points: int


@dataclass(slots=True)
class SignalStream:
    start: datetime
    end: datetime
    signals: list[SimulatedSignal] = field(default_factory=list)
    shocks: list[MarketShock] = field(default_factory=list)
    frames: int = 0
    evaluated: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    _by_cell: dict[str, list[SimulatedSignal]] = field(default_factory=lambda: defaultdict(list))

    def add(self, signal: SimulatedSignal) -> None:
        self.signals.append(signal)
        self._by_cell[signal.cell].append(signal)

    def live_edge(self, cell: str, at: datetime) -> SimulatedSignal | None:
        """The edge on ``cell`` Aryabhata would still be showing at ``at`` (published, not expired)."""
        candidates = self._by_cell.get(cell, [])
        i = bisect.bisect_right([s.at for s in candidates], at) - 1
        while i >= 0:
            signal = candidates[i]
            if signal.edge.expires_at > at:
                return signal
            if signal.at < at - timedelta(minutes=1):
                break
            i -= 1
        return None


class ReplayEngine:
    def __init__(self, store: HistoricalStore, settings: Settings, fx: Callable[[SimulationClock], HistoricalFxRouter], commissions: Mapping[str, Decimal]) -> None:
        self.store = store
        self.settings = settings
        self.fx = fx  # a router factory: volume conversions read FX on the replay's own clock
        self.commissions = dict(commissions)

    def build(self, start: datetime, end: datetime, *, progress: Callable[[float], None] | None = None) -> SignalStream:
        """Signals for frames in ``[start, end]``; earlier frames (up to ``WARMUP`` before) only build the
        books, the EMA states and the consensus history the first signals need."""
        stream = SignalStream(start, end)
        if not self.store.frames:
            return stream
        settings = self.settings
        clock = SimulationClock(min(self.store.frames[0].at, start), ceiling=self.store.horizon)
        view = MarketView(self.store, clock)
        router = self.fx(clock)
        line_age = timedelta(seconds=settings.ARYABHATA_LINE_MAX_AGE_SECONDS)
        book_age = timedelta(seconds=settings.ARYABHATA_BOOK_MAX_AGE_SECONDS)
        window = timedelta(seconds=settings.HIVE_FLASH_WINDOW_SECONDS)
        books: dict[tuple[str, str], dict[str, dict[str, TickRow]]] = defaultdict(lambda: defaultdict(dict))
        emas: dict[tuple[str, str], dict[str, EmaState]] = {}
        history: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        last_shock: dict[str, datetime] = {}
        total = len(self.store.frames)
        for n, frame in enumerate(self.store.frames):
            if frame.at > end:
                break
            if progress is not None and n % 250 == 0:
                progress(n / total)
            clock.advance(frame.at)
            market_key = (frame.fixture_id, frame.market)
            for tick in frame.ticks:
                clock.check_row(tick.at, "a tick")
                books[market_key][tick.bookmaker_id][tick.selection] = tick
            stream.frames += 1
            fixture = self.store.fixtures.get(frame.fixture_id)
            labels = LABELS.get(frame.market)
            if fixture is None or labels is None or frame.at < start - WARMUP:
                continue
            if fixture.commence_time <= frame.at:
                stream.skipped["in_play"] += 1
                continue
            if view.result(fixture.id) is not None:  # postponed, and already announced
                stream.skipped["postponed"] += 1
                continue
            lines = []
            for bookmaker_id, quotes in books[market_key].items():
                if all(label in quotes for label in labels):
                    lines.append(
                        BookLine(SOURCE, bookmaker_id, {label: quotes[label].odds for label in labels}, frame.at, is_suspended=any(quotes[label].suspended for label in labels))
                    )
            state = MarketState(fixture.id, frame.market, fixture.home_team, fixture.away_team, tuple(lines), fixture.sport_key, fixture.commence_time)
            evaluation = evaluate_market(
                state,
                now=frame.at,
                line_max_age=line_age,
                book_max_age=book_age,
                ema=emas.get(market_key),
                steam_period_seconds=settings.ARYABHATA_STEAM_PERIOD_SECONDS,
                steam_periods=settings.ARYABHATA_STEAM_PERIODS,
                commissions=self.commissions,
            )
            stream.evaluated += 1
            if evaluation.ema:
                emas[market_key] = dict(evaluation.ema)
            for label, probability in (evaluation.consensus or {}).items():
                cell = f"{fixture.id}|{frame.market}|{label}"
                points = history[cell]
                points.append((frame.at, float(probability)))
                shock = self._shock(cell, points, frame.at, window, last_shock)
                if shock is not None:
                    stream.shocks.append(shock)
            if frame.at < start:
                continue
            quoting = tuple(line for line in lines if not line.is_suspended)
            for edge in evaluation.edges:
                cell = f"{fixture.id}|{frame.market}|{edge.selection}"
                tail = tuple(p for _, p in history[cell][-settings.HIVE_HISTORY_POINTS :])
                live = LiveMarket(edge, evaluation.labels, quoting, tail, self._volume(books[market_key], quoting, edge.selection, router))
                quote = books[market_key][edge.bookmaker_id][edge.selection]
                stream.add(SimulatedSignal(frame.at, edge, MarketReadings(live), quote.liquidity, quote.currency, cell))
        if progress is not None:
            progress(1.0)
        return stream

    def _shock(self, cell: str, points: list[tuple[datetime, float]], now: datetime, window: timedelta, last: dict[str, datetime]) -> MarketShock | None:
        recent = [p for at, p in points if now - at <= window and 0.0 < p < 1.0]
        if len(recent) < self.settings.HIVE_FLASH_MIN_POINTS:
            return None
        low, high = min(recent), max(recent)
        swing = (high - low) / low * 100
        if swing <= self.settings.HIVE_FLASH_THRESHOLD_PCT:
            return None
        previous = last.get(cell)
        if previous is not None and now - previous <= window:
            return None  # the same shock, still inside its window
        last[cell] = now
        return MarketShock(now, cell, round(swing, 2), low, high, len(recent))

    @staticmethod
    def _volume(books: Mapping[str, Mapping[str, TickRow]], quoting: Sequence[BookLine], selection: str, router: HistoricalFxRouter) -> Decimal | None:
        """The money reported at the price across the quoting books, in INR (None: no book reports it)."""
        total, reported = Decimal(0), False
        for line in quoting:
            tick = books[line.bookmaker_id].get(selection)
            if tick is None or tick.liquidity is None:
                continue
            try:
                quote = router.resolve(tick.currency, record=False)
            except FxUnavailableError:
                continue
            total += to_inr(tick.liquidity, quote.quote)
            reported = True
        return total.quantize(Decimal("0.01")) if reported else None
