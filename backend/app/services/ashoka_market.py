"""Ashoka's live view of the market, and the slips it vets from it (Group 69).

Reading (no network: everything is already in Redis, written by Garuda's fleet and Aryabhata):

* fixtures: the live board (upcoming, inside ``horizon``), with teams, sport and kickoff;
* markets: Aryabhata's per-fixture market index (``Match Odds``, ``Totals 2.5``, ``BTTS`` ...);
* books: every book's latest prices per market. Books seen within ``ARYABHATA_BOOK_MAX_AGE_SECONDS``
  form the de-vigged consensus (any bookmaker counts); only Ashoka's five (Parimatch, 1xBet, Stake,
  Pinnacle, Betfair) become quotes, because those are where the user bets.

Per fixture the scoreline models are fitted to the 1X2 consensus (and Over/Under lines when quoted), and
every selection with a consensus and a quote becomes a ``LegCandidate``.

``vetted_slips`` runs the parlay engine over them and serialises each slip with its bookmaker views,
the Parimatch / 1xBet comparison at the recommended stake, and the quick-copy text. The engine's work
(not the stake, which depends on the bankroll asking) is cached for ``CACHE_SECONDS``.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.live_odds import read_snapshot
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS, canonical_bookmaker, fixture_name, market_selection_label
from app.domain.oracle.markets import MarketKind, MarketRef, parse_market
from app.domain.oracle.match_model import MarketTargets, build_models, consensus_two_way
from app.domain.oracle.parlay_engine import (
    GateThresholds,
    LegCandidate,
    ParlayEngine,
    Quote,
    SlipCandidate,
    SlipKind,
    Tier,
    league_label,
)
from app.domain.oracle.slip_formatter import LegPrice, book_view, compare, quick_copy, search_text
from app.services.aryabhata_pipeline import AryabhataKeys, read_market_books

logger = logging.getLogger("betdoc.ashoka")

CACHE_SECONDS = 20
HORIZON = timedelta(hours=72)
VETTED_BADGE = "1000% Vetted by Ashoka"
VALUE_BADGE = "Value · strictly bounded stake"
TITLES = {
    SlipKind.SINGLE: "Single Value Bet", SlipKind.DOUBLE: "Double", SlipKind.TREBLE: "Treble", SlipKind.ACCUMULATOR: "Accumulator",
    SlipKind.TRIXIE: "Trixie (4 bets)", SlipKind.YANKEE: "Yankee (11 bets)", SlipKind.CANADIAN: "Canadian (26 bets)", SlipKind.HEINZ: "Heinz (57 bets)",
}


def thresholds(settings: Settings) -> GateThresholds:
    return GateThresholds(settings.ASHOKA_MIN_JOINT_EV, settings.ASHOKA_MIN_JOINT_PROBABILITY, settings.ASHOKA_MAX_QUOTE_AGE_SECONDS, settings.ASHOKA_MIN_BOOKS)


def engine(settings: Settings, *, seed: int | None = None) -> ParlayEngine:
    return ParlayEngine(
        thresholds(settings), paths=settings.ASHOKA_MC_PATHS, kelly_fraction=settings.ASHOKA_KELLY_FRACTION, max_stake_pct=settings.ASHOKA_MAX_STAKE_PCT,
        value_max_stake_pct=settings.ASHOKA_VALUE_MAX_STAKE_PCT, max_legs=settings.ASHOKA_MAX_CANDIDATE_LEGS, max_slips=settings.ASHOKA_MAX_SLIPS,
        priority=priority(settings), seed=seed,
    )


def priority(settings: Settings) -> tuple[str, ...]:
    books = [canonical_bookmaker(b) for b in settings.ASHOKA_BOOKMAKER_PRIORITY.split(",")]
    return tuple(b for b in books if b) or tuple(ASHOKA_BOOKMAKERS)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def load_candidates(redis: Redis, settings: Settings, now: datetime, *, fixtures: set[str] | None = None) -> tuple[list[LegCandidate], dict[str, Any]]:
    """Every priced leg of every upcoming fixture (or just ``fixtures``), and what was read."""
    board = await read_snapshot(redis) or []
    meta: dict[str, dict[str, Any]] = {}
    for tick in board:
        kickoff = _aware(tick.commence_time)
        if tick.market_type != "Match Odds" or kickoff is None or not now < kickoff <= now + HORIZON:
            continue
        if fixtures is not None and tick.match_id not in fixtures:
            continue
        meta.setdefault(tick.match_id, {"home": tick.home_team, "away": tick.away_team, "sport_key": tick.sport_key, "kickoff": kickoff})
    stats: dict[str, Any] = {"fixtures": len(meta), "markets": 0, "books": 0, "legs": 0}
    if not meta:
        return [], stats
    keys = AryabhataKeys(settings.ARYABHATA_PREFIX)
    pipe = redis.pipeline(transaction=False)
    for fixture_id in meta:
        pipe.smembers(keys.markets(fixture_id))
    indexes = await pipe.execute()
    market_keys: list[str] = []
    for fixture_id, markets in zip(meta, indexes, strict=True):
        for market in {"Match Odds", *(markets or ())}:
            if parse_market(market) is not None:
                market_keys.append(f"{fixture_id}|{market}")
    books = await read_market_books(redis, settings, market_keys)
    stats["markets"] = sum(1 for lines in books.values() if lines)
    commissions = settings.EXCHANGE_COMMISSION_RATES
    consensus_age = timedelta(seconds=settings.ARYABHATA_BOOK_MAX_AGE_SECONDS)
    legs: list[LegCandidate] = []
    for fixture_id, info in meta.items():
        per_market: dict[MarketRef, tuple[dict[str, float] | None, int, dict[str, dict[str, Quote]]]] = {}
        for market_key, lines in books.items():
            fx, _, market = market_key.partition("|")
            if fx != fixture_id or not lines:
                continue
            ref = parse_market(market)
            if ref is None:
                continue
            fresh = [line for line in lines if not line.is_suspended and now - _aware(line.seen_at) <= consensus_age]  # type: ignore[operator]
            prices = [{label: float(price) for label, price in line.prices.items()} for line in fresh]
            complete = [p for p in prices if all(label in p for label in ref.selections)]
            fair = consensus_two_way(complete, ref.selections) if complete else None
            quotes: dict[str, dict[str, Quote]] = {s: {} for s in ref.selections}
            for line in lines:
                if line.is_suspended:
                    continue
                book = canonical_bookmaker(line.bookmaker_id)
                if book is None:
                    continue
                stats["books"] += 1
                for selection in ref.selections:
                    price = line.prices.get(selection)
                    if price is None or price <= 1:
                        continue
                    quote = Quote(book, float(price), _aware(line.seen_at), float(commissions.get(line.bookmaker_id, commissions.get(book, 0.0))))  # type: ignore[arg-type]
                    held = quotes[selection].get(book)
                    if held is None or quote.observed_at > held.observed_at or (quote.observed_at == held.observed_at and quote.odds > held.odds):
                        quotes[selection][book] = quote
            per_market[ref] = (fair, len(complete), quotes)
        match_odds = per_market.get(MarketRef(MarketKind.MATCH_ODDS))
        models = None
        if match_odds is not None and match_odds[0] is not None:
            fair = match_odds[0]
            totals = {ref.line: f["OVER"] for ref, (f, _, _) in per_market.items() if ref.kind is MarketKind.TOTALS and f is not None and ref.line is not None and not ref.has_push}
            try:
                models = build_models(MarketTargets(fair["HOME"], fair["DRAW"], fair["AWAY"], totals))
            except (ValueError, FloatingPointError):
                models = None
        for ref, (fair, n_books, quotes) in per_market.items():
            for selection in ref.selections:
                if not quotes[selection]:
                    continue
                legs.append(
                    LegCandidate(
                        fixture_id=fixture_id, home=info["home"], away=info["away"], market=ref, selection=selection, quotes=quotes[selection],
                        consensus=None if fair is None else fair[selection], consensus_books=n_books, models=models,
                        sport_key=info["sport_key"], league=league_label(info["sport_key"]), kickoff=info["kickoff"],
                    )
                )
    stats["legs"] = len(legs)
    return legs, stats


# ================================================================ serialising
def leg_payload(leg: LegCandidate, quote: Quote | None, now: datetime) -> dict[str, Any]:
    probability = leg.probability
    models = {name: round(sum(v for r, v in dist.items() if r.value in ("WON", "HALF_WON")), 4) for name, dist in leg.distributions().items()}
    ev = None if quote is None else leg.ev(quote.net_odds)
    parts = [f"fair {probability:.1%}"]
    if quote is not None:
        parts.append(f"{ASHOKA_BOOKMAKERS.get(quote.bookmaker, quote.bookmaker)} {quote.odds:g} implies {1 / quote.odds:.1%}")
        parts.append(f"EV {ev:+.1%}")
    if models:
        parts.append("models " + ", ".join(f"{name.replace('_', '-')} {p:.0%}" for name, p in models.items()))
    return {
        "leg_id": leg.leg_id,
        "fixture_id": leg.fixture_id,
        "fixture": fixture_name(leg.home, leg.away),
        "home": leg.home,
        "away": leg.away,
        "league": leg.league,
        "sport_key": leg.sport_key,
        "kickoff": leg.kickoff.isoformat() if leg.kickoff else None,
        "market": leg.market.key,
        "selection": leg.selection,
        "label": market_selection_label(leg.market.key, leg.selection, leg.home, leg.away),
        "fair_probability": round(probability, 4),
        "models": models,
        "ev": None if ev is None else round(ev, 4),
        "prices": {book: q.odds for book, q in leg.quotes.items()},
        "price_age_seconds": {book: round(q.age(now), 1) for book, q in leg.quotes.items()},
        "rationale": "; ".join(parts),
    }


def slip_core(slip: SlipCandidate, now: datetime) -> dict[str, Any]:
    """Everything about a slip that does not depend on who asks."""
    sim = slip.sim
    title = ("Multi-League Cross " if slip.cross_league and slip.kind in (SlipKind.DOUBLE, SlipKind.TREBLE, SlipKind.ACCUMULATOR) else "") + TITLES[slip.kind]
    return {
        "slip_id": slip.slip_id,
        "kind": slip.kind,
        "title": f"ASHOKA {title}",
        "tier": slip.verdict.tier,
        "badge": VETTED_BADGE if slip.verdict.tier is Tier.VETTED else VALUE_BADGE,
        "legs": [leg_payload(leg, q, now) for leg, q in zip(slip.legs, slip.quotes, strict=True)],
        "book": slip.book,
        "odds": round(slip.odds, 3),
        "cross_league": slip.cross_league,
        "leagues": list(slip.leagues),
        "stake_fraction": round(slip.stake_fraction, 6),
        "odds_age_seconds": round(max(q.age(now) for q in slip.quotes), 1),
        "simulation": {
            "paths": sim.paths,
            "joint_probability": round(sim.joint_probability, 4),
            "joint_probability_se": round(sim.joint_probability_se, 4),
            "full_win_probability": round(sim.full_win_probability, 4),
            "probability_band": [round(sim.probability_band[0], 4), round(sim.probability_band[1], 4)],
            "joint_ev": round(sim.joint_ev, 4),
            "joint_ev_se": round(sim.joint_ev_se, 4),
            "analytic_ev": None if sim.analytic_ev is None else round(sim.analytic_ev, 4),
            "std_return": round(sim.std_return, 4),
            "p_total_loss": round(sim.p_total_loss, 4),
            "var_95": round(sim.var_95, 4),
            "cvar_95": round(sim.cvar_95, 4),
            "kelly_fraction": round(sim.kelly_fraction, 4),
        },
        "verdict": {"tier": slip.verdict.tier, "checks": slip.verdict.checks, "reasons": list(slip.verdict.reasons)},
    }


def personalise(core: dict[str, Any], bankroll: Decimal | None) -> dict[str, Any]:
    """The stake for this bankroll, and the bookmaker views, comparison and quick copy at that stake."""
    stake = Decimal(0)
    if bankroll is not None and bankroll > 0:
        stake = (bankroll * Decimal(str(core["stake_fraction"]))).quantize(Decimal("1"), rounding=ROUND_DOWN)
        if stake < 10:
            stake = Decimal(0)
    reference = stake if stake > 0 else Decimal("1000")
    legs = [
        LegPrice(leg["fixture_id"], leg["home"], leg["away"], leg["market"], leg["selection"], leg["prices"], leg["fair_probability"], leg["league"], leg["kickoff"], leg["rationale"])
        for leg in core["legs"]
    ]
    multiple = len(legs) > 1
    if core["kind"] in (SlipKind.TRIXIE, SlipKind.YANKEE, SlipKind.CANADIAN, SlipKind.HEINZ):
        views = []  # a system's lines are priced line by line at the book; the legs' prices are what to compare
    else:
        views = [book_view(book, legs, reference) for book in ASHOKA_BOOKMAKERS]
    comparison = compare(views, reference) if views else None
    book = (comparison.recommended or comparison.best) if comparison else core["book"]
    odds = next((v.odds for v in views if v.bookmaker == book), None)
    sim = core["simulation"]
    extra = [f"True joint probability {sim['joint_probability']:.1%} · joint EV {sim['joint_ev']:+.1%} · {sim['paths']:,} simulated paths", core["badge"]]
    out = dict(core)
    out.update(
        stake_inr=str(stake),
        reference_stake_inr=str(reference),
        expected_profit_inr=str((reference * Decimal(str(sim["joint_ev"]))).quantize(Decimal("0.01"))),
        books=[
            {"bookmaker": v.bookmaker, "label": v.label, "available": v.available, "odds": None if v.odds is None else str(v.odds),
             "payout_inr": None if v.payout is None else str(v.payout), "legs": list(v.legs), "missing": list(v.missing), "note": v.note}
            for v in views
        ],
        comparison=None if comparison is None else {
            "best": comparison.best, "best_odds": None if comparison.best_odds is None else str(comparison.best_odds),
            "parimatch_odds": None if comparison.pair.get("parimatch") is None else str(comparison.pair["parimatch"]),
            "onexbet_odds": None if comparison.pair.get("1xbet") is None else str(comparison.pair["1xbet"]),
            "difference_inr": None if comparison.difference_inr is None else str(comparison.difference_inr),
            "difference_pct": None if comparison.difference_pct is None else str(comparison.difference_pct),
            "recommended": comparison.recommended, "recommendation": comparison.recommendation,
        },
        quick_copy=quick_copy(core["title"], legs, book or core["book"], odds, reference, extra),
        search=search_text(legs),
        multiple=multiple,
    )
    return out


async def vetted_slips(redis: Redis, settings: Settings, now: datetime, bankroll: Decimal | None, *, refresh: bool = False) -> dict[str, Any]:
    cache_key = f"{settings.ARYABHATA_PREFIX}:ashoka:slips"
    payload: dict[str, Any] | None = None
    if not refresh:
        try:
            raw = await redis.get(cache_key)
            payload = json.loads(raw) if raw else None
        except (RedisError, OSError, ValueError):
            payload = None
    if payload is None:
        candidates, stats = await load_candidates(redis, settings, now)
        report = engine(settings).generate(candidates, now)
        payload = {
            "generated_at": now.isoformat(),
            "read": stats,
            "scanned": report.scanned,
            "simulated": report.simulated,
            "legs_considered": report.legs_considered,
            "rejected": dict(report.rejected.most_common(8)),
            "thresholds": {
                "min_joint_ev": settings.ASHOKA_MIN_JOINT_EV, "min_joint_probability": settings.ASHOKA_MIN_JOINT_PROBABILITY,
                "max_quote_age_seconds": settings.ASHOKA_MAX_QUOTE_AGE_SECONDS, "paths": settings.ASHOKA_MC_PATHS,
            },
            "slips": [slip_core(slip, now) for slip in report.slips],
        }
        try:
            await redis.set(cache_key, json.dumps(payload, default=str), ex=CACHE_SECONDS)
        except (RedisError, OSError):
            pass
    generated = datetime.fromisoformat(payload["generated_at"])
    age = (now - generated).total_seconds()
    out = dict(payload)
    out["age_seconds"] = round(max(age, 0.0), 1)
    out["slips"] = [personalise(core, bankroll) for core in payload["slips"]]
    for slip in out["slips"]:
        slip["odds_age_seconds"] = round(slip["odds_age_seconds"] + max(age, 0.0), 1)
    return out


async def recheck(redis: Redis, settings: Settings, now: datetime, leg_ids: Sequence[str], kind: str | None, bankroll: Decimal | None) -> dict[str, Any] | None:
    """One slip re-priced against the latest feeds (None: a leg is no longer quoted)."""
    fixtures = {leg_id.split("|", 1)[0] for leg_id in leg_ids}
    candidates, _ = await load_candidates(redis, settings, now, fixtures=fixtures)
    by_id = {leg.leg_id: leg for leg in candidates}
    legs = [by_id.get(leg_id) for leg_id in leg_ids]
    if any(leg is None for leg in legs):
        return None
    slip_kind = SlipKind(kind) if kind in SlipKind.__members__ else None
    slip = engine(settings).evaluate(legs, slip_kind, now)  # type: ignore[arg-type]
    if slip is None:
        return None
    return personalise(slip_core(slip, now), bankroll)


def fair_value_inputs(leg: LegCandidate | None) -> float | None:
    """A leg's current probability for the cashout advisor (None when nothing prices it)."""
    if leg is None:
        return None
    try:
        p = leg.probability
    except ValueError:
        return None
    return p if math.isfinite(p) else None
