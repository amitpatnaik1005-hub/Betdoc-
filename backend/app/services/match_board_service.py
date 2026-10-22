"""The Oracle's match board (Group 103, points b.4 and b.11). Developed for Amit Ashok Kumar Patnaik.

A bookmaker-style view of the market BetDoc already prices: the manual parlay workbench's board (Ashoka's
candidates: Parimatch, 1xBet and Stake quotes from the authorised feeds, each selection's de-vigged fair
probability, EV and sharp steam, and the scoreline the market implies for three-way football), regrouped the way a
sportsbook lists it:

* sports, Cricket and Football first (``MATCH_BOARD_PRIORITY_SPORTS``), each with its fixture count in the window;
* a time window: ``live`` (kicked off, inside the board's in-play lookback), ``3h`` / ``12h`` (kicking off within),
  ``upcoming`` (not started), ``all``;
* countries and their leagues from the league registry (``odds_api_sport_key`` -> country); a feed key the registry
  does not map is listed under no country rather than given one.

Nothing here is invented: no fixture, price, score or count exists that the feeds and models did not produce. There
is no live-score feed for these fixtures, so the board says how long ago a match kicked off, never a score.
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
from app.models.sports_config import LeagueRegistry
from app.services import manual_parlay

Window = Literal["live", "3h", "12h", "upcoming", "all"]
WINDOW_HOURS: dict[str, float] = {"3h": 3.0, "12h": 12.0}


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


def _sport_order(labels: list[str], settings: Settings) -> list[str]:
    priority = [s for s in settings.MATCH_BOARD_PRIORITY_SPORTS if s in labels]
    return priority + [s for s in labels if s not in priority]


async def match_board(redis: Redis, db: AsyncSession, settings: Settings, now: datetime, *, sport: str | None = None,
                      window: Window = "all") -> dict[str, Any]:
    board = await manual_parlay.board(redis, settings, now, sport=None)
    directory = await league_directory(db)
    cards = [_summary(c, directory) for c in board["fixtures"] if in_window(c, window, now)]
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
        "stats": board.get("stats", {}),
    }
