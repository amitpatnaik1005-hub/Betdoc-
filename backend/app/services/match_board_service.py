"""The Oracle's match board (Group 103, points b.4 and b.11). Developed for Amit Ashok Kumar Patnaik.

A bookmaker-style view of the market BetDoc already prices: the manual parlay workbench's board (Ashoka's
candidates: Parimatch, 1xBet and Stake quotes from the authorised feeds, each selection's de-vigged fair
probability, EV and sharp steam, and the scoreline the market implies for three-way football), regrouped the way a
sportsbook lists it:

* sports, Cricket and Football first (``MATCH_BOARD_PRIORITY_SPORTS``), each with its fixture count in the window;
* a time window: ``live`` (kicked off, inside the board's in-play lookback), ``3h`` / ``12h`` (kicking off within),
  ``upcoming`` (not started), ``all``;
* countries and their leagues from the league registry (``odds_api_sport_key`` -> country); a feed key the registry
  does not map is listed under no country rather than given one;
* Group 112: the stored Odds API fixtures the live market is not pricing now (``MATCH_BOARD_STORED_PRICES``), with the
  prices as bought, how old they are and the fair odds de-vigged then: no EV, no best price, nothing to stake or slip
  (``priced_from: "stored"``); and each fixture's context from Football-Data.org or Sportmonks where one pairs it
  (kickoff confirmed or moved, status and score, table positions).

Nothing here is invented: no fixture, price, score or count exists that the feeds and models did not produce. A score
is shown only where a results feed paired the fixture and says so; otherwise the board says how long ago a match kicked
off.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.bookmakers.adapters import EXCHANGE_BOOKMAKERS, canonical_bookmaker, market_selection_label
from app.domain.manual_parlay import cognitive_parlay_rater as rater
from app.domain.oracle.markets import MarketKind, MarketRef
from app.domain.oracle.parlay_engine import league_label
from app.domain.pre_match.cadence import aware
from app.models.pre_match_odds import PreMatchOddsSnapshotModel
from app.models.sports_config import LeagueRegistry
from app.services import live_sports_ingestion_service as live_ingest
from app.services import manual_parlay, pre_match_odds_service
from app.services.omni_normalizer import default_alias_dictionary

Window = Literal["live", "3h", "12h", "upcoming", "all"]
WINDOW_HOURS: dict[str, float] = {"3h": 3.0, "12h": 12.0}
_SIDES = (("home", "HOME"), ("draw", "DRAW"), ("away", "AWAY"))
_MATCH_ODDS = MarketRef(MarketKind.MATCH_ODDS)


async def league_directory(db: AsyncSession) -> dict[str, dict[str, str]]:
    """Odds API feed key -> the registry's country and league name. Unreadable registry: empty (no country shown)."""
    try:
        rows = (await db.execute(select(LeagueRegistry.odds_api_sport_key, LeagueRegistry.country, LeagueRegistry.display_name)
                                 .where(LeagueRegistry.odds_api_sport_key.is_not(None)))).all()
    except SQLAlchemyError:
        return {}
    return {key: {"country": country, "league": name} for key, country, name in rows if key}


def in_window(card: dict[str, Any], window: Window, now: datetime) -> bool:
    if window == "all":
        return True
    if window == "live":
        return bool(card.get("in_play"))
    if card.get("in_play"):
        return False
    if window == "upcoming":
        return True
    kickoff = card.get("kickoff")
    if not kickoff:
        return False
    return datetime.fromisoformat(kickoff) <= now + timedelta(hours=WINDOW_HOURS[window])


def _summary(card: dict[str, Any], directory: dict[str, dict[str, str]]) -> dict[str, Any]:
    entry = directory.get(card.get("sport_key") or "", {})
    evs = [s["ev"] for m in card["markets"] for s in m["selections"] if s.get("ev") is not None]
    return {
        **card,
        "country": entry.get("country"),
        "league_name": entry.get("league") or card.get("league"),
        "market_count": len(card["markets"]),
        "selection_count": sum(len(m["selections"]) for m in card["markets"]),
        "best_ev": max(evs) if evs else None,
    }


def stored_card(row: PreMatchOddsSnapshotModel, fixture_id: str, now: datetime, settings: Settings) -> dict[str, Any] | None:
    """A fixture the live market is not pricing now, from its stored board: each account book's h2h price as bought, the
    fair probability de-vigged then, and the age of both. No EV and no best price: neither is claimed for a price that
    may have moved. None without two sides both priced and de-vigged."""
    bought = pre_match_odds_service.price_time(row)
    fair = (row.consensus_fair_odds or {}).get("outcomes") or {}
    prices: dict[str, dict[str, float]] = {}
    for book, quotes in (row.bookmaker_odds or {}).items():
        canonical = canonical_bookmaker(book)
        if canonical is None:
            continue
        for side, price in (quotes or {}).items():
            if isinstance(price, (int, float)) and price > 1.0:
                prices.setdefault(side, {}).setdefault(canonical, float(price))
    sides = [(side, label) for side, label in _SIDES if prices.get(side) and (fair.get(side) or {}).get("probability")]
    if len(sides) < 2:
        return None
    kickoff = aware(row.commence_time)
    started = kickoff <= now
    selections = [label for _, label in sides]
    quotes = {label: {b: p for b, p in prices[side].items() if b not in EXCHANGE_BOOKMAKERS} for side, label in sides}
    return {
        "fixture_id": fixture_id, "home": row.home_team, "away": row.away_team, "sport": manual_parlay.sport_of(row.sport_key, settings),
        "sport_key": row.sport_key, "league": league_label(row.sport_key), "kickoff": kickoff.isoformat(), "in_play": started,
        "minutes_since_kickoff": round((now - kickoff).total_seconds() / 60) if started else None, "max_stake_inr": None, "projection": None,
        "markets": [{
            "market": _MATCH_ODDS.key, "kind": _MATCH_ODDS.kind.value, "margins": rater.market_margins(selections, quotes),
            "selections": [{"leg_id": f"{fixture_id}|{_MATCH_ODDS.key}|{label}", "selection": label,
                            "label": market_selection_label(_MATCH_ODDS.key, label, row.home_team, row.away_team),
                            "fair_probability": round(float(fair[side]["probability"]), 4), "models": {}, "ev": None, "prices": prices[side],
                            "best": None, "steam": None} for side, label in sides],
        }],
        "priced_from": "stored", "prices_as_of": bought.isoformat(), "prices_age_minutes": max(0, round((now - bought).total_seconds() / 60)),
    }


async def stored_cards(db: AsyncSession, settings: Settings, now: datetime, live_ids: set[str]) -> list[dict[str, Any]]:
    """Every stored fixture (in play to the horizon) the live board does not already list. Unreadable store: none."""
    try:
        rows = await pre_match_odds_service.list_fixtures(db, settings, sport=None, window="all", now=now, limit=settings.MATCH_BOARD_MAX_FIXTURES * 2)
    except SQLAlchemyError:
        return []
    aliases = default_alias_dictionary()
    seen = set(live_ids)
    cards: list[dict[str, Any]] = []
    for row in rows:
        fixture_id = live_ingest.canonical_fixture_id(row, aliases)
        if fixture_id in seen:
            continue
        card = stored_card(row, fixture_id, now, settings)
        if card is not None:
            seen.add(fixture_id)
            cards.append(card)
    return cards


def _sport_order(labels: list[str], settings: Settings) -> list[str]:
    priority = [s for s in settings.MATCH_BOARD_PRIORITY_SPORTS if s in labels]
    return priority + [s for s in labels if s not in priority]


async def match_board(redis: Redis, db: AsyncSession, settings: Settings, now: datetime, *, sport: str | None = None,
                      window: Window = "all") -> dict[str, Any]:
    board = await manual_parlay.board(redis, settings, now, sport=None)
    live = [{**c, "priced_from": "live", "prices_as_of": None, "prices_age_minutes": None} for c in board["fixtures"]]
    stored = await stored_cards(db, settings, now, {c["fixture_id"] for c in live}) if settings.MATCH_BOARD_STORED_PRICES else []
    # in play first, then what the live market prices (actionable), then stored prices; soonest first within each
    every = sorted(live + stored, key=lambda c: (not c["in_play"], c["priced_from"] != "live", c["kickoff"] or ""))
    contexts = await live_ingest.read_context(redis, settings, [c["fixture_id"] for c in every])
    directory = await league_directory(db)
    cards = [{**_summary(c, directory), "context": contexts.get(c["fixture_id"])} for c in every if in_window(c, window, now)]
    sport_counts = Counter(c["sport"] for c in cards if c.get("sport"))
    sports = [{"sport": label, "count": sport_counts.get(label, 0), "live": sum(1 for c in cards if c.get("sport") == label and c.get("in_play")),
               "priority": label in settings.MATCH_BOARD_PRIORITY_SPORTS}
              for label in _sport_order(list(settings.MANUAL_PARLAY_SPORTS), settings)]
    if sport is not None:
        cards = [c for c in cards if c.get("sport") == sport]
    tree: dict[str | None, Counter[str]] = defaultdict(Counter)
    for c in cards:
        tree[c["country"]][c["league_name"] or "Unnamed league"] += 1
    countries = sorted(
        ({"country": country, "count": sum(leagues.values()), "leagues": [{"league": name, "count": n} for name, n in sorted(leagues.items())]}
         for country, leagues in tree.items()),
        key=lambda x: (x["country"] is None, -x["count"], x["country"] or ""),
    )
    return {
        "as_of": board["as_of"], "window": window, "sport": sport, "sports": sports, "countries": countries,
        "fixtures": cards[: settings.MATCH_BOARD_MAX_FIXTURES], "total": len(cards), "steam_readable": board["steam_readable"],
        "stats": {**board.get("stats", {}), "stored_fixtures": len(stored)},
    }
