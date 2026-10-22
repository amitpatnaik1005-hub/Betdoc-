"""The Chef's Table (Group 104, point b.5(c)): BetDoc's own bet generator, served three ways. Developed for Amit Ashok
Kumar Patnaik.

Fully automatic: Ashoka's vetted slips (every quoted fixture scanned, 10,000 Monte Carlo paths, the anti-correlation
gate, the joint EV and joint probability bars) are taken best joint EV first (``CHEF_TABLE_CANDIDATES``), each re-rated
through the 15-pillar fortress as a trial (nothing recorded) at the retail books into its BetDoc Slip Score, and
served as ``recipe.serve`` plates them: #1 the handcrafted parlay of the day, #2 the safe fortress, #3 the high-yield
special. Ashoka may count a sharp book's price; the table re-rates at the books a bet is placed with, so a slip with
negative joint EV there, or one a pillar vetoes, is never served.

Every leg carries a recipe card, all of it read from the engines:

1. why it was picked: BetDoc's fair probability against the price's implied probability, the edge, the models' views;
2. the scoreline link: its result at its fixture's likeliest score and its chance under the market-fitted Dixon-Coles
   matrix (three-way football; other sports carry no scoreline model and say so);
3. a stress test: its chance of paying inside three final-score scenarios, with each scenario's likelihood;
4. why the others were not served: Ashoka's own rejection counts and bars, and the slips this one out-scored.

The stake is KUMBHA's (the fortress's sizing pillar) for the asking user's bankroll. Cached per user for
``CHEF_TABLE_CACHE_SECONDS``; a slip whose legs are no longer quoted drops out.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.builder.recipe import Rated, scoreline_link, serve, stress_test
from app.domain.oracle.parlay_engine import LegCandidate
from app.services import ashoka_market, manual_parlay
from app.services.twin import vetting

logger = logging.getLogger("betdoc.chef")
DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"


def _cache_key(settings: Settings, user_id: uuid.UUID) -> str:
    return f"{settings.ARYABHATA_PREFIX}:chef:{user_id}"


def recipe_card(leg: dict[str, Any], book: str | None, candidate: LegCandidate | None) -> dict[str, Any]:
    """One leg's recipe card (points 1-3; point 4 is the table's)."""
    price = leg["prices"].get(book) if book else None
    price = price if price is not None else max(leg["prices"].values(), default=None)
    fair = float(leg["fair_probability"])
    implied = None if not price else 1.0 / price
    edge = None if not price else fair * price - 1.0
    matrix = candidate.models.matrices.get("dixon_coles") if candidate is not None and candidate.models is not None else None
    ref, selection = (candidate.market, candidate.selection) if candidate is not None else (None, None)
    why = (f"BetDoc prices it at {fair:.1%}; {book or 'the best book'} at {price} implies {implied:.1%}: an edge of {edge:+.1%}."
           if price and implied is not None and edge is not None else f"BetDoc prices it at {fair:.1%}; no price is quoted at {book} now.")
    return {
        "leg_id": leg["leg_id"], "fixture": leg["fixture"], "league": leg.get("league"), "kickoff": leg.get("kickoff"), "market": leg["market"],
        "selection": leg["selection"], "label": leg["label"], "price": price, "book": book,
        "why": {"fair_probability": fair, "implied_probability": None if implied is None else round(implied, 6),
                "edge": None if edge is None else round(edge, 6), "models": leg.get("models", {}), "text": why},
        "scoreline": None if ref is None else scoreline_link(matrix, ref, selection),  # type: ignore[arg-type]
        "stress_test": [] if ref is None else stress_test(matrix, ref, selection),  # type: ignore[arg-type]
    }


async def chef_table(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, bankroll: Decimal | None,
                     now: datetime, *, refresh: bool = False) -> dict[str, Any]:
    key = _cache_key(settings, user_id)
    if not refresh and settings.CHEF_TABLE_CACHE_SECONDS:
        try:
            cached = await redis.get(key)
            if cached:
                return json.loads(cached)
        except (RedisError, OSError, ValueError):
            pass
    vetted = await ashoka_market.vetted_slips(redis, settings, now, bankroll)
    pool = sorted(vetted["slips"], key=lambda s: -float(s["simulation"]["joint_ev"]))[: settings.CHEF_TABLE_CANDIDATES]
    rated: list[Rated] = []
    for slip in pool:
        leg_ids = [leg["leg_id"] for leg in slip["legs"]]
        try:
            run = await manual_parlay.trial(sessions, redis, settings, user_id, leg_ids=leg_ids, kind=str(slip["kind"]), book=None, bankroll=bankroll, now=now)
        except (manual_parlay.WorkbenchRefusal, vetting.TwinRefusal, ValueError) as exc:
            logger.info("chef: slip %s left the table (%s)", slip["slip_id"], exc)
            continue
        audit = run.audit
        rated.append(Rated(key=str(slip["slip_id"]), legs=len(leg_ids), score=run.rating.score, joint_ev=float(audit.joint_ev or 0.0),
                           joint_probability=float(audit.joint_probability or 0.0), odds=float(audit.total_odds),
                           vetoed=any(row["status"] == "FAIL" for row in run.rating.breakdown),
                           payload={"slip": slip, "rating": run.rating.as_dict(), "book": audit.bookmaker, "odds": str(audit.total_odds),
                                    "stake_inr": str(audit.stake_inr), "joint_ev": audit.joint_ev, "joint_probability": audit.joint_probability,
                                    "pillars_passed": audit.pillars_passed, "is_vetted": audit.is_vetted}))
    plates = serve(rated)
    fixtures = {leg["fixture_id"] for _, _, r in plates for leg in r.payload["slip"]["legs"]}
    candidates: dict[str, LegCandidate] = {}
    if fixtures:
        loaded, _ = await ashoka_market.load_candidates(redis, settings, now - vetting.LIVE_LOOKBACK, fixtures=fixtures)
        candidates = {leg.leg_id: leg for leg in loaded}
    served = []
    for code, title, r in plates:
        slip, book = r.payload["slip"], r.payload["book"]
        served.append({
            "code": code, "title": title, "slip_id": r.key, "kind": slip["kind"], "slip_title": slip["title"], "book": book, "odds": r.payload["odds"],
            "stake_inr": r.payload["stake_inr"], "joint_ev": r.payload["joint_ev"], "joint_probability": r.payload["joint_probability"],
            "rating": r.payload["rating"], "pillars_passed": r.payload["pillars_passed"], "is_vetted": r.payload["is_vetted"],
            "legs": slip["legs"], "recipe": [recipe_card(leg, book, candidates.get(leg["leg_id"])) for leg in slip["legs"]],
            "outscored": [{"slip_id": o.key, "score": o.score, "joint_ev": o.joint_ev} for o in rated if o.key != r.key and o.score < r.score],
        })
    table = {
        "generated_at": now.isoformat(), "plates": served, "rated": len(rated), "considered": len(pool),
        "rejected": vetted.get("rejected", {}), "thresholds": vetted.get("thresholds", {}), "scanned": vetted.get("scanned"),
        "simulated": vetted.get("simulated"), "legs_considered": vetted.get("legs_considered"), "developer_credit": DEVELOPER_CREDIT,
    }
    if settings.CHEF_TABLE_CACHE_SECONDS:
        try:
            await redis.set(key, json.dumps(table, default=str), ex=settings.CHEF_TABLE_CACHE_SECONDS)
        except (RedisError, OSError):
            pass
    return table
