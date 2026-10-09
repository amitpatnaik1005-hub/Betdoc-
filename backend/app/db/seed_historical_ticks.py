"""Seed The Lab's historical market: exactly ``--rows`` synthetic odds ticks spanning a year.

    python -m app.db.seed_historical_ticks                    # 10,000 ticks from 2025-10-01, 365 days
    python -m app.db.seed_historical_ticks --replace          # drop the synthetic dataset first
    python -m app.db.seed_historical_ticks --rows 20000 --seed 7 --start 2025-08-01 --dry-run

The data is generated, and every row says so (``source = "synthetic"``). It is built to have the
structure of a real pre-match football market, so a backtest exercises every penalty the engine
models, but its prices are not anybody's prices and a strategy's result on it proves the engine,
not the strategy. How it is made:

* Truth. Each fixture's goal rates come from fixed team ratings (Poisson, home advantage); the
  1X2 and over/under 2.5 probabilities follow from them, so both markets agree. News (an injury,
  a line-up) arrives as jumps in the rates; the score is drawn from the final, closing truth.
* The market's belief is the truth plus an error that shrinks toward kick-off: the closing line is
  efficient, an early price is not. A few fixtures are postponed (their bets are void).
* Four books. ``pinnacle`` (USD) and ``betfair_ex_uk`` (GBP, an exchange charging 5% on net
  winnings, odds on Betfair's tick ladder) quote the belief tightly and react to news in seconds;
  ``williamhill`` (GBP) and ``unibet_eu`` (EUR) carry 4.5-6.5% margins with a longshot bias, noisier
  prices, and lag news by a heavy-tailed delay (median 25-40s, sometimes minutes), sometimes
  suspending first. Stale soft lines after news are the classic edge, and the classic latency trap.
* Liquidity on every tick, in the book's currency: the exchange's money at the price grows toward
  kick-off; the bookmakers' figure is their maximum stake.
* FX: daily 16:00 UTC fixings for GBP, EUR and USD on a random walk, with missing days and a
  three-week EUR outage, so the backtester's static-rate fallback is exercised.
* Rows: snapshots (one poll: every book re-quotes in the same fetch), news bursts (each book on its
  own clock) and their suspensions are generated per
  fixture; single-book re-quotes between snapshots then fill the count exactly.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_FLOOR, Decimal

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

DATASET_PREFIX = "synthetic-v1"
SOURCE = "synthetic"
DEFAULT_ROWS = 10_000
DEFAULT_START = date(2025, 10, 1)
DEFAULT_DAYS = 365
MATCH_ODDS, TOTALS = "Match Odds", "Over/Under 2.5"
LABELS = {MATCH_ODDS: ("HOME", "DRAW", "AWAY"), TOTALS: ("OVER", "UNDER")}
SNAPSHOT_HOURS = (168.0, 96.0, 48.0, 24.0, 12.0, 6.0, 3.0, 1.0, 0.25)  # opening ... closing (15 min before kick-off)
POSTPONED_RATE = 0.015
_MAX_GOALS = 12


# ---------------------------------------------------------------- the world
@dataclass(frozen=True, slots=True)
class BookSpec:
    bookmaker_id: str
    currency: str
    sharp: bool
    margin: tuple[float, float]  # booksum range
    noise: float  # per-quote noise on each probability (log scale)
    reaction: tuple[float, float]  # sharp: uniform seconds; soft: (median seconds, log sigma) of a lognormal
    ladder: str  # betfair | cents | bookmaker
    liquidity_open: float  # in the book's currency
    liquidity_close: float
    suspends: float = 0.0  # chance the book suspends before re-quoting a news move


BOOKS: tuple[BookSpec, ...] = (
    BookSpec("pinnacle", "USD", True, (1.020, 1.030), 0.004, (0.4, 2.0), "cents", 3_000, 25_000),
    BookSpec("betfair_ex_uk", "GBP", True, (1.004, 1.014), 0.006, (0.8, 3.5), "betfair", 1_500, 90_000),
    BookSpec("williamhill", "GBP", False, (1.050, 1.065), 0.020, (25.0, 1.1), "bookmaker", 400, 2_500, 0.30),
    BookSpec("unibet_eu", "EUR", False, (1.045, 1.060), 0.018, (40.0, 1.0), "bookmaker", 500, 3_000, 0.25),
)
LEAGUES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "epl": (
        "soccer_epl", "Premier League",
        ("Arsenal", "Aston Villa", "Bournemouth", "Brentford", "Brighton", "Burnley", "Chelsea", "Crystal Palace", "Everton", "Fulham",
         "Leeds", "Liverpool", "Manchester City", "Manchester United", "Newcastle", "Nottingham Forest", "Sunderland", "Tottenham", "West Ham", "Wolves"),
    ),
    "laliga": (
        "soccer_spain_la_liga", "La Liga",
        ("Alaves", "Athletic Bilbao", "Atletico Madrid", "Barcelona", "Celta Vigo", "Elche", "Espanyol", "Getafe", "Girona", "Levante",
         "Mallorca", "Osasuna", "Rayo Vallecano", "Real Betis", "Real Madrid", "Real Oviedo", "Real Sociedad", "Sevilla", "Valencia", "Villarreal"),
    ),
}
FX_BASE = {"GBP": 108.0, "EUR": 92.5, "USD": 84.0}


@dataclass(slots=True)
class Tick:
    fixture_id: str
    market: str
    selection: str
    bookmaker_id: str
    odds: Decimal
    liquidity: Decimal | None
    currency: str
    is_suspended: bool
    created_at: datetime

    def row(self) -> dict[str, object]:
        return {
            "fixture_id": self.fixture_id, "market": self.market, "selection": self.selection, "bookmaker_id": self.bookmaker_id, "odds": self.odds,
            "liquidity": self.liquidity, "currency": self.currency, "is_suspended": self.is_suspended, "source": SOURCE, "created_at": self.created_at,
        }


@dataclass(slots=True)
class Fixture:
    id: str
    sport_key: str
    league: str
    home_team: str
    away_team: str
    commence_time: datetime
    listed_at: datetime
    markets: tuple[str, ...]
    status: str = "FINISHED"
    home_goals: int | None = None
    away_goals: int | None = None
    result_at: datetime | None = None
    ticks: list[Tick] = field(default_factory=list)
    snapshots: list[list[Tick]] = field(default_factory=list)  # removable batches (not the opening or closing one)


@dataclass(slots=True)
class FxFixing:
    currency: str
    inr_per_unit: Decimal
    created_at: datetime


@dataclass(slots=True)
class Dataset:
    name: str
    seed: int
    fixtures: list[Fixture]
    ticks: list[Tick]
    fx: list[FxFixing]

    @property
    def span(self) -> tuple[datetime, datetime]:
        return self.ticks[0].created_at, self.ticks[-1].created_at


# ---------------------------------------------------------------- maths
def _poisson(lam: float) -> list[float]:
    p = [math.exp(-lam)]
    for k in range(1, _MAX_GOALS + 1):
        p.append(p[-1] * lam / k)
    return p


def outcome_probabilities(lam_home: float, lam_away: float) -> dict[str, float]:
    """1X2 and over/under 2.5 from independent Poisson goal counts (truncated at 12 goals, renormalised)."""
    ph, pa = _poisson(lam_home), _poisson(lam_away)
    home = draw = away = under = 0.0
    for i, a in enumerate(ph):
        for j, b in enumerate(pa):
            joint = a * b
            if i > j:
                home += joint
            elif i == j:
                draw += joint
            else:
                away += joint
            if i + j <= 2:
                under += joint
    total = home + draw + away
    return {"HOME": home / total, "DRAW": draw / total, "AWAY": away / total, "OVER": 1 - under / total, "UNDER": under / total}


def _power_margin(probs: Sequence[float], booksum: float) -> list[float]:
    """Implied probabilities ``p ** k`` summing to ``booksum`` (k < 1): the margin lands mostly on
    the longshots, the favourite-longshot bias real books show."""
    lo, hi = 0.3, 1.0
    for _ in range(60):
        k = (lo + hi) / 2
        if sum(p**k for p in probs) > booksum:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return [p**k for p in probs]


_BETFAIR_LADDER = ((2, "0.01"), (3, "0.02"), (4, "0.05"), (6, "0.1"), (10, "0.2"), (20, "0.5"), (30, "1"), (50, "2"), (100, "5"), (1000, "10"))


def round_odds(odds: float, ladder: str) -> Decimal:
    """Down to the book's price grid: a quoted price is never better than the fair one implies."""
    value = Decimal(repr(max(odds, 1.01)))
    if ladder == "betfair":
        for ceiling, step in _BETFAIR_LADDER:
            if value < ceiling:
                increment = Decimal(step)
                return max(Decimal("1.01"), (value / increment).to_integral_value(rounding=ROUND_FLOOR) * increment).quantize(Decimal("0.01"))
        return Decimal(1000)
    if ladder == "cents":
        return max(Decimal("1.01"), value.quantize(Decimal("0.001"), rounding=ROUND_DOWN))
    step = Decimal("0.01") if value < 4 else Decimal("0.05") if value < 10 else Decimal("0.25")
    return max(Decimal("1.01"), ((value / step).to_integral_value(rounding=ROUND_FLOOR) * step).quantize(Decimal("0.01")))


def _money(value: float) -> Decimal:
    return Decimal(repr(max(value, 0.0))).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


# ---------------------------------------------------------------- one fixture
class _Market:
    """A fixture's latent state: the truth (goal-rate offsets that news moves) and the market's
    belief about it (truth plus an error that shrinks toward kick-off)."""

    def __init__(self, rng: random.Random, lam_home: float, lam_away: float) -> None:
        self.rng = rng
        self.lam = (lam_home, lam_away)
        self.truth = [0.0, 0.0]  # strength shift s (home up, away down), total-goals shift u
        self.error = [rng.gauss(0, 0.16), rng.gauss(0, 0.10)]

    def drift(self, hours_left: float) -> None:
        """The belief error reverts toward a floor that shrinks as kick-off nears (an efficient close)."""
        scale = 0.02 + 0.14 * min(hours_left / 168.0, 1.0)
        for i in range(2):
            self.error[i] = 0.55 * self.error[i] + self.rng.gauss(0, scale * 0.6)

    def news(self) -> None:
        jump = (self.rng.gauss(0, 0.075), self.rng.gauss(0, 0.045))
        self.truth[0] += jump[0]
        self.truth[1] += jump[1]

    def rates(self, offsets: Sequence[float]) -> tuple[float, float]:
        s, u = offsets
        return self.lam[0] * math.exp(s + u), self.lam[1] * math.exp(-s + u)

    def belief(self) -> dict[str, float]:
        return outcome_probabilities(*self.rates([self.truth[i] + self.error[i] for i in range(2)]))

    def final_truth(self) -> tuple[float, float]:
        return self.rates(self.truth)


def _quote(rng: random.Random, book: BookSpec, market: str, belief: dict[str, float], hours_left: float) -> list[tuple[str, Decimal, Decimal]]:
    labels = LABELS[market]
    noisy = [belief[label] * math.exp(rng.gauss(0, book.noise)) for label in labels]
    total = sum(noisy)
    fair = [p / total for p in noisy]
    implied = _power_margin(fair, rng.uniform(*book.margin))
    closeness = math.exp(-max(hours_left, 0.0) / 40.0)  # 0 a week out, 1 at kick-off
    out = []
    for label, p, q in zip(labels, fair, implied, strict=True):
        odds = round_odds(1.0 / q, book.ladder)
        pool = book.liquidity_open + (book.liquidity_close - book.liquidity_open) * closeness
        if book.ladder == "betfair":
            pool *= 0.35 + p  # more money waits behind the favourite
        else:
            pool *= min(1.0, 0.45 + p)  # bookmakers cap longshot stakes harder
        out.append((label, odds, _money(pool * rng.uniform(0.75, 1.25))))
    return out


def _lag(rng: random.Random, book: BookSpec) -> float:
    if book.sharp:
        return rng.uniform(*book.reaction)
    median, sigma = book.reaction
    return min(max(math.exp(math.log(median) + rng.gauss(0, sigma)), 0.8), 900.0)


def _requote(fx: Fixture, rng: random.Random, state: _Market, book: BookSpec, at: datetime, markets: Sequence[str]) -> list[Tick]:
    hours_left = (fx.commence_time - at).total_seconds() / 3600
    belief = state.belief()
    ticks: list[Tick] = []
    for market in markets:
        for label, odds, pool in _quote(rng, book, market, belief, hours_left):
            ticks.append(Tick(fx.id, market, label, book.bookmaker_id, odds, pool, book.currency, False, at))
    return ticks


def _fixture(rng: random.Random, fx: Fixture, ratings: dict[str, float]) -> None:
    lam_home = math.exp(0.27 + ratings[fx.home_team] - ratings[fx.away_team])
    lam_away = math.exp(0.06 + ratings[fx.away_team] - ratings[fx.home_team])
    state = _Market(rng, lam_home, lam_away)
    inner = list(SNAPSHOT_HOURS[1:-1])
    chosen = sorted(rng.sample(inner, rng.randint(2, 4)), reverse=True)
    hours = [SNAPSHOT_HOURS[0], *chosen, SNAPSHOT_HOURS[-1]]
    news_count = min(len(hours) - 1, sum(1 for _ in range(4) if rng.random() < 0.22))
    news_gaps = set(rng.sample(range(1, len(hours)), news_count)) if news_count else set()

    previous: float | None = None
    for gap, h in enumerate(hours):
        at = fx.commence_time - timedelta(hours=h)
        if previous is not None and gap in news_gaps:
            # News lands between two snapshots: the truth jumps, sharp books move in seconds, soft books lag
            news_at = at - timedelta(hours=(previous - h) * rng.uniform(0.25, 0.85))
            state.drift((fx.commence_time - news_at).total_seconds() / 3600)
            state.news()
            for book in BOOKS:
                if not book.sharp and rng.random() < book.suspends:
                    pause = news_at + timedelta(seconds=rng.uniform(0.5, 2.5))
                    last = {t.market + t.selection: t for t in fx.ticks if t.bookmaker_id == book.bookmaker_id}
                    for market in fx.markets:
                        for label in LABELS[market]:
                            prior = last[market + label]
                            fx.ticks.append(Tick(fx.id, market, label, book.bookmaker_id, prior.odds, prior.liquidity, book.currency, True, pause))
                requote_at = news_at + timedelta(seconds=_lag(rng, book))
                if requote_at < at:
                    fx.ticks += _requote(fx, rng, state, book, requote_at, fx.markets)
        state.drift(h)
        batch: list[Tick] = []
        polled = at + timedelta(milliseconds=rng.randint(0, 999))
        for book in BOOKS:  # one poll: every book's market arrives in the same fetch, stamped with its time
            batch += _requote(fx, rng, state, book, polled, fx.markets)
        fx.ticks += batch
        if 0 < gap < len(hours) - 1:
            fx.snapshots.append(batch)
        previous = h

    lam_h, lam_a = state.final_truth()
    if rng.random() < POSTPONED_RATE:
        fx.status, fx.result_at = "POSTPONED", fx.commence_time - timedelta(hours=rng.uniform(0.5, 6))
    else:
        fx.home_goals, fx.away_goals = _draw_poisson(rng, lam_h), _draw_poisson(rng, lam_a)
        fx.result_at = fx.commence_time + timedelta(minutes=rng.uniform(110, 125))


def _draw_poisson(rng: random.Random, lam: float) -> int:
    threshold, k, product = math.exp(-lam), 0, rng.random()
    while product > threshold:
        k += 1
        product *= rng.random()
    return k


# ---------------------------------------------------------------- the dataset
def _kickoffs(rng: random.Random, n: int, start: datetime, days: int) -> list[datetime]:
    first = start + timedelta(hours=SNAPSHOT_HOURS[0])
    last = start + timedelta(days=days, hours=SNAPSHOT_HOURS[-1])
    slots = (time(11, 30), time(14, 0), time(16, 30), time(19, 0), time(19, 45))
    span_days = max((last - first).days - 1, 1)
    middle = []
    for _ in range(max(n - 2, 0)):
        day = (first + timedelta(days=rng.randint(1, span_days))).date()
        middle.append(datetime.combine(day, rng.choice(slots), tzinfo=UTC))
    return [first, *sorted(middle), last]


def _fx_fixings(rng: random.Random, start: datetime, days: int) -> list[FxFixing]:
    fixings: list[FxFixing] = []
    lo = max(1, min(60, days // 3))
    outage_start = start + timedelta(days=rng.randint(lo, max(lo, days - max(21, days // 3))))
    level = dict(FX_BASE)
    for d in range(-8, days + 3):
        at = datetime.combine((start + timedelta(days=d)).date(), time(16, 0), tzinfo=UTC)
        for ccy in FX_BASE:
            level[ccy] *= math.exp(rng.gauss(0, 0.0042))
            if rng.random() < 0.10:  # a missed fixing
                continue
            if ccy == "EUR" and outage_start <= at < outage_start + timedelta(days=21):  # the feed's outage
                continue
            fixings.append(FxFixing(ccy, Decimal(repr(level[ccy])).quantize(Decimal("0.000001")), at))
    return fixings


def generate_dataset(rows: int = DEFAULT_ROWS, *, seed: int = 66, start: date = DEFAULT_START, days: int = DEFAULT_DAYS) -> Dataset:
    """Exactly ``rows`` ticks, deterministic for a seed, the first at ``start`` 00:00 UTC and the
    last ``days`` later."""
    if rows < 500:
        raise ValueError("at least 500 rows: two fixtures' opening and closing markets alone take ~100")
    if days < 30:
        raise ValueError("at least 30 days")
    rng = random.Random(seed)
    origin = datetime.combine(start, time(0, 0), tzinfo=UTC)
    ratings = {team: rng.gauss(0, 0.25) for _, _, teams in LEAGUES.values() for team in teams}
    estimate = 92.0  # rows per fixture, measured on the generator
    n = max(2, round(rows * 0.86 / estimate))
    fixtures: list[Fixture] = []
    taken: set[str] = set()
    for i, kickoff in enumerate(_kickoffs(rng, n, origin, days)):
        code = rng.choice(tuple(LEAGUES))
        sport_key, league, teams = LEAGUES[code]
        home, away = rng.sample(teams, 2)
        fid = f"lab-{code}-{kickoff:%Y%m%d}-{home[:3].lower()}-{away[:3].lower()}"
        if fid in taken:
            fid = f"{fid}-{i}"
        taken.add(fid)
        markets = (MATCH_ODDS, TOTALS) if code == "epl" or rng.random() < 0.3 else (MATCH_ODDS,)
        listed = kickoff - timedelta(hours=SNAPSHOT_HOURS[0], minutes=5)
        fx = Fixture(fid, sport_key, league, home, away, kickoff, listed, markets)
        _fixture(rng, fx, ratings)
        fixtures.append(fx)

    # The first fixture's opening snapshot is the dataset's first instant
    for tick in fixtures[0].ticks:
        if tick.created_at < origin + timedelta(seconds=2):
            tick.created_at = origin
    base = sum(len(f.ticks) for f in fixtures)
    while base > rows:  # trim whole inner snapshots, never an opening or a closing market
        candidates = [f for f in fixtures if f.snapshots]
        if not candidates:
            raise ValueError("too few rows for this many fixtures")
        victim = rng.choice(candidates)
        batch = victim.snapshots.pop(rng.randrange(len(victim.snapshots)))
        dropped = {id(t) for t in batch}
        victim.ticks = [t for t in victim.ticks if id(t) not in dropped]
        base -= len(batch)
    _fill(rng, fixtures, rows - base)

    ticks = sorted((t for f in fixtures for t in f.ticks), key=lambda t: (t.created_at, t.fixture_id, t.market, t.bookmaker_id, t.selection))
    assert len(ticks) == rows
    return Dataset(f"{DATASET_PREFIX}-s{seed}", seed, fixtures, ticks, _fx_fixings(rng, origin, days))


def _fill(rng: random.Random, fixtures: Sequence[Fixture], remaining: int) -> None:
    """Single-book re-quotes between a fixture's first and last tick, each nudging that book's own
    last price by up to 1.5%: a full market re-quote while three rows or more are missing, then an
    exchange runner moving on its own (one row) for the rest."""
    while remaining > 0:
        fx = rng.choice(fixtures)
        opened, closed = fx.ticks[0].created_at, max(t.created_at for t in fx.ticks if not t.is_suspended)
        at = opened + timedelta(seconds=rng.uniform(60, max((closed - opened).total_seconds() - 60, 61)))
        latest: dict[tuple[str, str, str], Tick] = {}
        for t in fx.ticks:
            if t.created_at <= at:
                latest[(t.bookmaker_id, t.market, t.selection)] = t
        if remaining >= 3:
            book = rng.choice(BOOKS)
            market = rng.choice(fx.markets)
            if len(LABELS[market]) > remaining:
                market = MATCH_ODDS if remaining >= 3 else TOTALS
            selections = LABELS[market]
        else:
            book, market = BOOKS[1], rng.choice(fx.markets)
            selections = (rng.choice(LABELS[market]),)
        for label in selections:
            prior = latest.get((book.bookmaker_id, market, label))
            if prior is None:
                break
            nudged = round_odds(float(prior.odds) * math.exp(rng.gauss(0, 0.008)), book.ladder)
            fx.ticks.append(Tick(fx.id, market, label, book.bookmaker_id, nudged, prior.liquidity, book.currency, False, at))
            remaining -= 1
            if remaining == 0:
                break
    for fx in fixtures:
        fx.ticks.sort(key=lambda t: t.created_at)


# ---------------------------------------------------------------- the database
async def seed_dataset(session: AsyncSession, dataset: Dataset, *, replace: bool = False) -> dict[str, int]:
    """Insert the dataset (the caller commits). Refuses to mix with an existing synthetic dataset
    unless ``replace``; never touches rows from any other source."""
    from app.models.lab_quant import LabFixture, LabFixtureResult, LabFxRate, LabOddsTick  # noqa: PLC0415 - the CLI imports models lazily

    existing = await session.scalar(select(func.count()).select_from(LabOddsTick).where(LabOddsTick.source == SOURCE))
    if existing and not replace:
        raise RuntimeError(f"{existing} synthetic ticks already loaded; pass replace=True (--replace) to rebuild them")
    if replace:
        await session.execute(delete(LabOddsTick).where(LabOddsTick.source == SOURCE))
        await session.execute(delete(LabFixtureResult).where(LabFixtureResult.source == SOURCE))
        await session.execute(delete(LabFixture).where(LabFixture.source == SOURCE))
        await session.execute(delete(LabFxRate).where(LabFxRate.source == SOURCE))
    await session.execute(
        insert(LabFixture),
        [
            {"id": f.id, "dataset": dataset.name, "sport_key": f.sport_key, "league": f.league, "home_team": f.home_team, "away_team": f.away_team,
             "commence_time": f.commence_time, "source": SOURCE, "created_at": f.listed_at}
            for f in dataset.fixtures
        ],
    )
    await session.execute(
        insert(LabFixtureResult),
        [
            {"fixture_id": f.id, "status": f.status, "home_goals": f.home_goals, "away_goals": f.away_goals, "source": SOURCE, "created_at": f.result_at}
            for f in dataset.fixtures
        ],
    )
    for chunk in _chunks([t.row() for t in dataset.ticks], 2_000):
        await session.execute(insert(LabOddsTick), chunk)
    for chunk in _chunks([{"currency": x.currency, "inr_per_unit": x.inr_per_unit, "source": SOURCE, "created_at": x.created_at} for x in dataset.fx], 2_000):
        await session.execute(insert(LabFxRate), chunk)
    return {"fixtures": len(dataset.fixtures), "ticks": len(dataset.ticks), "fx_fixings": len(dataset.fx)}


def _chunks(items: list[dict[str, object]], size: int) -> Iterator[list[dict[str, object]]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def describe(dataset: Dataset) -> str:
    first, last = dataset.span
    by_book: dict[str, int] = {}
    for t in dataset.ticks:
        by_book[t.bookmaker_id] = by_book.get(t.bookmaker_id, 0) + 1
    postponed = sum(1 for f in dataset.fixtures if f.status == "POSTPONED")
    suspended = sum(1 for t in dataset.ticks if t.is_suspended)
    return (
        f"{dataset.name}: {len(dataset.ticks)} ticks, {len(dataset.fixtures)} fixtures ({postponed} postponed), "
        f"{(last - first).days} days ({first:%Y-%m-%d} -> {last:%Y-%m-%d}), {suspended} suspension ticks, "
        f"{len(dataset.fx)} FX fixings; by book: " + ", ".join(f"{k} {v}" for k, v in sorted(by_book.items()))
    )


async def _main(args: argparse.Namespace) -> int:
    dataset = generate_dataset(args.rows, seed=args.seed, start=args.start, days=args.days)
    print(describe(dataset))
    if args.dry_run:
        return 0
    from app.core.database import AsyncSessionLocal  # noqa: PLC0415 - only the CLI needs the configured database

    async with AsyncSessionLocal() as session:
        counts = await seed_dataset(session, dataset, replace=args.replace)
        await session.commit()
    print("seeded:", counts)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--seed", type=int, default=66)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--replace", action="store_true", help="drop the existing synthetic dataset first")
    parser.add_argument("--dry-run", action="store_true", help="generate and describe, write nothing")
    try:
        return asyncio.run(_main(parser.parse_args(argv)))
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BOOKS", "Dataset", "DATASET_PREFIX", "SOURCE", "describe", "generate_dataset", "outcome_probabilities", "round_odds", "seed_dataset"]
