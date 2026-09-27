import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.domain.market_signals.vig_calculator import remove_vig_power_method
from app.schemas.market_signals import (
    MAX_FUTURE_SKEW,
    ArbitrageOpportunity,
    LineShopResult,
    MarketSurebet,
    OddsTick,
    OddsType,
    SurebetLeg,
)

signals_log = logging.getLogger("betdoc.signals")

MarketKey = tuple[str, str, float | None]  # (match_id, market_type, line)

# Known market sizes: surebets are only valid if EVERY outcome is covered.
DEFAULT_REQUIRED_SELECTIONS: dict[str, int] = {
    "MATCH_WINNER_1X2": 3,
    "DRAW_NO_BET": 2,
    "BTTS": 2,
    "ASIAN_HANDICAP": 2,
    "ASIAN_OVER_UNDER": 2,
    "OVER_UNDER_GOALS": 2,
}


def _market_key(t: OddsTick) -> MarketKey:
    return (t.match_id, t.market_type, t.line)


def _as_of(as_of: datetime | None) -> datetime:
    if as_of is None:
        return datetime.now(timezone.utc)
    return as_of.replace(tzinfo=timezone.utc) if as_of.tzinfo is None else as_of.astimezone(timezone.utc)


def _latest_fresh(
    ticks: list[OddsTick], odds_type: OddsType, as_of: datetime, max_staleness_minutes: int
) -> list[OddsTick]:
    """Latest tick per (bookmaker, market, selection) within the staleness window."""
    cutoff = as_of - timedelta(minutes=max(0, max_staleness_minutes))
    horizon = as_of + MAX_FUTURE_SKEW
    latest: dict[tuple, OddsTick] = {}
    for t in ticks:
        if t.odds_type != odds_type or t.timestamp < cutoff or t.timestamp > horizon:
            continue
        ident = (t.bookmaker_id, t.match_id, t.market_type, t.line, t.selection_id)
        current = latest.get(ident)
        if current is None or t.timestamp > current.timestamp:
            latest[ident] = t
    return list(latest.values())


def calculate_true_odds(
    market_ticks: list[OddsTick], max_staleness_minutes: int, as_of: datetime | None = None
) -> dict[str, float]:
    """Sharp-book consensus fair odds for ONE market (match, market_type, line)."""
    now = _as_of(as_of)
    fresh = _latest_fresh(market_ticks, OddsType.BACK, now, max_staleness_minutes)
    if not fresh:
        return {}
    if len({_market_key(t) for t in fresh}) > 1:
        raise ValueError("calculate_true_odds expects ticks from a single (match, market_type, line) market")

    universe = {t.selection_id for t in fresh}
    if len(universe) < 2:
        return {}
    selections = sorted(universe)

    sharp_quotes: dict[str, dict[str, float]] = defaultdict(dict)
    for t in fresh:
        if t.is_sharp:
            sharp_quotes[t.bookmaker_id][t.selection_id] = t.decimal_odds

    prob_sums = {s: 0.0 for s in selections}
    books_used = 0
    for book, quotes in sharp_quotes.items():
        if set(quotes) != universe:
            continue  # partial market: cannot de-vig
        odds = [quotes[s] for s in selections]
        if any(o <= 1.0 for o in odds):
            continue
        if sum(1.0 / o for o in odds) <= 1.0:
            signals_log.info("Excluding %s from consensus: implied sum <= 1.0 (inverted/incomplete)", book)
            continue
        fair_odds = remove_vig_power_method(odds)
        for s, fo in zip(selections, fair_odds):
            prob_sums[s] += 1.0 / fo
        books_used += 1

    if books_used == 0:
        return {}
    avg = {s: prob_sums[s] / books_used for s in selections}
    total = sum(avg.values())
    if total <= 0.0 or not math.isfinite(total):
        return {}
    return {s: total / p for s, p in avg.items() if p > 0.0}


def find_best_price(
    market_ticks: list[OddsTick],
    selection_id: str,
    max_staleness_minutes: int,
    as_of: datetime | None = None,
) -> LineShopResult | None:
    now = _as_of(as_of)
    backs = [
        t for t in _latest_fresh(market_ticks, OddsType.BACK, now, max_staleness_minutes)
        if t.selection_id == selection_id and t.decimal_odds > 1.0
    ]
    if not backs:
        return None
    best = max(backs, key=lambda t: (t.decimal_odds, t.timestamp))
    true_odds = calculate_true_odds(market_ticks, max_staleness_minutes, now).get(selection_id)
    if not true_odds or true_odds <= 1.0:
        return None
    ev_pct = (best.decimal_odds / true_odds - 1.0) * 100.0
    return LineShopResult(
        match_id=best.match_id,
        selection_id=selection_id,
        best_back_odds=float(best.decimal_odds),
        best_back_bookmaker=best.bookmaker_id,
        true_odds_consensus=round(float(true_odds), 6),
        ev_pct=round(float(ev_pct), 4),
    )


def back_lay_profit_fraction(back_odds: float, lay_odds: float, commission_pct: float) -> float | None:
    """Per 1 unit backed: L = O_back / (O_lay - c); profit = (O_back - L*(O_lay-1) - 1)."""
    denominator = lay_odds - commission_pct
    if lay_odds <= 1.0 or back_odds <= 1.0 or denominator <= 0.0:
        return None
    lay_stake = back_odds / denominator
    profit = back_odds - lay_stake * (lay_odds - 1.0) - 1.0
    return profit if math.isfinite(profit) else None


def find_back_lay_arbitrage(
    market_ticks: list[OddsTick],
    selection_id: str,
    commission_pct: float,
    max_staleness_minutes: int,
    as_of: datetime | None = None,
) -> ArbitrageOpportunity | None:
    if not 0.0 <= commission_pct < 1.0:
        raise ValueError("commission_pct must be a fraction in [0, 1)")
    now = _as_of(as_of)
    backs = [t for t in _latest_fresh(market_ticks, OddsType.BACK, now, max_staleness_minutes)
             if t.selection_id == selection_id]
    lays = [t for t in _latest_fresh(market_ticks, OddsType.LAY, now, max_staleness_minutes)
            if t.selection_id == selection_id]

    best: tuple[float, OddsTick, OddsTick] | None = None
    for b in backs:
        for lay in lays:
            if b.bookmaker_id == lay.bookmaker_id or _market_key(b) != _market_key(lay):
                continue
            profit = back_lay_profit_fraction(b.decimal_odds, lay.decimal_odds, commission_pct)
            if profit is not None and profit > 0.0 and (best is None or profit > best[0]):
                best = (profit, b, lay)

    if best is None:
        return None
    profit, b, lay = best
    return ArbitrageOpportunity(
        match_id=b.match_id,
        selection_id=selection_id,
        back_bookmaker=b.bookmaker_id,
        back_odds=float(b.decimal_odds),
        lay_bookmaker=lay.bookmaker_id,
        lay_odds=float(lay.decimal_odds),
        exchange_commission_pct=float(commission_pct),
        net_profit_pct=round(profit * 100.0, 4),
    )


def find_market_surebets(
    ticks: list[OddsTick],
    max_staleness_minutes: int,
    as_of: datetime | None = None,
    required_selections: dict[str, int] | None = None,
    min_profit_pct: float = 0.0,
) -> list[MarketSurebet]:
    now = _as_of(as_of)
    required = {**DEFAULT_REQUIRED_SELECTIONS, **(required_selections or {})}
    fresh = _latest_fresh(ticks, OddsType.BACK, now, max_staleness_minutes)

    groups: dict[MarketKey, list[OddsTick]] = defaultdict(list)
    for t in fresh:
        if t.decimal_odds > 1.0:
            groups[_market_key(t)].append(t)

    surebets: list[MarketSurebet] = []
    for (match_id, market_type, line), group in groups.items():
        universe = {t.selection_id for t in group}
        expected = required.get(market_type)
        if len(universe) < 2 or (expected is not None and len(universe) != expected):
            continue

        # Completeness evidence: at least one bookmaker prices the ENTIRE market.
        book_selections: dict[str, set[str]] = defaultdict(set)
        for t in group:
            book_selections[t.bookmaker_id].add(t.selection_id)
        if not any(sels == universe for sels in book_selections.values()):
            continue

        best: dict[str, OddsTick] = {}
        for t in group:
            current = best.get(t.selection_id)
            if current is None or t.decimal_odds > current.decimal_odds:
                best[t.selection_id] = t

        inverse_sum = sum(1.0 / t.decimal_odds for t in best.values())
        if inverse_sum <= 0.0 or inverse_sum >= 1.0:
            continue
        profit_pct = (1.0 / inverse_sum - 1.0) * 100.0
        if profit_pct <= min_profit_pct:
            continue

        legs = [
            SurebetLeg(
                selection_id=sel,
                bookmaker_id=t.bookmaker_id,
                odds=float(t.decimal_odds),
                recommended_stake_pct=round((1.0 / t.decimal_odds) / inverse_sum, 8),
            )
            for sel, t in sorted(best.items())
        ]
        surebets.append(
            MarketSurebet(
                match_id=match_id,
                market_type=market_type,
                line=line,
                legs=legs,
                profit_pct=round(profit_pct, 4),
            )
        )
    return sorted(surebets, key=lambda s: s.profit_pct, reverse=True)
