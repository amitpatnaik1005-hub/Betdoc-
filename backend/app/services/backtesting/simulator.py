"""One backtest run: a set of bots trading a signal stream, event by event, on simulated time.

The decision rules are the live Hive's (``hive_engine``), on in-memory state instead of Redis and the
ledger, and the pipeline is the live one itself (``hive_pipeline.evaluate``):

* every active bot evaluates each signal against its own sub-bankroll; one order per signal, sized
  by the strongest proposal (stake, then EV), the rest merge into it;
* a bet against an open position on another outcome of the same market is a wash trade: blocked;
* the velocity breaker (``max_bets_per_minute`` within 60s) and the 24h drawdown breaker suspend a
  bot; a market shock (the flash-crash rule) halts every bot. Live, a person lifts either; here they
  resume after ``resume_after`` (none: never, within the run);
* cooldowns per bot and selection; TWAP slices 60-120s apart for bots that slice, each re-checking
  the halt, the bot, the live edge, wash trades and the breakers when it comes due.

An order then meets ``reality``: a stake converted at the historical FX rate and reserved, the
outbound rate limit, network latency, the quote at arrival, liquidity, impact and slippage. Fills
settle when the result is known: per bot, venue and market, the venue's commission is taken off net
winnings (Betfair's rule), the payout converts home at the settlement-time rate less the FX haircut,
and a void (postponed, or injected) refunds the stake to the paisa.
"""

from __future__ import annotations

import heapq
import itertools
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from app.core.config import Settings
from app.domain.math.arbitrage_calc import HOME_CURRENCY, from_inr, leg_cost_inr
from app.services.aryabhata_engine import MIN_EV
from app.services.backtesting.fx_router import FxResolution, HistoricalFxRouter
from app.services.backtesting.reality import (
    LatencyModel,
    OrderAtVenue,
    OutboundRateLimiter,
    RealityConfig,
    VenueQuote,
    fill_at_arrival,
    injected_void,
    order_floor,
    stable_rng,
)
from app.services.backtesting.replay_engine import HistoricalStore, MarketShock, MarketView, ResultInfo, SignalStream, SimulatedSignal
from app.services.backtesting.time_lock import SimulationClock
from app.services.fx_rates import FxUnavailableError
from app.services.hive_engine import slice_countdowns, slice_stake
from app.services.hive_pipeline import Proposal, Rejection, RiskContext, evaluate

ZERO, ONE, PAISA, HUNDRED = Decimal(0), Decimal(1), Decimal("0.01"), Decimal(100)
_RESULT, _SHOCK, _ARRIVAL, _SLICE, _SIGNAL = range(5)  # at one instant: results first, new signals last
_LOG_LIMIT = 300


@dataclass(frozen=True, slots=True)
class SimBot:
    """A bot as the pipeline reads it (``hive_pipeline.evaluate`` duck-types ``TradingBot``)."""

    id: uuid.UUID
    name: str
    math_models: tuple[str, ...]
    risk_models: tuple[str, ...]
    target_bet_types: tuple[str, ...]
    kelly_multiplier: Decimal
    max_stake_pct: Decimal
    min_edge_pct: Decimal
    min_quoting_books: int
    min_market_liquidity: Decimal
    enable_order_slicing: bool
    slice_size_inr: Decimal
    max_bets_per_minute: int
    drawdown_limit_pct: Decimal
    cooldown_seconds: int
    risk_params: Mapping[str, Any]
    capital: Decimal
    origin: str = "bot"  # bot (a saved Hive bot) | strategy (an ad-hoc pipeline)

    @property
    def min_ev(self) -> Decimal:
        return max(self.min_edge_pct / HUNDRED, MIN_EV)

    def tuned(self, kelly_multiplier: Decimal) -> SimBot:
        return replace(self, kelly_multiplier=kelly_multiplier)

    def parameters(self) -> dict[str, Any]:
        return {
            "kelly_multiplier": str(self.kelly_multiplier), "max_stake_pct": str(self.max_stake_pct), "min_edge_pct": str(self.min_edge_pct),
            "min_quoting_books": self.min_quoting_books, "min_market_liquidity": str(self.min_market_liquidity),
            "enable_order_slicing": self.enable_order_slicing, "slice_size_inr": str(self.slice_size_inr), "max_bets_per_minute": self.max_bets_per_minute,
            "drawdown_limit_pct": str(self.drawdown_limit_pct), "cooldown_seconds": self.cooldown_seconds,
            "math_models": list(self.math_models), "risk_models": list(self.risk_models), "target_bet_types": list(self.target_bet_types),
        }


@dataclass(slots=True)
class Position:
    id: int
    bot_id: uuid.UUID
    fixture_id: str
    market: str
    selection: str
    bookmaker_id: str
    currency: str
    stake_ccy: Decimal
    cost_inr: Decimal
    fx_bet: Decimal
    fx_bet_source: str
    requested_odds: Decimal
    arrival_odds: Decimal
    fill_odds: Decimal
    commission: Decimal
    conviction: Decimal
    ev_decision: Decimal
    decided_at: datetime
    filled_at: datetime
    latency_ms: int
    queue_ms: int
    participation: Decimal
    impact_pct: Decimal
    fill_reason: str
    plan_id: int | None = None
    status: str = "OPEN"  # OPEN | WON | LOST | VOID | OPEN_AT_END
    settled_at: datetime | None = None
    pnl_inr: Decimal | None = None
    commission_inr: Decimal = ZERO
    fx_settle: Decimal | None = None
    fx_settle_source: str | None = None
    void_reason: str | None = None

    @property
    def ev_at_fill(self) -> Decimal:
        net = (self.fill_odds - ONE) * (ONE - self.commission) + ONE
        return self.conviction * net - ONE


@dataclass(slots=True)
class Account:
    bot: SimBot
    capital: Decimal
    equity: Decimal
    available: Decimal
    exposure: Decimal = ZERO
    pnls: list[Decimal] = field(default_factory=list)
    settled_times: list[datetime] = field(default_factory=list)
    curve: list[float] = field(default_factory=list)
    open: dict[int, Position] = field(default_factory=dict)
    fires: deque[datetime] = field(default_factory=deque)
    suspended_until: datetime | None = None
    suspended_reason: str | None = None
    cooldowns: dict[tuple[str, str], datetime] = field(default_factory=dict)
    in_flight: dict[int, tuple[str, Decimal]] = field(default_factory=dict)  # order id -> (fixture, reserved rupees)

    def active(self, now: datetime) -> bool:
        if self.suspended_reason is None:
            return True
        if self.suspended_until is not None and now >= self.suspended_until:
            self.suspended_reason, self.suspended_until = None, None
            return True
        return False

    def risk_context(self, now: datetime, started: datetime) -> RiskContext:
        base = float(self.capital) if self.capital > 0 else 1.0
        open_stakes: dict[str, Decimal] = defaultdict(lambda: ZERO)
        open_edges = []
        for fixture_id, cost in self.in_flight.values():  # booked PENDING live: exposure already
            open_stakes[fixture_id] += cost
        for pos in self.open.values():
            open_stakes[pos.fixture_id] += pos.cost_inr
            net = (pos.fill_odds - ONE) * (ONE - pos.commission) + ONE
            open_edges.append((float(pos.conviction * net - ONE), float(net)))
        return RiskContext(
            equity=self.equity,
            available=max(self.available, ZERO),
            allocated=self.capital,
            returns=tuple(float(p) / base for p in self.pnls),
            equity_curve=tuple([float(self.capital), *self.curve]),
            open_stakes=dict(open_stakes),
            open_edges=tuple(open_edges),
            pipeline_age_days=max((now - started).total_seconds() / 86_400, 0.0),
        )


@dataclass(slots=True)
class _Order:
    id: int
    account: Account
    signal: SimulatedSignal
    stake_ccy: Decimal
    cost_inr: Decimal
    fx: FxResolution
    floor: Decimal
    conviction: Decimal
    ev: Decimal
    decided_at: datetime
    arrives_at: datetime
    latency_ms: int
    queue_ms: int
    plan_id: int | None


@dataclass(slots=True)
class _Plan:
    id: int
    account: Account
    signal: SimulatedSignal
    conviction: Decimal
    stakes: list[Decimal]
    status: list[str]


@dataclass(slots=True)
class RunResult:
    label: str
    start: datetime
    end: datetime
    capital: Decimal
    bots: list[SimBot]
    positions: list[Position]
    curve: list[tuple[datetime, Decimal]]  # portfolio equity after each settlement
    bot_curves: dict[str, list[tuple[datetime, Decimal]]]
    decisions: Counter[str]
    rejections: Counter[str]
    events: list[dict[str, Any]]
    fx: dict[str, dict[str, int]]
    queued_orders: int
    queue_seconds: float
    signals: int

    @property
    def settled(self) -> list[Position]:
        return [p for p in self.positions if p.status in ("WON", "LOST", "VOID")]


class Simulator:
    def __init__(
        self,
        store: HistoricalStore,
        stream: SignalStream,
        bots: Sequence[SimBot],
        config: RealityConfig,
        settings: Settings,
        fx: Callable[[SimulationClock], HistoricalFxRouter],
        *,
        start: datetime,
        end: datetime,
        resume_after: timedelta | None = timedelta(hours=24),
        label: str = "run",
    ) -> None:
        if not bots:
            raise ValueError("a run needs at least one bot")
        if len({b.id for b in bots}) != len(bots):
            raise ValueError("each bot runs once per backtest")
        self.store, self.stream, self.config, self.settings = store, stream, config, settings
        self.start, self.end, self.label = start, end, label
        self.resume_after = resume_after
        self.clock = SimulationClock(start, ceiling=store.horizon)
        self.view = MarketView(store, self.clock)
        self.router = fx(self.clock)
        self.latency = LatencyModel(config)
        self.limiter = OutboundRateLimiter(config)
        self.accounts = [Account(bot, bot.capital, bot.capital, bot.capital) for bot in bots]
        self._by_bot = {a.bot.id: a for a in self.accounts}
        self._heap: list[tuple[datetime, int, int, int, Any]] = []
        self._seq = itertools.count()
        self._pos_ids = itertools.count(1)
        self._plan_ids = itertools.count(1)
        self.halted_until: datetime | None = None
        self.halt_reason: str | None = None
        self.positions: list[Position] = []
        self.decisions: Counter[str] = Counter()
        self.rejections: Counter[str] = Counter()
        self.events: list[dict[str, Any]] = []
        self.curve: list[tuple[datetime, Decimal]] = [(start, self._equity())]
        self.bot_curves: dict[str, list[tuple[datetime, Decimal]]] = {str(a.bot.id): [(start, a.equity)] for a in self.accounts}
        self.queued = 0
        self.queue_seconds = 0.0
        self._settling: set[str] = set()
        self._signals = 0
        self._inflight: dict[int, _Order] = {}  # reserved and sent, not yet landed: the ledger already holds them
        self._order_ids = itertools.count(1)

    # -------------------------------------------------------------- the loop
    def run(self) -> RunResult:
        for signal in self.stream.signals:
            if self.start <= signal.at <= self.end:
                self._push(signal.at, _SIGNAL, signal)
        for shock in self.stream.shocks:
            if self.start <= shock.at <= self.end:
                self._push(shock.at, _SHOCK, shock)
        while self._heap:
            at, kind, _, _, payload = heapq.heappop(self._heap)
            self.clock.advance(at)
            if kind == _SIGNAL:
                self._signals += 1
                self._on_signal(payload)
            elif kind == _ARRIVAL:
                self._on_arrival(payload)
            elif kind == _RESULT:
                self._on_result(payload)
            elif kind == _SHOCK:
                self._on_shock(payload)
            elif kind == _SLICE:
                self._on_slice(*payload)
        for account in self.accounts:
            for pos in account.open.values():
                pos.status = "OPEN_AT_END"  # its result is past this run's horizon: valued at cost, never graded
        return RunResult(
            self.label, self.start, self.end, sum((a.capital for a in self.accounts), ZERO), [a.bot for a in self.accounts], self.positions,
            self.curve, self.bot_curves, self.decisions, self.rejections, self.events, self.router.report(), self.queued, self.queue_seconds, self._signals,
        )

    def _push(self, at: datetime, kind: int, payload: Any) -> None:
        heapq.heappush(self._heap, (at, kind, next(self._seq), kind, payload))

    def _log(self, kind: str, **detail: Any) -> None:
        if len(self.events) < _LOG_LIMIT:
            self.events.append({"at": self.clock.now.isoformat(), "event": kind, **{k: (str(v) if isinstance(v, Decimal | uuid.UUID) else v) for k, v in detail.items()}})

    def _equity(self) -> Decimal:
        return sum((a.equity for a in self.accounts), ZERO)

    def _halted(self, now: datetime) -> bool:
        if self.halt_reason is None:
            return False
        if self.halted_until is not None and now >= self.halted_until:
            self.halt_reason, self.halted_until = None, None
            return False
        return True

    # -------------------------------------------------------------- signals
    def _on_signal(self, signal: SimulatedSignal) -> None:
        now = signal.at
        if self._halted(now):
            self.decisions["HALTED"] += 1
            return
        edge = signal.edge
        proposals: list[tuple[Account, Proposal]] = []
        for account in self.accounts:
            if not account.active(now):
                self.decisions[f"SUSPENDED:{account.suspended_reason}"] += 1
                continue
            until = account.cooldowns.get((edge.fixture_id, edge.selection))
            if until is not None and now < until:
                self.decisions["COOLDOWN"] += 1
                continue
            verdict = evaluate(account.bot, signal.readings, account.risk_context(now, self.start))  # type: ignore[arg-type]
            if isinstance(verdict, Rejection):
                self.decisions[f"SKIPPED:{verdict.reason}"] += 1
            else:
                proposals.append((account, verdict))
        if not proposals:
            return
        proposals.sort(key=lambda item: (item[1].stake, item[1].ev, str(item[0].bot.id)), reverse=True)
        winner, proposal = proposals[0]
        for account, _ in proposals[1:]:
            self.decisions["MERGED"] += 1
        self._fire(winner, proposal, signal)
        for account, _ in proposals:  # one entry per selection per cooldown, for every bot that matched
            if account.bot.cooldown_seconds > 0:
                account.cooldowns[(edge.fixture_id, edge.selection)] = now + timedelta(seconds=account.bot.cooldown_seconds)

    def _fire(self, account: Account, proposal: Proposal, signal: SimulatedSignal) -> None:
        edge = signal.edge
        if self._wash(edge.fixture_id, edge.market_type, edge.selection):
            self.decisions["BLOCKED:WASH_TRADE"] += 1
            return
        breaker = self._breakers(account)
        if breaker is not None:
            self.decisions[f"BLOCKED:{breaker}"] += 1
            return
        bot = account.bot
        if bot.enable_order_slicing and proposal.stake > bot.slice_size_inr > 0:
            rng = stable_rng(self.config.seed, "twap", bot.id, edge.signal_id)
            stakes = slice_stake(proposal.stake, bot.slice_size_inr, self.settings.HIVE_SLICE_JITTER_PCT, rng)
            countdowns = slice_countdowns(len(stakes), self.settings.HIVE_SLICE_DELAY_MIN_SECONDS, self.settings.HIVE_SLICE_DELAY_MAX_SECONDS, rng)
            plan = _Plan(next(self._plan_ids), account, signal, proposal.conviction, stakes, ["SCHEDULED"] * len(stakes))
            for index, countdown in enumerate(countdowns):
                self._push(self.clock.now + timedelta(seconds=countdown), _SLICE, (plan, index))
            self.decisions["SLICED"] += 1
            return
        self.decisions["FIRED"] += 1
        self._submit(account, signal, proposal.stake, proposal.conviction, proposal.ev, None)

    def _wash(self, fixture_id: str, market: str, selection: str) -> bool:
        """Another outcome of this market already held, or already on its way: live, the ledger books
        an order (PENDING) before it is sent, so an order still in flight counts as held."""
        held = any(p.fixture_id == fixture_id and p.market == market and p.selection != selection for a in self.accounts for p in a.open.values())
        return held or any(
            o.signal.edge.fixture_id == fixture_id and o.signal.edge.market_type == market and o.signal.edge.selection != selection for o in self._inflight.values()
        )

    def _breakers(self, account: Account) -> str | None:
        now = self.clock.now
        window = timedelta(seconds=self.settings.HIVE_VELOCITY_WINDOW_SECONDS)
        while account.fires and now - account.fires[0] > window:
            account.fires.popleft()
        if len(account.fires) >= account.bot.max_bets_per_minute:
            self._suspend(account, "VELOCITY_BREAKER", orders_in_window=len(account.fires))
            return "VELOCITY_BREAKER"
        since = now - timedelta(hours=24)
        pnl = sum((p for p, at in zip(account.pnls, account.settled_times, strict=True) if at >= since), ZERO)
        if pnl < ZERO:
            start = account.equity - pnl
            drop = -pnl / start if start > ZERO else ONE
            if drop * HUNDRED > account.bot.drawdown_limit_pct:
                self._suspend(account, "DRAWDOWN_BREAKER", drop_pct=(drop * HUNDRED).quantize(PAISA))
                return "DRAWDOWN_BREAKER"
        return None

    def _suspend(self, account: Account, reason: str, **detail: Any) -> None:
        account.suspended_reason = reason
        account.suspended_until = None if self.resume_after is None else self.clock.now + self.resume_after
        self.decisions[f"SUSPENSION:{reason}"] += 1
        self._log("SUSPENDED", bot=account.bot.name, reason=reason, until=account.suspended_until.isoformat() if account.suspended_until else None, **detail)

    def _on_shock(self, shock: MarketShock) -> None:
        if self._halted(shock.at):
            return
        self.halt_reason = "FLASH_CRASH"
        self.halted_until = None if self.resume_after is None else shock.at + self.resume_after
        self.decisions["HALT:FLASH_CRASH"] += 1
        self._log("HALTED", reason="FLASH_CRASH", market=shock.cell, swing_pct=shock.swing_pct, low=shock.low, high=shock.high, points=shock.points)

    # -------------------------------------------------------------- TWAP slices
    def _on_slice(self, plan: _Plan, index: int) -> None:
        if plan.status[index] != "SCHEDULED":
            return
        now = self.clock.now
        account = plan.account

        def cancel(reason: str) -> None:
            for i in range(index, len(plan.status)):
                if plan.status[i] == "SCHEDULED":
                    plan.status[i] = "CANCELLED"
            self.decisions[f"SLICE_CANCELLED:{reason}"] += 1

        if self._halted(now):
            return cancel("HALTED")
        if not account.active(now):
            return cancel(f"BOT_{account.suspended_reason}")
        live = self.stream.live_edge(plan.signal.cell, now)
        if live is None:
            return cancel("EDGE_GONE")
        floor = order_floor(plan.signal.edge.odds, plan.conviction, live.edge.commission, account.bot.min_ev, self.config)
        if live.edge.odds < floor:
            return cancel("EDGE_GONE")
        if self._wash(live.edge.fixture_id, live.edge.market_type, live.edge.selection):
            return cancel("WASH_TRADE")
        breaker = self._breakers(account)
        if breaker is not None:
            return cancel(breaker)
        stake = min(plan.stakes[index], account.available)
        plan.status[index] = "FIRED"
        self.decisions["SLICE_FIRED"] += 1
        net = (live.edge.odds - ONE) * (ONE - live.edge.commission) + ONE
        self._submit(account, live, stake, plan.conviction, plan.conviction * net - ONE, plan.id, index)

    # -------------------------------------------------------------- orders
    def _submit(self, account: Account, signal: SimulatedSignal, stake_inr: Decimal, conviction: Decimal, ev: Decimal, plan_id: int | None, index: int = 0) -> None:
        now = self.clock.now
        edge = signal.edge
        try:
            fx = self.router.resolve(signal.currency)
        except FxUnavailableError:
            self.rejections["FX_UNAVAILABLE"] += 1
            return
        stake_ccy = from_inr(stake_inr, fx.quote)
        if stake_ccy <= ZERO:
            self.rejections["STAKE_TOO_SMALL"] += 1
            return
        cost = leg_cost_inr(stake_ccy, fx.quote)
        if cost > account.available:
            self.rejections["INSUFFICIENT_FUNDS"] += 1
            return
        wait = self.limiter.acquire(edge.bookmaker_id, now)
        if wait is None:
            self.rejections["OUTBOUND_THROTTLED"] += 1
            return
        if wait > 0:
            self.queued += 1
            self.queue_seconds += wait
        account.available -= cost
        account.exposure += cost
        account.fires.append(now)
        latency = self.latency.delay(account.bot.id, edge.signal_id, plan_id, index)
        arrives = now + timedelta(seconds=wait) + latency
        floor = order_floor(edge.odds, conviction, edge.commission, account.bot.min_ev, self.config)
        order = _Order(next(self._order_ids), account, signal, stake_ccy, cost, fx, floor, conviction, ev, now, arrives, int(latency.total_seconds() * 1000), int(wait * 1000), plan_id)
        self._inflight[order.id] = order
        account.in_flight[order.id] = (edge.fixture_id, cost)
        self._push(arrives, _ARRIVAL, order)

    def _release(self, order: _Order, amount: Decimal) -> None:
        order.account.available += amount
        order.account.exposure -= amount

    def _on_arrival(self, order: _Order) -> None:
        self._inflight.pop(order.id, None)
        order.account.in_flight.pop(order.id, None)
        now = self.clock.now
        edge = order.signal.edge
        fixture = self.store.fixtures[edge.fixture_id]
        if now >= fixture.commence_time:
            self._release(order, order.cost_inr)
            self.rejections["MARKET_STARTED"] += 1
            return
        if self.view.result(fixture.id) is not None:
            self._release(order, order.cost_inr)
            self.rejections["MARKET_CLOSED"] += 1
            return
        tick = self.view.quote(edge.fixture_id, edge.market_type, edge.bookmaker_id, edge.selection)
        quote = None if tick is None else VenueQuote(tick.odds, tick.liquidity, tick.suspended)
        if tick is not None and tick.liquidity is not None:
            pool = tick.liquidity
        else:
            pool = from_inr(self.config.unreported_liquidity_inr, order.fx.quote)
        venue_order = OrderAtVenue(edge.odds, order.floor, order.conviction, edge.commission, order.account.bot.min_ev, order.stake_ccy)
        fill = fill_at_arrival(venue_order, quote, pool, self.config)
        if not fill.filled:
            self._release(order, order.cost_inr)
            self.rejections[fill.reason] += 1
            return
        cost = order.cost_inr
        if fill.stake_ccy < order.stake_ccy:
            cost = leg_cost_inr(fill.stake_ccy, order.fx.quote)
            self._release(order, order.cost_inr - cost)
            self.decisions[f"PARTIAL:{fill.reason}"] += 1
        position = Position(
            next(self._pos_ids), order.account.bot.id, edge.fixture_id, edge.market_type, edge.selection, edge.bookmaker_id, order.signal.currency,
            fill.stake_ccy, cost, order.fx.quote.inr_per_unit, order.fx.source, edge.odds, fill.arrival_odds or edge.odds, fill.fill_odds or edge.odds,
            Decimal(edge.commission), order.conviction, order.ev, order.decided_at, now, order.latency_ms, order.queue_ms, fill.participation, fill.impact_pct,
            fill.reason, order.plan_id,
        )
        order.account.open[position.id] = position
        self.positions.append(position)
        if fill.reason == "LATENCY_SLIPPAGE":
            self.decisions["FILLED_WORSE_AFTER_LATENCY"] += 1
        if edge.fixture_id not in self._settling:
            result = self.store.results.get(edge.fixture_id)
            if result is not None:
                self._settling.add(edge.fixture_id)
                self._push(max(result.known_at, now), _RESULT, result)

    # -------------------------------------------------------------- settlement
    def _on_result(self, result: ResultInfo) -> None:
        now = self.clock.now
        known = self.view.result(result.fixture_id)
        if known is None:  # cannot happen: the event fires at the instant the result is published
            return
        void_reason = "POSTPONED" if known.postponed else ("INJECTED" if injected_void(self.config, known.fixture_id) else None)
        groups: dict[tuple[uuid.UUID, str, str], list[Position]] = defaultdict(list)
        for account in self.accounts:
            for pos in [p for p in account.open.values() if p.fixture_id == known.fixture_id]:
                if void_reason is not None:
                    account.available += pos.cost_inr
                    account.exposure -= pos.cost_inr
                    pos.status, pos.pnl_inr, pos.settled_at, pos.void_reason = "VOID", ZERO, now, void_reason
                    del account.open[pos.id]
                    self.decisions[f"VOID:{void_reason}"] += 1
                else:
                    groups[(account.bot.id, pos.bookmaker_id, pos.market)].append(pos)
        for (bot_id, _, market), positions in groups.items():
            account = self._by_bot[bot_id]
            winners = known.winners(market)
            gross = {p.id: (p.stake_ccy * (p.fill_odds - ONE) if p.selection in winners else -p.stake_ccy) for p in positions}
            net_market = sum(gross.values(), ZERO)
            rate = positions[0].commission
            commission = rate * net_market if net_market > ZERO and rate > ZERO else ZERO  # on net market winnings
            won_total = sum((g for g in gross.values() if g > ZERO), ZERO)
            for pos in positions:
                share = commission * gross[pos.id] / won_total if commission > ZERO and gross[pos.id] > ZERO else ZERO
                if pos.selection in winners:
                    fx = self._settle_rate(pos)
                    payout_ccy = pos.stake_ccy * pos.fill_odds - share
                    haircut = self.config.fx_haircut if pos.currency != HOME_CURRENCY else ZERO
                    payout_inr = payout_ccy * fx * (ONE - haircut)
                    pnl = (payout_inr - pos.cost_inr).quantize(PAISA, rounding=ROUND_DOWN)
                    pos.status, pos.commission_inr = "WON", (share * fx).quantize(PAISA)
                else:
                    pos.fx_settle, pos.fx_settle_source = pos.fx_bet, "bet"
                    pnl = -pos.cost_inr
                    pos.status = "LOST"
                account.equity += pnl
                account.available += pos.cost_inr + pnl
                account.exposure -= pos.cost_inr
                account.pnls.append(pnl)
                account.settled_times.append(now)
                pos.pnl_inr, pos.settled_at = pnl, now
                del account.open[pos.id]
            account.curve.append(float(account.equity))
            self.bot_curves[str(bot_id)].append((now, account.equity))
        self.curve.append((now, self._equity()))

    def _settle_rate(self, pos: Position) -> Decimal:
        try:
            resolution = self.router.resolve(pos.currency)
        except FxUnavailableError:
            pos.fx_settle, pos.fx_settle_source = pos.fx_bet, "bet"
            return pos.fx_bet
        pos.fx_settle, pos.fx_settle_source = resolution.quote.inr_per_unit, resolution.source
        return resolution.quote.inr_per_unit
