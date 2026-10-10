"""Reality penalties: what stands between a backtested decision and a backtested fill.

* Network latency. Every order arrives ``U(1500, 3000)`` ms after the decision (plus any queueing),
  and is priced at the book's quote at arrival, never at the decision's. A price that moved up is
  not credited (no positive slippage); a price that moved down fills there, unless the edge, net of
  commission, is now under the bot's minimum EV (``LATENCY_EDGE_DECAY``) or the price is under the
  slippage tolerance (``SLIPPAGE_REJECTED``). A suspended book refuses the order.
* The Group 63 outbound rate limit: a token bucket per venue (2 bets/s, burst 2). An order queues
  for a token up to 3 s; past that it is refused un-sent (``OUTBOUND_THROTTLED``).
* Liquidity and market impact. An order takes at most the money at the price. Quadratic (the default):
  past 5% of it, the price walks: the net-of-one price is multiplied by ``1 - k * (x - 0.05)^2`` for a
  participation ``x`` (continuous at the threshold, quadratic past it). Square-root (Group 77,
  ``impact_model="sqrt"``): ``1 - k * sqrt(x)`` from the first rupee, the concave law of market impact. The order's price floor still
  binds, so a large order fills only the part that keeps its average price above the floor.
* Simulated slippage: a flat haircut on every fill's net-of-one price, the execution cost the tape
  cannot show (spread, queue position). A cost, not a refusal.
* Survivor bias. A fixture can be void (postponed): in the data, and injected at random by the
  backtest so the bankroll must handle refunds; a void bet returns its stake and nothing else.

Every random draw is a pure function of the run's seed and the event it belongs to (``stable_rng``),
never of the order things happen in: two runs of a sweep see the same latencies and the same voids,
so their difference is the parameter, not the dice.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from app.domain.backtesting.engine_math import sqrt_impact_multiplier, sqrt_max_participation
from app.services.aryabhata_engine import break_even_odds, net_odds

ZERO, ONE, CENT = Decimal(0), Decimal(1), Decimal("0.01")
ODDS_QUANTUM = Decimal("0.0001")


def stable_rng(seed: int, *parts: object) -> random.Random:
    digest = hashlib.blake2b("|".join(map(str, (seed, *parts))).encode(), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


@dataclass(frozen=True, slots=True)
class RealityConfig:
    latency_ms: tuple[int, int] = (1500, 3000)
    bets_per_second: float = 2.0  # the Group 63 per-venue outbound limit
    burst: int = 2
    max_queue_seconds: float = 3.0  # SNIPER_RATE_MAX_WAIT_SECONDS: past this the order is refused un-sent
    slippage_pct: Decimal = Decimal("0.25")  # simulated execution cost on every fill
    max_slippage_pct: Decimal = Decimal("0.50")  # the order's tolerance below its asked price (G62 default)
    impact_threshold: Decimal = Decimal("0.05")  # participation past which the price walks
    impact_coefficient: Decimal = Decimal("2")
    impact_model: str = "quadratic"  # quadratic | sqrt (Group 77)
    void_rate: float = 0.02  # injected postponements, on top of the ones in the data
    unreported_liquidity_inr: Decimal = Decimal("25000")  # the money assumed at a price no source reports
    fx_haircut: Decimal = Decimal("0.005")  # off every foreign payout coming home
    seed: int = 66

    def __post_init__(self) -> None:
        lo, hi = self.latency_ms
        if not 0 <= lo <= hi <= 60_000:
            raise ValueError("latency must be 0 <= min <= max <= 60000 ms")
        if self.bets_per_second <= 0 or self.burst < 1 or self.max_queue_seconds < 0:
            raise ValueError("the rate limit needs a positive rate, a burst of at least 1 and a non-negative queue")
        if not ZERO <= self.slippage_pct < Decimal(100) or not ZERO <= self.max_slippage_pct <= Decimal(100):
            raise ValueError("slippage is a percentage")
        if not ZERO <= self.impact_threshold < ONE or self.impact_coefficient < 0:
            raise ValueError("impact needs a threshold in [0, 1) and a non-negative coefficient")
        if not 0 <= self.void_rate <= 1:
            raise ValueError("the void rate is a probability")
        if self.impact_model not in ("quadratic", "sqrt"):
            raise ValueError("the impact model is quadratic or sqrt")


# ---------------------------------------------------------------- latency and the rate limit
class LatencyModel:
    def __init__(self, config: RealityConfig) -> None:
        self.config = config

    def delay(self, *key: object) -> timedelta:
        lo, hi = self.config.latency_ms
        return timedelta(milliseconds=stable_rng(self.config.seed, "latency", *key).uniform(lo, hi))


@dataclass(slots=True)
class _Bucket:
    tokens: float
    at: datetime


class OutboundRateLimiter:
    """The Group 63 token bucket, in simulated time: a token per ``1/rate`` seconds up to ``burst``.
    ``acquire`` returns the wait for a token (0: one is free now), or None when the wait would pass
    the queue limit (the order is refused and takes no token)."""

    def __init__(self, config: RealityConfig) -> None:
        self.rate, self.burst, self.max_wait = config.bets_per_second, float(config.burst), config.max_queue_seconds
        self._buckets: dict[str, _Bucket] = {}

    def acquire(self, venue: str, at: datetime) -> float | None:
        bucket = self._buckets.get(venue)
        if bucket is None:
            bucket = self._buckets[venue] = _Bucket(self.burst, at)
        elapsed = max((at - bucket.at).total_seconds(), 0.0)
        tokens = min(self.burst, bucket.tokens + elapsed * self.rate)
        if tokens >= 1:
            bucket.tokens, bucket.at = tokens - 1, at
            return 0.0
        wait = (1 - tokens) / self.rate
        if wait > self.max_wait + 1e-9:
            bucket.tokens, bucket.at = tokens, at
            return None
        bucket.tokens, bucket.at = tokens - 1, at  # the token is spoken for: the next order queues behind it
        return wait


# ---------------------------------------------------------------- price impact
def impact_multiplier(participation: Decimal, config: RealityConfig) -> Decimal:
    """What is left of the net-of-one price after taking ``participation`` of the money at it."""
    if config.impact_model == "sqrt":
        return sqrt_impact_multiplier(participation, config.impact_coefficient)
    excess = participation - config.impact_threshold
    if excess <= 0:
        return ONE
    return max(ZERO, ONE - config.impact_coefficient * excess * excess)


def max_participation(price: Decimal, floor: Decimal, config: RealityConfig) -> Decimal:
    """The largest share of the money at ``price`` an order can take while its average price, after
    impact, stays at or above ``floor``."""
    if config.impact_model == "sqrt":
        return sqrt_max_participation(price, floor, config.impact_coefficient)
    if price <= floor:
        return config.impact_threshold if price == floor else ZERO
    if config.impact_coefficient == 0:
        return ONE
    room = ONE - (floor - ONE) / (price - ONE)  # the share of the net price the order may give up
    return config.impact_threshold + Decimal(math.sqrt(float(room / config.impact_coefficient))).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


# ---------------------------------------------------------------- one order meets the market
@dataclass(frozen=True, slots=True)
class OrderAtVenue:
    requested_odds: Decimal
    floor_odds: Decimal  # the slippage tolerance below the asked price, never under the bot's minimum EV
    conviction: Decimal
    commission: Decimal
    min_ev: Decimal
    stake_ccy: Decimal
    min_stake_ccy: Decimal = CENT


@dataclass(frozen=True, slots=True)
class VenueQuote:
    odds: Decimal
    liquidity_ccy: Decimal | None
    suspended: bool


@dataclass(frozen=True, slots=True)
class FillResult:
    status: str  # FILLED | PARTIAL | REJECTED
    reason: str
    fill_odds: Decimal | None = None
    stake_ccy: Decimal = ZERO
    arrival_odds: Decimal | None = None
    participation: Decimal = ZERO
    impact_pct: Decimal = ZERO  # of the net-of-one price
    slippage_pct: Decimal = ZERO
    detail: dict[str, str] = field(default_factory=dict)

    @property
    def filled(self) -> bool:
        return self.status != "REJECTED"


def order_floor(requested: Decimal, conviction: Decimal, commission: Decimal, min_ev: Decimal, config: RealityConfig) -> Decimal:
    floor = requested * (ONE - config.max_slippage_pct / Decimal(100))
    even = break_even_odds(conviction, commission, min_ev)
    if even is not None:
        floor = max(floor, even)
    return min(requested, floor.quantize(ODDS_QUANTUM))


def fill_at_arrival(order: OrderAtVenue, quote: VenueQuote | None, liquidity_ccy: Decimal, config: RealityConfig) -> FillResult:
    """The order as the venue sees it when it lands. ``liquidity_ccy`` is the money at the price in
    the venue's currency (the quote's own figure, or the run's assumption when none is reported)."""
    if quote is None:
        return FillResult("REJECTED", "NO_PRICE")
    if quote.suspended:
        return FillResult("REJECTED", "MARKET_SUSPENDED", arrival_odds=quote.odds)
    arrival = quote.odds
    price = min(order.requested_odds, arrival)  # a better price is not credited
    net = net_odds(price, order.commission)
    ev = None if net is None else order.conviction * net - ONE
    moved = price < order.requested_odds
    if ev is None or ev < order.min_ev:
        reason = "LATENCY_EDGE_DECAY" if moved else "EDGE_BELOW_MINIMUM"
        return FillResult("REJECTED", reason, arrival_odds=arrival, detail={"ev_at_arrival": str(ev.quantize(ODDS_QUANTUM)) if ev is not None else "none", "min_ev": str(order.min_ev)})
    if price < order.floor_odds:
        return FillResult("REJECTED", "SLIPPAGE_REJECTED", arrival_odds=arrival, detail={"floor": str(order.floor_odds)})

    stake = order.stake_ccy
    pool = liquidity_ccy if liquidity_ccy > 0 else ZERO
    status, reason = "FILLED", "LATENCY_SLIPPAGE" if moved else "FILLED"
    if pool <= 0:
        return FillResult("REJECTED", "NO_LIQUIDITY", arrival_odds=arrival)
    if stake > pool:
        stake, status, reason = pool.quantize(CENT, rounding=ROUND_DOWN), "PARTIAL", "LIQUIDITY_CAP"
    participation = stake / pool
    multiplier = impact_multiplier(participation, config)
    impacted = ONE + (price - ONE) * multiplier
    if impacted < order.floor_odds:
        share = max_participation(price, order.floor_odds, config)
        stake = min(stake, (share * pool).quantize(CENT, rounding=ROUND_DOWN))
        if stake < order.min_stake_ccy:
            return FillResult("REJECTED", "IMPACT_BELOW_FLOOR", arrival_odds=arrival, participation=participation, detail={"floor": str(order.floor_odds)})
        status, reason = "PARTIAL", "IMPACT_PARTIAL"
        participation = stake / pool
        multiplier = impact_multiplier(participation, config)
        impacted = ONE + (price - ONE) * multiplier
    slip = config.slippage_pct / Decimal(100)
    fill = (ONE + (impacted - ONE) * (ONE - slip)).quantize(ODDS_QUANTUM, rounding=ROUND_DOWN)
    if fill <= ONE:
        return FillResult("REJECTED", "NO_PRICE_LEFT", arrival_odds=arrival)
    return FillResult(
        status, reason, fill, stake, arrival, participation.quantize(Decimal("0.000001")), ((ONE - multiplier) * 100).quantize(Decimal("0.0001")), config.slippage_pct
    )


def injected_void(config: RealityConfig, fixture_id: str) -> bool:
    return stable_rng(config.seed, "void", fixture_id).random() < config.void_rate
