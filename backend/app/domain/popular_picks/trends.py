"""Ashoka's trend scan: sharp steam parlays, the public trap detector, AI-vetted hybrids (Group 69).

Built from the live market only (``ashoka_market.load_candidates``) and Aryabhata's steam flags:

* SHARP STEAM: legs where sharp money is moving the consensus (Aryabhata's steam flag) and the best
  price still beats it, two or three on different fixtures, kept if the slip's joint EV is positive.
* PUBLIC TRAP: the crowd's treble, the day's shortest-priced favourites on different fixtures. Priced
  at the best book against the de-vigged market, it usually loses money to the vig, and the warning says
  by how much. A public share is shown only when it was measured: the share of BetDoc's own recorded
  bets on those selections, or a figure an administrator entered with its source. Never an estimate.
* AI-VETTED HYBRID: the crowd's favourite anchor leg with the sharpest value leg on another fixture,
  kept if the pair's joint EV is positive.

Every slip goes through the same engine (Monte Carlo, gates) as Ashoka's own; the trend rows carry its
numbers. Rows expire with the prices they were built on.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.bookmakers.adapters import canonical_bookmaker, fixture_name, market_selection_label
from app.domain.oracle.markets import MarketKind, parse_market
from app.domain.oracle.parlay_engine import LegCandidate, Quote, SlipCandidate
from app.models.popular_picks import PickType, PopularParlayModel, TrendCategory
from app.models.user_bets_ledger import UserPlacedBet, UserPlacedLeg
from app.services import ashoka_market

logger = logging.getLogger("betdoc.ashoka.trends")

TREND_TTL = timedelta(minutes=30)
PICK_TYPE = {TrendCategory.SHARP_STEAM: PickType.SHARP_MONEY, TrendCategory.PUBLIC_TRAP: PickType.TRENDING, TrendCategory.AI_HYBRID: PickType.AI_PREDICTED}


def _leg_json(slip: SlipCandidate) -> list[dict[str, Any]]:
    return [
        {
            "match_id": leg.fixture_id, "selection": leg.selection, "odds": round(q.odds, 3), "market": leg.market.key,
            "label": market_selection_label(leg.market.key, leg.selection, leg.home, leg.away), "fixture": fixture_name(leg.home, leg.away),
            "fair_probability": round(leg.probability, 4), "book": q.bookmaker,
        }
        for leg, q in zip(slip.legs, slip.quotes, strict=True)
    ]


async def crowd_shares(session: AsyncSession, legs: Sequence[LegCandidate], now: datetime, *, days: int = 7, min_bets: int = 5) -> dict[str, float]:
    """leg id -> the share of BetDoc's recorded bets on that fixture and market that took this selection."""
    if not legs:
        return {}
    rows = (
        await session.execute(
            select(UserPlacedLeg.fixture_id, UserPlacedLeg.market, UserPlacedLeg.selection)
            .join(UserPlacedBet, UserPlacedBet.id == UserPlacedLeg.bet_id)
            .where(UserPlacedLeg.fixture_id.in_({leg.fixture_id for leg in legs}), UserPlacedBet.placed_at >= now - timedelta(days=days))
        )
    ).all()
    totals: Counter[tuple[str, str]] = Counter((f, m) for f, m, _ in rows)
    picks: Counter[tuple[str, str, str]] = Counter(rows)
    out: dict[str, float] = {}
    for leg in legs:
        total = totals[(leg.fixture_id, leg.market.key)]
        if total >= min_bets:
            out[leg.leg_id] = picks[(leg.fixture_id, leg.market.key, leg.selection)] / total
    return out


async def steam_leg_ids(redis: Redis, settings: Settings, now: datetime) -> set[str]:
    from app.services.aryabhata_pipeline import read_active_edges  # noqa: PLC0415

    try:
        edges = await read_active_edges(redis, settings, now)
    except (RedisError, OSError, TimeoutError):
        return set()
    out = set()
    for edge in edges:
        ref = parse_market(edge.market_type)
        if edge.is_steam_move and ref is not None:
            out.add(f"{edge.fixture_id}|{ref.key}|{edge.selection}")
    return out


def _row(category: TrendCategory, title: str, slip: SlipCandidate, now: datetime, *, warning: str | None = None, share: float | None = None, share_source: str | None = None, extra: dict[str, Any] | None = None) -> PopularParlayModel:
    sim = slip.sim
    return PopularParlayModel(
        id=uuid.uuid4(), title=title[:120], pick_type=PICK_TYPE[category].value, legs=_leg_json(slip), total_odds=round(max(slip.odds, 1.0), 4),
        historical_success_rate=round(min(max(sim.joint_probability, 0.0), 1.0), 4), is_active=True, expires_at=now + TREND_TTL,
        category=category.value, true_ev_pct=round(sim.joint_ev * 100, 2), true_probability=round(sim.joint_probability, 4),
        public_share_pct=None if share is None else round(share * 100, 1), public_share_source=share_source, warning=warning,
        analysis={
            "slip_id": slip.slip_id, "kind": slip.kind, "book": slip.book, "tier": slip.verdict.tier, "reasons": list(slip.verdict.reasons),
            "paths": sim.paths, "joint_ev": round(sim.joint_ev, 4), "joint_probability": round(sim.joint_probability, 4), **(extra or {}),
        },
    )


async def scan(session: AsyncSession, redis: Redis, settings: Settings, now: datetime) -> list[PopularParlayModel]:
    """Replace the previous scan's rows with this one's (manual and legacy rows are left alone)."""
    candidates, _ = await ashoka_market.load_candidates(redis, settings, now)
    engine = ashoka_market.engine(settings)
    max_age = settings.ASHOKA_MAX_QUOTE_AGE_SECONDS
    priced: list[tuple[LegCandidate, float, float]] = []  # leg, ev at its best fresh price, probability
    for leg in candidates:
        quote = leg.best_quote(now, max_age, multiples=True)
        if quote is None:
            continue
        try:
            priced.append((leg, leg.ev(quote.net_odds), leg.probability))
        except ValueError:
            continue
    rows: list[PopularParlayModel] = []
    steam = await steam_leg_ids(redis, settings, now)

    # sharp steam: positive-EV steam legs, best per fixture
    sharp: dict[str, LegCandidate] = {}
    for leg, ev, _ in sorted(priced, key=lambda item: -item[1]):
        if ev > 0 and leg.leg_id in steam:
            sharp.setdefault(leg.fixture_id, leg)
    sharp_legs = list(sharp.values())
    for size in (3, 2):
        if len(sharp_legs) >= size:
            slip = engine.evaluate(sharp_legs[:size], None, now)
            if slip is not None and slip.sim.joint_ev > 0:
                rows.append(_row(TrendCategory.SHARP_STEAM, f"Sharp Steam {'Treble' if size == 3 else 'Double'}: smart money and +EV aligned", slip, now))
                break

    # the public's treble: the day's three shortest favourites (1X2), on different fixtures
    favourites: dict[str, tuple[LegCandidate, float]] = {}
    for leg, _, probability in priced:
        if leg.market.kind is MarketKind.MATCH_ODDS and leg.selection != "DRAW":
            held = favourites.get(leg.fixture_id)
            if held is None or probability > held[1]:
                favourites[leg.fixture_id] = (leg, probability)
    public = [leg for leg, p in sorted(favourites.values(), key=lambda item: -item[1]) if p >= 0.5][:3]
    if len(public) >= 2:
        slip = engine.evaluate(public, None, now)
        if slip is not None:
            shares = await crowd_shares(session, public, now)
            measured = [shares[leg.leg_id] for leg in public if leg.leg_id in shares]
            share = sum(measured) / len(measured) if len(measured) == len(public) else None
            who = f"{share:.0%} of BetDoc bettors on these fixtures are on these favourites" if share is not None else "The day's shortest favourites, the multiple casual bettors back most"
            kind = "treble" if len(public) == 3 else "double"
            if slip.sim.joint_ev < 0:
                warning = f"⚠️ Public Trap: {who}, but true EV is {slip.sim.joint_ev:+.1%} on this {kind} due to the bookmaker's vig."
                rows.append(_row(TrendCategory.PUBLIC_TRAP, f"Public Favourites {kind.title()}", slip, now, warning=warning, share=share, share_source="BetDoc recorded bets" if share is not None else None))
            anchor = public[0]
            # the hybrid: the crowd's best anchor with the sharpest value leg elsewhere
            value = next((leg for leg, ev, _ in sorted(priced, key=lambda item: (-(item[0].leg_id in steam), -item[1])) if ev > 0 and leg.fixture_id != anchor.fixture_id), None)
            if value is not None:
                hybrid = engine.evaluate([anchor, value], None, now)
                if hybrid is not None and hybrid.sim.joint_ev > 0:
                    rows.append(
                        _row(TrendCategory.AI_HYBRID, f"AI Vetted Hybrid: {anchor.home if anchor.selection == 'HOME' else anchor.away} anchor + value leg", hybrid, now,
                             extra={"anchor": anchor.leg_id, "value_leg": value.leg_id, "value_is_steam": value.leg_id in steam})
                    )

    await session.execute(update(PopularParlayModel).where(PopularParlayModel.category.is_not(None), PopularParlayModel.is_active.is_(True)).values(is_active=False))
    session.add_all(rows)
    await session.commit()
    logger.info("Ashoka trend scan: %d legs priced, %d steam legs, %d trend rows", len(priced), len(steam), len(rows))
    return rows


async def evaluate_external(session: AsyncSession, redis: Redis, settings: Settings, now: datetime, body: Any) -> PopularParlayModel:
    """An administrator's copy of a bookmaker's popular parlay, priced against the live market."""
    candidates, _ = await ashoka_market.load_candidates(redis, settings, now)

    def norm(text: str) -> str:
        return " ".join("".join(ch for ch in text.casefold() if ch.isalnum() or ch.isspace()).split())

    legs: list[LegCandidate] = []
    unmatched: list[str] = []
    book = canonical_bookmaker(body.bookmaker) or body.bookmaker.strip().casefold().replace(" ", "_")
    for item in body.legs:
        ref = parse_market(item.market)
        selection = item.selection.strip().upper()
        found = next(
            (c for c in candidates if ref is not None and c.market == ref and c.selection == selection and {norm(c.home), norm(c.away)} == {norm(item.home), norm(item.away)}),
            None,
        )
        if found is None:
            unmatched.append(f"{item.home} vs {item.away} {item.market} {selection}")
            continue
        quotes = {book: Quote(book, float(item.odds), now)}  # the price as the administrator saw it on that book
        legs.append(LegCandidate(found.fixture_id, found.home, found.away, found.market, found.selection, quotes, found.consensus, found.consensus_books, found.models, found.sport_key, found.league, found.kickoff))
    if unmatched or len(legs) < 2:
        raise ValueError("not every leg is in the live market: " + "; ".join(unmatched or ["fewer than two legs matched"]))
    slip = ashoka_market.engine(settings).evaluate(legs, None, now)
    if slip is None:
        raise ValueError("the parlay could not be priced")
    share = None if body.public_share_pct is None else body.public_share_pct / 100
    warning = None
    category = TrendCategory.PUBLIC_TRAP
    if slip.sim.joint_ev < 0:
        who = f"{body.public_share_pct:g}% of {body.bookmaker} bettors are on this" if body.public_share_pct is not None else f"A popular {body.bookmaker} parlay"
        warning = f"⚠️ Public Trap: {who} ({body.public_share_source or 'source not given'}), but true EV is {slip.sim.joint_ev:+.1%} due to heavy bookmaker vig."
    row = _row(category, body.title, slip, now, warning=warning, share=share, share_source=body.public_share_source)
    if slip.sim.joint_ev >= 0:
        row.category = None  # not a trap: shown as an ordinary trending parlay
    row.expires_at = now + timedelta(hours=6)
    session.add(row)
    await session.commit()
    return row


def odds_product(legs: Sequence[dict[str, Any]]) -> float:
    return math.prod(float(leg["odds"]) for leg in legs)
