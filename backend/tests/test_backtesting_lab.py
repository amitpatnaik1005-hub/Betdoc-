"""Group 66: The Lab, quantitative backtesting.

The brief's two proofs come first: the exchange commission fix reduces the simulated ROI of a
winning Betfair trade by exactly the commission's share of the win, and a simulated 2,500 ms network
delay aborts a bet whose line fell below the minimum EV during the delay. Then the commission patch
in the live edge scorer, the FX fallback router, the anti-leakage locks, the historical seeder, the
replay engine, every reality penalty (latency, rate limit, quadratic impact, slippage, voids), the
Monte Carlo bootstrapper, the Kelly sweep and the walk-forward lock, the metrics, and the runner and
API end to end (SQLite).
"""

from __future__ import annotations

import asyncio
import math
import statistics
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import CheckConstraint, MetaData, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.db.seed_historical_ticks import BOOKS, generate_dataset, outcome_probabilities, round_odds, seed_dataset
from app.models import User
from app.models.hive_bots import TradingBot
from app.models.lab_quant import BacktestStatus, LabBacktestRun, LabFixture, LabFixtureResult, LabFxRate, LabOddsTick
from app.models.the_core import SmallcaseRegistryModel
from app.schemas.aryabhata import EdgeSignal
from app.schemas.lab_quant import BacktestParams, StrategySpec
from app.services.aryabhata_engine import BookLine, MarketState, break_even_odds, evaluate_market, expected_value, kelly_fraction, net_odds
from app.services.backtesting.fx_router import HistoricalFxRouter
from app.services.backtesting.metrics import compute_metrics, daily_equity, drawdown, ratios
from app.services.backtesting.monte_carlo import risk_of_ruin
from app.services.backtesting.optimizer import LockedParameters, best_row, kelly_grid, overfit_verdict
from app.services.backtesting.reality import (
    OrderAtVenue,
    OutboundRateLimiter,
    RealityConfig,
    VenueQuote,
    fill_at_arrival,
    impact_multiplier,
    max_participation,
)
from app.services.backtesting.replay_engine import FixtureInfo, HistoricalStore, MarketView, ReplayEngine, ResultInfo, SignalStream, SimulatedSignal, TickRow
from app.services.backtesting.runner import BacktestError, Window, execute, fx_factory, prepare_bots
from app.services.backtesting.simulator import SimBot, Simulator
from app.services.backtesting.time_lock import AS_OF, DataLeakageError, SimulationClock, install_time_lock
from app.services.cfo_execution import slippage_floor
from app.services.cfo_ledger import potential_profit
from app.services.fx_rates import FxUnavailableError
from app.services.hive_pipeline import LiveMarket, MarketReadings, Proposal, RiskContext, evaluate
from app.services.hive_registry import seed_registry

D = Decimal
BASE = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
KICKOFF = BASE + timedelta(hours=3)
FIX = "fx-ars-lee"
PIPE = ("math.consensus", "math.kelly_criterion")
TARGETS = ("bet.match_winner_1x2", "bet.single")


def T(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"LAB_BACKTEST_EXECUTOR": "inline"})


# ================================================================ a hand-built tape
def store_with(
    quotes: list[tuple[float, str, str, str]],
    *,
    currency: str = "INR",
    status: str = "FINISHED",
    score: tuple[int | None, int | None] = (2, 1),
    result_at: datetime | None = None,
    fx: dict[str, list[tuple[datetime, Decimal]]] | None = None,
    liquidity: str | None = "1000000",
) -> HistoricalStore:
    """``(seconds after BASE, bookmaker, selection, odds)`` quotes on one Arsenal v Leeds 1X2 market."""
    result_at = result_at or KICKOFF + timedelta(hours=2)
    ticks = [TickRow(FIX, "Match Odds", sel, book, D(odds), D(liquidity) if liquidity else None, currency, False, T(s)) for s, book, sel, odds in quotes]
    fixture = FixtureInfo(FIX, "soccer_epl", "Premier League", "Arsenal", "Leeds", KICKOFF, BASE - timedelta(days=7))
    goals = (None, None) if status == "POSTPONED" else score
    return HistoricalStore([fixture], [ResultInfo(FIX, status, goals[0], goals[1], result_at)], ticks, fx or {}, result_at + timedelta(hours=1), ["test"])


def signal_at(
    seconds: float, odds: str, *, p: str = "0.48", book: str = "betfair_ex_uk", commission: str = "0.05", currency: str = "INR", selection: str = "HOME"
) -> SimulatedSignal:
    at = T(seconds)
    ev = expected_value(D(p), D(odds), D(commission))
    edge = EdgeSignal(
        signal_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{seconds}|{odds}|{book}|{selection}"), fixture_id=FIX, market_id=uuid.uuid4(), market_type="Match Odds", selection=selection,
        home_team="Arsenal", away_team="Leeds", sport_key="soccer_epl", commence_time=KICKOFF, bookmaker_id=book, source="lab", odds=D(odds), commission=D(commission),
        true_prob=D(p), ev=ev, ev_percent=(ev * 100).quantize(D("0.0001")), full_kelly=kelly_fraction(D(p), D(odds), D(commission)), devig_method="shin",
        overround=D("0.02"), books=3, timestamp=at, expires_at=at + timedelta(seconds=15),
    )
    books = tuple(BookLine("lab", b, {"HOME": D("2.0"), "DRAW": D("3.6"), "AWAY": D("4.0")}, at) for b in ("pinnacle", "williamhill", "unibet_eu"))
    readings = MarketReadings(LiveMarket(edge, ("HOME", "DRAW", "AWAY"), books, ()))
    readings.cache["math.consensus"] = D(p)  # the model's view, fixed: these tests are about what happens after the decision
    return SimulatedSignal(at, edge, readings, D("1000000"), currency, f"{FIX}|Match Odds|{selection}")


def stream_of(*signals: SimulatedSignal) -> SignalStream:
    stream = SignalStream(BASE, KICKOFF)
    for s in signals:
        stream.add(s)
    return stream


def sim_bot(name: str = "Exchange sniper", **fields: Any) -> SimBot:
    base: dict[str, Any] = {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, name), "name": name, "math_models": PIPE, "risk_models": (), "target_bet_types": TARGETS, "kelly_multiplier": D("0.25"),
        "max_stake_pct": D("5"), "min_edge_pct": D("1"), "min_quoting_books": 2, "min_market_liquidity": D("0"), "enable_order_slicing": False, "slice_size_inr": D("10000"),
        "max_bets_per_minute": 100, "drawdown_limit_pct": D("50"), "cooldown_seconds": 0, "risk_params": {}, "capital": D("100000.00"),
    }
    return SimBot(**{**base, **fields})


def simulate(store: HistoricalStore, stream: SignalStream, settings: Settings, *bots: SimBot, **reality: Any) -> Any:
    config = RealityConfig(**{"slippage_pct": D(0), "void_rate": 0.0, "latency_ms": (2000, 2000), **reality})
    return Simulator(store, stream, bots or (sim_bot(),), config, settings, fx_factory(store, settings), start=BASE, end=KICKOFF, resume_after=None).run()


# ================================================================ the brief's two proofs
def test_exchange_commission_cuts_the_simulated_roi_of_a_winning_betfair_trade(settings: Settings) -> None:
    """Arsenal win at Betfair 2.50. Gross, the trade returns 150% of its stake; Betfair keeps 5% of
    the net win, so the backtest books 142.5%: ROI_net = (odds - 1) * (1 - 0.05), whatever the stake."""
    quotes = [(0, "betfair_ex_uk", "HOME", "2.50"), (0, "betfair_ex_uk", "DRAW", "3.40"), (0, "betfair_ex_uk", "AWAY", "3.30")]
    net_run = simulate(store_with(quotes), stream_of(signal_at(0, "2.50", commission="0.05")), settings)
    gross_run = simulate(store_with(quotes), stream_of(signal_at(0, "2.50", commission="0")), settings)

    (net_trade,) = net_run.settled
    (gross_trade,) = gross_run.settled
    assert net_trade.status == gross_trade.status == "WON" and net_trade.fill_odds == gross_trade.fill_odds == D("2.50")
    stake = net_trade.cost_inr
    assert net_trade.pnl_inr == (stake * D("1.5") * D("0.95")).quantize(D("0.01"), rounding="ROUND_DOWN")
    assert net_trade.commission_inr == (stake * D("1.5") * D("0.05")).quantize(D("0.01"))
    tape = store_with(quotes)
    net, gross = compute_metrics(net_run, tape), compute_metrics(gross_run, tape)
    assert gross["roi_pct"] == pytest.approx(150.0, abs=0.01)
    assert net["roi_pct"] == pytest.approx(142.5, abs=0.01)  # 150% * (1 - 0.05)
    assert net["roi_pct"] < gross["roi_pct"] and net["commission_paid_inr"] > 0 == gross["commission_paid_inr"]
    # And the decision itself was taken net: Kelly on the net price stakes less than on the raw one
    assert net_trade.cost_inr < gross_trade.cost_inr
    assert net_trade.ev_decision == expected_value(D("0.48"), D("2.50"), D("0.05"))  # 0.48 * 2.425 - 1 = 0.164


def test_a_2500ms_delay_aborts_a_bet_whose_line_fell_below_the_minimum_ev(settings: Settings) -> None:
    """The bot decides at 2.50 (net EV +16.4%). 2.0s later Betfair's price is 2.10: at p = 0.48 that
    is a net EV of -1.8%, under the bot's +1% minimum. The order lands at 2.5s and is refused; its
    stake goes back. Had the line moved at 3.0s, after the order landed, it would have filled at 2.50."""
    signal = signal_at(0, "2.50")
    assert break_even_odds(D("0.48"), D("0.05"), D("0.01")) > D("2.10")  # 2.10 cannot clear +1% net of 5%
    moved_inside = store_with([(0, "betfair_ex_uk", "HOME", "2.50"), (2.0, "betfair_ex_uk", "HOME", "2.10")])
    run = simulate(moved_inside, stream_of(signal), settings, latency_ms=(2500, 2500))
    assert run.positions == [] and run.rejections == {"LATENCY_EDGE_DECAY": 1}
    assert run.curve[-1][1] == D("100000.00")  # not a paisa moved

    moved_after = store_with([(0, "betfair_ex_uk", "HOME", "2.50"), (3.0, "betfair_ex_uk", "HOME", "2.10")])
    (filled,) = simulate(moved_after, stream_of(signal), settings, latency_ms=(2500, 2500)).positions
    assert filled.fill_odds == D("2.50") and filled.latency_ms == 2500 and filled.filled_at == T(2.5)


def test_a_line_that_moves_but_keeps_its_edge_fills_at_the_worse_price(settings: Settings) -> None:
    """2.50 -> 2.40 inside the delay: still +11.8% net, inside a 10% tolerance: filled at 2.40, never
    at the better price the bot saw. Inside the default 0.5% tolerance the venue refuses it instead."""
    store = store_with([(0, "betfair_ex_uk", "HOME", "2.50"), (1.0, "betfair_ex_uk", "HOME", "2.40")])
    (pos,) = simulate(store, stream_of(signal_at(0, "2.50")), settings, max_slippage_pct=D("10")).positions
    assert (pos.requested_odds, pos.arrival_odds, pos.fill_odds, pos.fill_reason) == (D("2.50"), D("2.40"), D("2.40"), "LATENCY_SLIPPAGE")
    strict = simulate(store, stream_of(signal_at(0, "2.50")), settings)
    assert strict.positions == [] and strict.rejections == {"SLIPPAGE_REJECTED": 1}
    better = store_with([(0, "betfair_ex_uk", "HOME", "2.50"), (1.0, "betfair_ex_uk", "HOME", "2.70")])
    (up,) = simulate(better, stream_of(signal_at(0, "2.50")), settings).positions
    assert up.fill_odds == D("2.50")  # a price that improved is not credited


# ================================================================ the commission gap, live
def test_the_live_edge_scorer_judges_every_line_net_of_commission() -> None:
    """Betfair's 2.10 at 5% returns 2.045: the bookmaker's 2.06 is the better line, and EV and Kelly
    are both net of the cut."""
    now = BASE
    prices = {"pinnacle": ("2.06", "3.60", "4.10"), "betfair_ex_uk": ("2.10", "3.65", "4.20"), "williamhill": ("1.85", "3.50", "4.30"), "unibet_eu": ("1.86", "3.45", "4.40")}
    books = tuple(BookLine("lab", b, dict(zip(("HOME", "DRAW", "AWAY"), map(D, p), strict=True)), now) for b, p in prices.items())
    state = MarketState(FIX, "Match Odds", "Arsenal", "Leeds", books, "soccer_epl", now + timedelta(hours=3))
    kwargs = {"now": now, "line_max_age": timedelta(seconds=90), "book_max_age": timedelta(seconds=300)}
    raw = {e.selection: e for e in evaluate_market(state, **kwargs).edges}
    net = {e.selection: e for e in evaluate_market(state, **kwargs, commissions={"betfair_ex_uk": D("0.05")}).edges}
    assert raw["HOME"].bookmaker_id == "betfair_ex_uk" and raw["HOME"].commission == 0
    assert net["HOME"].bookmaker_id == "pinnacle" and net["HOME"].odds == D("2.06")
    assert net_odds(D("2.10"), D("0.05")) == D("2.045") < D("2.06")
    assert abs(net["HOME"].ev - expected_value(net["HOME"].true_prob, D("2.06"))) < D("1e-9")  # pinnacle charges nothing
    assert raw["HOME"].ev > expected_value(raw["HOME"].true_prob, D("2.10"), D("0.05"))  # the raw scorer overstated Betfair's edge


def test_commission_reaches_kelly_the_hive_pipeline_the_floor_and_the_ledger() -> None:
    p = D("0.5")
    assert kelly_fraction(p, D("2.50"), D("0.05")) < kelly_fraction(p, D("2.50"))
    assert abs(kelly_fraction(p, D("2.50"), D("0.05")) - (p * D("2.425") - 1) / D("1.425")) < D("1e-25")
    # The slippage floor: never under the lowest price that still clears +0.5% EV, net of commission
    assert slippage_floor(D("2.50"), p, D("20"), D("0.05")) == D("2.0632")  # 1 + (1.005 / 0.5 - 1) / 0.95, rounded up
    assert slippage_floor(D("2.50"), p, D("20")) == D("2.0100")  # 1.005 / 0.5 without commission
    # The ledger books a win net of the cut
    assert potential_profit(D("100.00"), D("2.50"), D("0.05")) == D("142.50") and potential_profit(D("100.00"), D("2.50")) == D("150.00")
    # The Hive pipeline: the same signal is worth less at a commission venue
    ctx = RiskContext(D("100000"), D("100000"), D("100000"), (), (100000.0,), {}, (), 0.0)
    gross = evaluate(sim_bot(), signal_at(0, "2.50", commission="0").readings, ctx)  # type: ignore[arg-type]
    net = evaluate(sim_bot(), signal_at(0, "2.50", commission="0.05").readings, ctx)  # type: ignore[arg-type]
    assert isinstance(gross, Proposal) and isinstance(net, Proposal)
    assert net.ev == D("0.48") * D("2.425") - 1 and gross.ev == D("0.48") * D("2.50") - 1 and net.stake < gross.stake


# ================================================================ FX: the fallback router
def test_the_fx_router_uses_the_fixing_then_the_static_map_then_refuses(settings: Settings) -> None:
    clock = SimulationClock(BASE)
    fixings = {"GBP": [(BASE - timedelta(days=1), D("105.00")), (BASE - timedelta(hours=2), D("106.25"))], "EUR": [(BASE - timedelta(days=10), D("90.00"))]}
    router = HistoricalFxRouter(fixings, {"EUR": "92.00", "GBP": "107.50"}, clock, max_age=timedelta(hours=96))
    gbp = router.resolve("GBP")
    assert (gbp.source, gbp.quote.inr_per_unit, gbp.fixed_at) == ("fixing", D("106.25"), BASE - timedelta(hours=2))  # the latest at or before now
    eur = router.resolve("EUR")
    assert (eur.source, eur.quote.inr_per_unit, eur.is_fallback) == ("static", D("92.00"), True)  # its fixing is 10 days old
    assert router.resolve("INR").source == "home"
    with pytest.raises(FxUnavailableError):
        router.resolve("JPY")  # neither a fixing nor a static rate: refused, never guessed
    assert router.report() == {"EUR": {"static": 1}, "GBP": {"fixing": 1}} and router.fallbacks == 1
    with pytest.raises(DataLeakageError):
        router.resolve("GBP", BASE + timedelta(seconds=1))  # a rate from the future


def test_a_foreign_trade_converts_at_the_bet_and_settlement_fixings(settings: Settings) -> None:
    """A GBP stake costs its rupees at the fixing in force when it was placed; the win comes home at
    the fixing in force when it settled, less the 0.5% FX haircut."""
    fx = {"GBP": [(BASE - timedelta(hours=1), D("105.00")), (KICKOFF + timedelta(hours=1), D("110.00"))]}
    store = store_with([(0, "betfair_ex_uk", "HOME", "2.50")], currency="GBP", fx=fx)
    run = simulate(store, stream_of(signal_at(0, "2.50", currency="GBP")), settings)
    (pos,) = run.settled
    assert (pos.fx_bet, pos.fx_bet_source, pos.fx_settle, pos.fx_settle_source) == (D("105.00"), "fixing", D("110.00"), "fixing")
    payout = (pos.stake_ccy * D("2.50") - pos.stake_ccy * D("1.5") * D("0.05")) * D("110.00") * D("0.995")
    assert pos.pnl_inr == (payout - pos.cost_inr).quantize(D("0.01"), rounding="ROUND_DOWN")
    assert pos.cost_inr == (pos.stake_ccy * D("105.00")).quantize(D("0.01"), rounding="ROUND_UP")


# ================================================================ anti-leakage
def test_the_clock_never_runs_backwards_or_past_its_ceiling() -> None:
    clock = SimulationClock(BASE, ceiling=BASE + timedelta(hours=1))
    clock.advance(BASE + timedelta(minutes=5))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(BASE)
    with pytest.raises(DataLeakageError):
        clock.advance(BASE + timedelta(hours=2))
    with pytest.raises(DataLeakageError):
        clock.check(BASE + timedelta(minutes=6), "a peek")


def test_the_market_view_refuses_the_future() -> None:
    store = store_with([(0, "betfair_ex_uk", "HOME", "2.50"), (60, "betfair_ex_uk", "HOME", "2.10")])
    clock = SimulationClock(T(30))
    view = MarketView(store, clock)
    assert view.quote(FIX, "Match Odds", "betfair_ex_uk", "HOME").odds == D("2.50")  # the 60s tick is not there yet
    assert view.result(FIX) is None  # the result is hours away
    with pytest.raises(DataLeakageError):
        view.quote(FIX, "Match Odds", "betfair_ex_uk", "HOME", at=T(61))
    with pytest.raises(DataLeakageError):
        view.result(FIX, at=KICKOFF + timedelta(hours=3))


class _Peeker(MarketReadings):
    """A math model that cheats: it reads the next minute's price."""

    view: MarketView

    def get(self, key: str) -> Decimal | None:
        self.view.quote(FIX, "Match Odds", "betfair_ex_uk", "HOME", at=self.view.clock.now + timedelta(minutes=1))
        return D("0.9")


def test_a_strategy_that_peeks_at_the_future_fails_the_run(settings: Settings) -> None:
    store = store_with([(0, "betfair_ex_uk", "HOME", "2.50")])
    sig = signal_at(0, "2.50")
    sim = Simulator(store, stream_of(sig), [sim_bot()], RealityConfig(void_rate=0.0), settings, fx_factory(store, settings), start=BASE, end=KICKOFF, resume_after=None)
    peeker = _Peeker(sig.readings.market)
    peeker.view = sim.view
    object.__setattr__(sim.stream.signals[0], "readings", peeker)
    with pytest.raises(DataLeakageError):
        sim.run()


# ================================================================ the database lock (SQLite)
LAB_TABLES = [User.__table__, TradingBot.__table__, SmallcaseRegistryModel.__table__, LabFixture.__table__, LabOddsTick.__table__, LabFixtureResult.__table__, LabFxRate.__table__, LabBacktestRun.__table__]


def _metadata() -> MetaData:
    md = MetaData()
    for table in LAB_TABLES:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(_metadata().create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


SMALL = {"rows": 2500, "seed": 7, "start": date(2026, 1, 1), "days": 60}


@pytest_asyncio.fixture
async def seeded(sessions: async_sessionmaker[AsyncSession]) -> Any:
    dataset = generate_dataset(**SMALL)
    async with sessions() as session:
        await seed_dataset(session, dataset)
        await seed_registry(session)
        await session.commit()
    return dataset


@pytest.mark.asyncio
async def test_the_session_lock_refuses_unbounded_future_raw_and_write_reads(sessions: async_sessionmaker[AsyncSession], seeded: Any) -> None:
    middle = seeded.ticks[len(seeded.ticks) // 2].created_at
    clock = SimulationClock(middle)
    async with sessions() as session:
        remove = install_time_lock(session, clock)
        for statement in (select(LabOddsTick), select(func.count(LabOddsTick.id)), select(LabFxRate.currency)):
            with pytest.raises(DataLeakageError, match="unbounded"):
                await session.execute(statement)
        with pytest.raises(DataLeakageError, match="future"):
            await session.execute(select(LabOddsTick).execution_options(**{AS_OF: middle + timedelta(seconds=1)}))
        with pytest.raises(DataLeakageError, match="raw SQL"):
            await session.execute(text("SELECT * FROM lab_hist_ticks"))
        # A declared read sees exactly the past, whatever its own WHERE asks for
        seen = await session.scalar(select(func.count(LabOddsTick.id)).where(LabOddsTick.created_at > datetime(1970, 1, 1, tzinfo=UTC)).execution_options(**{AS_OF: middle}))
        assert seen == sum(1 for t in seeded.ticks if t.created_at <= middle) < len(seeded.ticks)
        newest = await session.scalar(select(func.max(LabOddsTick.created_at)).execution_options(**{AS_OF: middle}))
        assert newest.replace(tzinfo=UTC) <= middle
        remove()
        assert await session.scalar(select(func.count(LabOddsTick.id))) == len(seeded.ticks)  # the lock is gone with its guard


@pytest.mark.asyncio
async def test_an_in_sample_store_holds_no_out_of_sample_row(sessions: async_sessionmaker[AsyncSession], seeded: Any, settings: Settings) -> None:
    first, last = seeded.span
    split = first + (last - first) * 0.75
    async with sessions() as session:
        in_sample = await HistoricalStore.load(session, horizon=split)
        full = await HistoricalStore.load(session, horizon=last + timedelta(days=3))
    assert in_sample.ticks and max(t.at for t in in_sample.ticks) <= split < max(t.at for t in full.ticks)
    assert all(r.known_at <= split for r in in_sample.results.values()) and len(in_sample.results) < len(full.results)
    assert all(at <= split for rows in in_sample.fx.values() for at, _ in rows)
    assert len(full.ticks) == len(seeded.ticks)
    stream = ReplayEngine(in_sample, settings, fx_factory(in_sample, settings), {}).build(first, split)
    assert stream.signals and all(s.at <= split for s in stream.signals)


# ================================================================ the seeder
def test_the_seeder_writes_exactly_10000_realistic_rows_over_a_year() -> None:
    data = generate_dataset()
    first, last = data.span
    assert len(data.ticks) == 10_000 and (last - first) >= timedelta(days=365) and first == datetime(2025, 10, 1, tzinfo=UTC)
    assert all(t.odds > 1 and t.currency == {b.bookmaker_id: b.currency for b in BOOKS}[t.bookmaker_id] for t in data.ticks)
    assert {t.bookmaker_id for t in data.ticks} == {b.bookmaker_id for b in BOOKS}
    assert generate_dataset().ticks[1234] == data.ticks[1234]  # deterministic for a seed
    assert any(t.is_suspended for t in data.ticks) and any(f.status == "POSTPONED" for f in data.fixtures)
    # Margins: the exchange is tight, the soft books are not (and every complete quote is over 100%)
    quotes: dict[tuple[str, str, str, datetime], dict[str, Decimal]] = {}
    for t in data.ticks:
        if not t.is_suspended:
            quotes.setdefault((t.fixture_id, t.market, t.bookmaker_id, t.created_at), {})[t.selection] = t.odds
    margins: dict[str, list[float]] = {}
    for (_, market, book, _), prices in quotes.items():
        if len(prices) == (3 if market == "Match Odds" else 2):
            margins.setdefault(book, []).append(float(sum(1 / p for p in prices.values())))
    assert statistics.fmean(margins["betfair_ex_uk"]) < statistics.fmean(margins["pinnacle"]) < statistics.fmean(margins["williamhill"])
    assert statistics.fmean(margins["williamhill"]) > 1.04
    # FX has holes (missed days, the EUR outage) for the fallback to fill
    eur_days = sorted(x.created_at for x in data.fx if x.currency == "EUR")
    assert max(b - a for a, b in zip(eur_days, eur_days[1:])) >= timedelta(days=21)


def test_the_seeders_maths() -> None:
    probs = outcome_probabilities(1.5, 1.1)
    assert math.isclose(probs["HOME"] + probs["DRAW"] + probs["AWAY"], 1.0) and math.isclose(probs["OVER"] + probs["UNDER"], 1.0)
    assert probs["HOME"] > probs["AWAY"]
    assert round_odds(2.137, "betfair") == D("2.12") and round_odds(3.33, "betfair") == D("3.30") and round_odds(5.57, "betfair") == D("5.50")
    assert round_odds(4.37, "bookmaker") == D("4.35") and round_odds(1.999, "cents") == D("1.999")


# ================================================================ the replay engine
def test_the_replay_turns_the_tape_into_the_hives_signal_stream(settings: Settings) -> None:
    data = generate_dataset(**SMALL)
    first, last = data.span
    store = HistoricalStore.from_dataset(data, horizon=last + timedelta(days=3))
    stream = ReplayEngine(store, settings, fx_factory(store, settings), {"betfair_ex_uk": D("0.05")}).build(first, last)
    assert stream.frames == len(store.frames) and stream.signals
    assert [s.at for s in stream.signals] == sorted(s.at for s in stream.signals)
    for s in stream.signals:
        edge = s.edge
        assert isinstance(edge, EdgeSignal) and edge.timestamp == s.at and edge.ev >= D("0.005")
        assert edge.commission == (D("0.05") if edge.bookmaker_id == "betfair_ex_uk" else 0)
        assert store.fixtures[edge.fixture_id].commence_time > s.at  # pre-match only
        assert len(s.market.history) <= settings.HIVE_HISTORY_POINTS


# ================================================================ reality penalties
def test_the_outbound_rate_limit_is_two_a_second_with_a_three_second_queue() -> None:
    limiter = OutboundRateLimiter(RealityConfig())
    waits = [limiter.acquire("betfair_ex_uk", BASE) for _ in range(9)]
    assert waits == [0.0, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, None]  # burst 2, then a token every 0.5s, refused past 3s
    assert limiter.acquire("pinnacle", BASE) == 0.0  # per venue
    assert limiter.acquire("betfair_ex_uk", BASE + timedelta(seconds=10)) == 0.0  # the bucket refills


def test_the_rate_limit_queues_and_then_refuses_simultaneous_orders(settings: Settings) -> None:
    store = store_with([(0, "betfair_ex_uk", "HOME", "2.50")])
    stream = SignalStream(BASE, KICKOFF)
    for i in range(10):  # ten edges on ten fixtures-worth of the same instant, all at Betfair
        sig = signal_at(0, "2.50")
        object.__setattr__(sig, "cell", f"{FIX}|Match Odds|HOME|{i}")
        stream.add(sig)
    run = Simulator(store, stream, [sim_bot(cooldown_seconds=0)], RealityConfig(void_rate=0.0, slippage_pct=D(0)), settings, fx_factory(store, settings), start=BASE, end=KICKOFF, resume_after=None).run()
    assert run.rejections["OUTBOUND_THROTTLED"] == 2 and run.queued_orders == 6 and run.queue_seconds == pytest.approx(0.5 + 1 + 1.5 + 2 + 2.5 + 3)
    assert sorted(p.queue_ms for p in run.positions) == [0, 0, 500, 1000, 1500, 2000, 2500, 3000]  # each queued order lands that much later


def test_quadratic_impact_past_5_percent_and_the_partial_fill_it_forces() -> None:
    config = RealityConfig(slippage_pct=D(0))
    assert impact_multiplier(D("0.05"), config) == 1 and impact_multiplier(D("0.25"), config) == D("0.92")  # 1 - 2 * 0.2^2
    order = OrderAtVenue(D("3.00"), D("2.00"), D("0.5"), D(0), D("0.01"), D("2500.00"))
    full = fill_at_arrival(order, VenueQuote(D("3.00"), D("10000"), False), D("10000"), config)
    assert (full.status, full.fill_odds, full.participation) == ("FILLED", D("2.84"), D("0.25"))  # 1 + 2 * 0.92
    small = fill_at_arrival(OrderAtVenue(D("3.00"), D("2.00"), D("0.5"), D(0), D("0.01"), D("400.00")), VenueQuote(D("3.00"), D("10000"), False), D("10000"), config)
    assert small.fill_odds == D("3.00") and small.impact_pct == 0  # 4% of the pool: no impact
    tight = fill_at_arrival(OrderAtVenue(D("3.00"), D("2.95"), D("0.5"), D(0), D("0.01"), D("2500.00")), VenueQuote(D("3.00"), D("10000"), False), D("10000"), config)
    share = max_participation(D("3.00"), D("2.95"), config)
    assert tight.status == "PARTIAL" and tight.reason == "IMPACT_PARTIAL" and tight.stake_ccy == (share * 10000).quantize(D("0.01"), rounding="ROUND_DOWN")
    assert tight.fill_odds >= D("2.95") and tight.stake_ccy < D("2500")  # the floor holds: only what keeps the average above it fills
    capped = fill_at_arrival(OrderAtVenue(D("3.00"), D("1.01"), D("0.9"), D(0), D("0.01"), D("50000")), VenueQuote(D("3.00"), D("10000"), False), D("10000"), config)
    assert capped.stake_ccy <= D("10000")  # never more than the money at the price
    slipped = fill_at_arrival(order, VenueQuote(D("3.00"), D("10000"), False), D("10000"), RealityConfig(slippage_pct=D("1")))
    assert slipped.fill_odds == (1 + D("1.84") * D("0.99")).quantize(D("0.0001"), rounding="ROUND_DOWN")  # slippage is a cost, after impact
    assert fill_at_arrival(order, VenueQuote(D("3.00"), D("10000"), True), D("10000"), config).reason == "MARKET_SUSPENDED"


def test_a_void_refunds_the_stake_exactly_postponed_or_injected(settings: Settings) -> None:
    quotes = [(0, "betfair_ex_uk", "HOME", "2.50")]
    injected = simulate(store_with(quotes), stream_of(signal_at(0, "2.50")), settings, void_rate=1.0)
    (pos,) = injected.settled
    assert (pos.status, pos.void_reason, pos.pnl_inr) == ("VOID", "INJECTED", D(0)) and injected.curve[-1][1] == D("100000.00")
    postponed = simulate(store_with(quotes, status="POSTPONED", result_at=KICKOFF - timedelta(minutes=30)), stream_of(signal_at(0, "2.50")), settings)
    (pos,) = postponed.settled
    assert (pos.status, pos.void_reason, pos.pnl_inr) == ("VOID", "POSTPONED", D(0)) and postponed.curve[-1][1] == D("100000.00")
    # Announced before the order lands: the market is closed, nothing fills
    early = store_with(quotes, status="POSTPONED", result_at=T(1))
    closed = simulate(early, stream_of(signal_at(0, "2.50")), settings)
    assert closed.positions == [] and closed.rejections == {"MARKET_CLOSED": 1}
    metrics = compute_metrics(injected, store_with(quotes))
    assert metrics["trades"] == 0 and metrics["voids"] == metrics["voids_injected"] == 1 and metrics["pnl_inr"] == 0  # a void is no win, and no turnover


def test_the_simulator_keeps_the_hives_wash_trade_and_velocity_rules(settings: Settings) -> None:
    home = signal_at(0, "2.50")
    away = signal_at(1, "4.50", p="0.25", selection="AWAY")  # +8.1% net: a real edge, on the other side of the bot's own bet
    store = store_with([(0, "betfair_ex_uk", "HOME", "2.50"), (0, "betfair_ex_uk", "AWAY", "4.50")])
    run = simulate(store, stream_of(home, away), settings, latency_ms=(100, 100))
    assert run.decisions["FIRED"] == 1 and run.decisions["BLOCKED:WASH_TRADE"] == 1 and len(run.positions) == 1
    # The first order is still in flight (2s latency) when the opposite edge arrives a second later:
    # it is already booked, so the second is a wash trade all the same
    in_flight = simulate(store, stream_of(home, away), settings, latency_ms=(2000, 2000))
    assert in_flight.decisions["BLOCKED:WASH_TRADE"] == 1 and [p.selection for p in in_flight.positions] == ["HOME"]
    fast = stream_of(*[signal_at(i * 2, "2.50") for i in range(5)])
    velocity = simulate(store_with([(0, "betfair_ex_uk", "HOME", "2.50")]), fast, settings, sim_bot(max_bets_per_minute=3))
    assert velocity.decisions["FIRED"] == 3 and velocity.decisions["SUSPENSION:VELOCITY_BREAKER"] == 1


# ================================================================ Monte Carlo
def test_monte_carlo_reshuffles_the_same_trades_and_measures_ruin() -> None:
    trades = [10.0] * 5 + [-30.0]  # ruin only if the loss comes first: 25 - 30 < 0, a 1-in-6 ordering
    mc = risk_of_ruin(trades, 25.0, iterations=6000, seed=1)
    assert 14.0 < mc["risk_of_ruin_pct"] < 19.5  # 16.7% in expectation
    assert mc["final_equity_inr"] == 45.0  # the same trades: the same end, whatever the order
    assert risk_of_ruin(trades, 25.0, iterations=500, seed=9) == risk_of_ruin(trades, 25.0, iterations=500, seed=9)
    safe = risk_of_ruin(trades, 1000.0, iterations=1000, seed=1)
    assert safe["risk_of_ruin_pct"] == 0.0 and safe["max_drawdown_pct"]["p99"] < 3.1
    floor = risk_of_ruin(trades, 25.0, iterations=2000, ruin_floor_pct=50, seed=1)
    assert floor["risk_of_ruin_pct"] > mc["risk_of_ruin_pct"]  # a higher floor is touched more often
    assert mc["iterations"] == 6000 and len(mc["fan"]) == 6 and mc["bootstrap"]["p_loss_pct"] > 0
    assert risk_of_ruin([], 100.0)["risk_of_ruin_pct"] == 0.0


# ================================================================ sweep, lock, verdict, metrics
def test_the_kelly_sweep_grid_and_its_winner() -> None:
    grid = kelly_grid()
    assert len(grid) == 10 and grid[0] == D("0.1") and grid[-1] == D("0.5") and all(b > a for a, b in zip(grid, grid[1:]))
    rows = [{"kelly": "0.1", "sharpe": 0.8, "roi_pct": 3.0}, {"kelly": "0.3", "sharpe": 1.2, "roi_pct": 2.0}, {"kelly": "0.5", "sharpe": None, "roi_pct": 9.0}]
    assert best_row(rows)["kelly"] == "0.3"  # the best Sharpe; a run without one never wins
    assert best_row([{"kelly": "0.2", "sharpe": 1.0, "roi_pct": 1.0}, {"kelly": "0.4", "sharpe": 1.0, "roi_pct": 1.0}])["kelly"] == "0.2"  # ties: the smaller bet
    locked = LockedParameters(D("0.3"), "sweep:sharpe", "in_sample")
    assert locked.fingerprint([{"id": "x"}]) == LockedParameters(D("0.3"), "sweep:sharpe", "in_sample").fingerprint([{"id": "x"}]) != locked.fingerprint([{"id": "y"}])


def test_the_overfit_verdict() -> None:
    good = {"sharpe": 1.5, "roi_pct": 4.0, "trades": 50}
    assert overfit_verdict(good, {"sharpe": 1.0, "roi_pct": 2.0, "trades": 20})["verdict"] == "ROBUST"
    assert overfit_verdict(good, {"sharpe": 0.4, "roi_pct": 0.5, "trades": 20})["verdict"] == "DEGRADED"
    assert overfit_verdict(good, {"sharpe": -0.2, "roi_pct": -1.0, "trades": 20})["verdict"] == "OVERFIT"
    assert overfit_verdict(good, {"sharpe": 2.0, "roi_pct": 3.0, "trades": 4})["verdict"] == "INSUFFICIENT_DATA"
    assert overfit_verdict({"sharpe": -0.5, "roi_pct": -2.0, "trades": 50}, {"sharpe": 1.0, "roi_pct": 1.0, "trades": 20})["verdict"] == "NO_EDGE"


def test_drawdown_sharpe_sortino_calmar_on_a_known_curve() -> None:
    day = timedelta(days=1)
    curve = [(BASE, D("100")), (BASE + day, D("110")), (BASE + 2 * day, D("99")), (BASE + 3 * day, D("121"))]
    dd = drawdown(curve)
    assert dd["max_drawdown_pct"] == pytest.approx(10.0) and dd["max_drawdown_inr"] == pytest.approx(11.0)
    daily = daily_equity(curve, BASE, BASE + 3 * day)
    values = [v for _, v in daily]
    assert values == [100.0, 110.0, 99.0, 121.0]  # end of each day: Mar 7 (no settlement yet), 8, 9, 10
    r = ratios(daily, 100.0)
    returns = [b / a - 1 for a, b in zip([100.0, *values], values)]
    mean, sd = statistics.fmean(returns), statistics.stdev(returns)
    assert r["sharpe"] == pytest.approx(mean / sd * math.sqrt(365), rel=1e-3)
    downside = math.sqrt(sum(min(x, 0) ** 2 for x in returns) / len(returns))
    assert r["sortino"] == pytest.approx(mean / downside * math.sqrt(365), rel=1e-3)


# ================================================================ the runner and the API
def _strategy(name: str = "Shin consensus", **extra: Any) -> StrategySpec:
    return StrategySpec(name=name, math_models=["math.consensus", "math.devig_shin", "math.kelly_criterion"], risk_models=["risk.drawdown", "risk.exposure"],
                        target_bet_types=["bet.match_winner_1x2", "bet.over_under_goals", "bet.single"], **extra)


@pytest.mark.asyncio
async def test_bots_are_built_from_the_seeded_registry_rows(sessions: async_sessionmaker[AsyncSession], seeded: Any) -> None:  # noqa: ARG001
    async with sessions() as session:
        prepared = await prepare_bots(session, uuid.uuid4(), BacktestParams(strategies=[_strategy()]))
        (bot,) = prepared.bots
        rows = {r.component_key: r for r in (await session.execute(select(SmallcaseRegistryModel))).scalars()}
        used = prepared.components[str(bot.id)]
        assert [c["key"] for c in used] == ["math.consensus", "math.devig_shin", "math.kelly_criterion", "risk.drawdown", "risk.exposure", "bet.match_winner_1x2", "bet.over_under_goals", "bet.single"]
        assert all(c["registry_id"] == str(rows[c["key"]].id) for c in used)
        with pytest.raises(BacktestError, match="backtest only"):
            await prepare_bots(session, uuid.uuid4(), BacktestParams(strategies=[StrategySpec(name="x", math_models=["math.lstm", "math.kelly_criterion"], target_bet_types=["bet.match_winner_1x2"])]))
    async with sessions() as session:
        await session.execute(SmallcaseRegistryModel.__table__.delete())
        await session.commit()
        with pytest.raises(BacktestError, match="registry is empty"):
            await prepare_bots(session, uuid.uuid4(), BacktestParams(strategies=[_strategy()]))


def test_the_walk_forward_locks_the_in_sample_winner_and_tests_it_out_of_sample(settings: Settings) -> None:
    data = generate_dataset(**SMALL)
    first, last = data.span
    params = BacktestParams(strategies=[_strategy()], sweep_steps=3, monte_carlo_iterations=200, train_ratio=0.7)
    window = Window(first, last, first + (last - first) * 0.7)
    full = HistoricalStore.from_dataset(data, horizon=last + timedelta(days=3))
    in_sample = HistoricalStore.from_dataset(data, horizon=window.split)
    bot = sim_bot("Shin consensus", math_models=("math.consensus", "math.devig_shin", "math.kelly_criterion"), risk_models=("risk.drawdown", "risk.exposure"),
                  target_bet_types=("bet.match_winner_1x2", "bet.over_under_goals", "bet.single"), max_bets_per_minute=3, cooldown_seconds=300)
    result = execute([bot], params, window, full, in_sample, settings)
    sweep, wf = result["sweep"], result["walk_forward"]
    assert sweep["window"] == "in_sample" and [r["kelly"] for r in sweep["rows"]] == ["0.1", "0.3", "0.5"]
    assert wf["locked_parameters"]["kelly_multiplier"] == sweep["best_kelly"] == result["locked_parameters"]["kelly_multiplier"]
    assert sweep["best_kelly"] == best_row(sweep["rows"])["kelly"]
    assert result["per_bot"][0]["kelly_multiplier"] == sweep["best_kelly"]  # the full-window run uses exactly the locked value
    assert wf["verdict"]["verdict"] in {"ROBUST", "DEGRADED", "OVERFIT", "INSUFFICIENT_DATA", "NO_EDGE"}
    assert result["monte_carlo"]["iterations"] == 200 and result["monte_carlo"]["trades"] == result["metrics"]["trades"]
    assert any("synthetic" in w for w in result["warnings"])
    for key in ("roi_pct", "max_drawdown_pct", "sharpe", "sortino", "calmar", "mae_avg_pct", "clv_beat_pct"):
        assert key in result["metrics"]
    assert result["curves"]["equity"][0]["equity"] == 100000.0 and len(result["curves"]["underwater"]) == len(result["curves"]["equity"])


@pytest.mark.asyncio
async def test_the_lab_api_runs_a_backtest_to_completion(sessions: async_sessionmaker[AsyncSession], seeded: Any, settings: Settings) -> None:  # noqa: ARG001
    from fastapi import FastAPI

    from app.api.deps import get_current_admin, get_current_user
    from app.api.v1 import cfo_execution, lab_quant
    from app.workers import lab_worker

    async with sessions() as session:
        user = User(username="lab_api", hashed_password="x")
        other = User(username="lab_other", hashed_password="x")
        session.add_all([user, other])
        await session.commit()
    app = FastAPI()
    app.include_router(lab_quant.router, prefix="/api/v1")
    current = {"user": user}
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    app.dependency_overrides[get_current_admin] = lambda: current["user"]
    app.dependency_overrides[cfo_execution.get_session_factory] = lambda: sessions
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        info = (await client.get("/api/v1/lab/quant/dataset")).json()
        assert info["ticks"] == SMALL["rows"] and info["synthetic"] and {b["bookmaker_id"] for b in info["books"]} == {b.bookmaker_id for b in BOOKS}
        bad = await client.post("/api/v1/lab/quant/backtests", json={"strategies": [{"name": "x", "math_models": ["math.lstm", "math.kelly_criterion"], "target_bet_types": ["bet.match_winner_1x2"]}]})
        assert bad.status_code == 422 and bad.json()["detail"]["reason"] == "BACKTEST_INVALID"
        body = {"name": "API run", "strategies": [_strategy().model_dump(mode="json")], "sweep_steps": 2, "monte_carlo_iterations": 100}
        made = await client.post("/api/v1/lab/quant/backtests", json=body)
        assert made.status_code == 202 and made.json()["status"] == "QUEUED"
        await asyncio.gather(*list(lab_worker._INLINE))  # noqa: SLF001 - the inline executor's task
        run_id = made.json()["id"]
        done = (await client.get(f"/api/v1/lab/quant/backtests/{run_id}")).json()
        assert done["status"] == BacktestStatus.COMPLETED, done["error"]
        assert done["result"]["metrics"]["trades"] >= 0 and done["result"]["bots"][0]["components"]
        listed = (await client.get("/api/v1/lab/quant/backtests")).json()
        assert [r["id"] for r in listed] == [run_id] and listed[0]["summary"]["best_kelly"] == done["result"]["sweep"]["best_kelly"]
        current["user"] = other
        assert (await client.get(f"/api/v1/lab/quant/backtests/{run_id}")).status_code == 404  # not yours
        current["user"] = user
        assert (await client.delete(f"/api/v1/lab/quant/backtests/{run_id}")).status_code == 204
