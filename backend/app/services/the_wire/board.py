"""VIDUR's intelligence board (Group 108, point b.14b): the Wire's own records as one four-column board.

``build`` reads, for the fixtures the Wire tracks (``fixtures.tracked``):

* the articles published in the last ``WIRE_BOARD_HOURS`` (``wire_news_sentiment_articles``), each against the
  consensus when it landed (recorded for catalyst impacts) and its catalyst, if the market followed it;
* each fixture's active absences, through the lineup model, as one item;
* each fixture's latest forecast, as an item when its friction factor or dew makes it news;
* the steam moves in the odds ticks over ``WIRE_BOARD_STEAM_WINDOW_MINUTES``;

then the consensus now (``news.consensus``) and Ashoka's valued legs (the smallcases' shared scan) for every fixture
an item names, and what the last neural chain recorded of each fixture's best leg. The unfiltered board is cached
for ``WIRE_BOARD_CACHE_SECONDS`` (Redis) and filtered per request. Nothing is filled in: a figure that was not
measured is absent, and the item says so. Developed for Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS
from app.domain.market_signals import SQLOddsTickRepository, SteamDetectorEngine
from app.domain.the_wire import intel_board as ib
from app.models.the_wire import InjuryRosterReport, NewsArticleSentiment, WeatherSnapshot
from app.schemas.market_signals import OddsType, SteamAlert
from app.services import neural_bus as bus
from app.services.the_wire import lineups, news, weather
from app.services.the_wire.fixtures import Fixture, tracked

logger = logging.getLogger("betdoc.vidur.board")

ARTICLE_CAP = 600  # read at most this many recent articles per build
SEVERE = frozenset({"OUT", "SUSPENDED"})


def _key(settings: Settings) -> str:
    return f"{settings.WIRE_PREFIX}:board"


def _aware(moment: datetime) -> datetime:
    return news._aware(moment)  # noqa: SLF001 - SQLite returns naive UTC


def oracle_link(fixture_id: str) -> str:
    return f"/oracle?tab=board&fixture={fixture_id}"


def _fixture(f: Fixture, now: datetime, *, side: str | None = None, team: str | None = None) -> dict[str, Any]:
    return {"fixture_id": f.fixture_id, "home": f.home, "away": f.away, "sport_key": f.sport_key, "family": f.family, "kickoff": f.kickoff.isoformat(),
            "in_play": f.kickoff <= now, "side": side, "team": team, "oracle_link": oracle_link(f.fixture_id)}


def _edges(picks: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """fixture -> its best leg priced above fair at a retail book, and how many are."""
    by_fixture: dict[str, list[Any]] = defaultdict(list)
    for p in picks:
        by_fixture[p.fixture_id].append(p)
    out = {}
    for fixture_id, legs in by_fixture.items():
        positive = sorted((p for p in legs if p.ev > 0), key=lambda p: (-p.ev, p.leg_id))
        best = positive[0] if positive else None
        out[fixture_id] = {
            "valued": len(legs), "positive": len(positive),
            "best": None if best is None else {"leg_id": best.leg_id, "label": best.label, "market": best.market, "book": best.book,
                                               "book_label": ASHOKA_BOOKMAKERS.get(best.book, best.book), "price": best.price,
                                               "fair_probability": round(best.fair_probability, 4), "ev": round(best.ev, 4)},
        }
    return out


def _market(fixture_id: str | None, edges: Mapping[str, dict[str, Any]], recorded: Mapping[str, dict[str, Any]], settings: Settings,
            **measured: Any) -> dict[str, Any]:
    edge = edges.get(fixture_id) if fixture_id else None
    best = (edge or {}).get("best")
    before = recorded.get(bus.edge_key(settings, fixture_id)) if fixture_id else None
    return {**measured, "edge": best, "legs_valued": (edge or {}).get("valued", 0), "legs_above_fair": (edge or {}).get("positive", 0),
            "edge_shift_pts": ib.edge_shift(best["ev"] if best else None, before, best["leg_id"] if best else None),
            "edge_recorded": before}


def _source(name: str, tier: ib.Tier, *, url: str | None = None, credibility: float | None = None, basis: str) -> dict[str, Any]:
    return {"name": name, "url": url, "tier": tier.value, "credibility": credibility, "basis": basis}


def _lineup_views(rows: Sequence[InjuryRosterReport], by_id: Mapping[str, Fixture], consensus: Mapping[str, Mapping[str, float]],
                  settings: Settings) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[InjuryRosterReport]] = defaultdict(list)
    for r in rows:
        grouped[r.fixture_id].append(r)
    return {fid: lineups.view(rs, by_id[fid], settings, consensus.get(fid)) for fid, rs in grouped.items() if fid in by_id}


def _lineup_measure(view: Mapping[str, Any] | None, side: str | None = None) -> dict[str, Any] | None:
    if not view:
        return None
    delta = view["delta"]
    return {"delta_pts": {k: round(100.0 * v, 2) for k, v in delta.items()}, "probabilities": view["probabilities"], "adjusted": view["adjusted"],
            "absences": len(view["absences"]), "unrated": len(view["unrated"]), "side": side}


def _news_item(a: NewsArticleSentiment, by_id: Mapping[str, Fixture], consensus: Mapping[str, Mapping[str, float]],
               views: Mapping[str, dict[str, Any]], edges: Mapping[str, dict[str, Any]], recorded: Mapping[str, dict[str, Any]],
               settings: Settings, now: datetime) -> dict[str, Any]:
    named = [f for f in (a.fixtures or []) if f.get("fixture_id") in by_id]
    subject = a.subject if a.subject and a.subject.get("fixture_id") in by_id else (named[0] if named else None)
    fixture = by_id[subject["fixture_id"]] if subject else None
    fid = fixture.fixture_id if fixture else None
    category = ib.category_of(a.events or [], catalyst=bool(a.catalyst))
    tier = ib.tier_of(a.url, a.source_credibility, official_hosts=settings.WIRE_OFFICIAL_SOURCE_HOSTS, press_min=settings.WIRE_PRESS_MIN_CREDIBILITY)
    move = ib.probability_move((a.baseline_probabilities or {}).get(fid) if fid else None, consensus.get(fid) if fid else None, subject["side"] if subject else None)
    catalyst = None
    if a.catalyst and a.catalyst.get("fixture_id") in by_id:
        c = a.catalyst
        catalyst = {"selection": c["selection"], "before": c["probability_before"], "after": c["probability_after"],
                    "delta_pts": round(100.0 * c["shift"], 2), "latency_seconds": c["latency_seconds"]}
    lineup = _lineup_measure(views.get(fid), subject["side"] if subject else None) if fid and category is ib.Category.LINEUP_INJURY else None
    published = _aware(a.published_at)
    return {
        "id": f"news:{a.id}", "kind": ib.Kind.NEWS.value, "category": category.value,
        "sport": ib.sport_label(fixture.family if fixture else a.sport, settings.WIRE_BOARD_SPORTS),
        "headline": a.title, "summary": ib.two_sentences(a.summary), "impact_level": a.tactical_impact, "events": list(a.events or []),
        "sentiment": a.sentiment_score, "source": _source(a.source, tier, url=a.url, credibility=a.source_credibility, basis="the article"),
        "published_at": published.isoformat(), "_epoch": published.timestamp(),
        "fixture": None if fixture is None else _fixture(fixture, now, side=subject["side"], team=subject.get("team")),
        "also_names": [f["fixture_id"] for f in named if f["fixture_id"] != fid],
        "market": _market(fid, edges, recorded, settings, move=move, catalyst=catalyst, lineup=lineup, steam=None, weather=None),
    }


def _absence_item(fid: str, rows: Sequence[InjuryRosterReport], view: Mapping[str, Any], fixture: Fixture, edges: Mapping[str, dict[str, Any]],
                  recorded: Mapping[str, dict[str, Any]], settings: Settings, now: datetime) -> dict[str, Any]:
    by_status: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        by_status[r.status].append(f"{r.player_name} ({fixture.home if r.side == 'HOME' else fixture.away})")
    summary = ". ".join(f"{status.title()}: {', '.join(names)}" for status, names in sorted(by_status.items()))
    severe = any(r.status in SEVERE for r in rows)
    near = timedelta(0) <= fixture.kickoff - now <= timedelta(hours=settings.WIRE_CRITICAL_WINDOW_HOURS)
    sources = sorted({r.source_name for r in rows})
    updated = max(_aware(r.updated_at or r.reported_at) for r in rows)
    basis = " · ".join("ESPN injury list" if s == "espn" else f"entered by {s.split(':', 1)[1]}" if s.startswith("admin:") else s for s in sources)
    return {
        "id": f"absences:{fid}", "kind": ib.Kind.ABSENCES.value, "category": ib.Category.LINEUP_INJURY.value,
        "sport": ib.sport_label(fixture.family, settings.WIRE_BOARD_SPORTS),
        "headline": f"{len(rows)} absence{'' if len(rows) == 1 else 's'} recorded for {fixture.home} v {fixture.away}",
        "summary": ib.two_sentences(summary + "."), "impact_level": ("CRITICAL" if near else "HIGH") if severe else "MEDIUM", "events": ["ABSENCE"],
        "sentiment": None, "source": _source(", ".join(sources), ib.Tier.MEASURED, basis=basis),
        "published_at": updated.isoformat(), "_epoch": updated.timestamp(), "fixture": _fixture(fixture, now), "also_names": [],
        "market": _market(fid, edges, recorded, settings, move=None, catalyst=None, lineup=_lineup_measure(view), steam=None, weather=None),
    }


def _weather_item(snap: WeatherSnapshot, fixture: Fixture, edges: Mapping[str, dict[str, Any]], recorded: Mapping[str, dict[str, Any]],
                  settings: Settings, now: datetime) -> dict[str, Any]:
    factor = snap.pitch_impact_score
    venue = snap.venue_name or "the venue"
    parts = [f"friction factor {factor:.2f}" if factor is not None else None, "dew expected" if snap.dew_expected else None]
    headline = f"{snap.condition or 'Forecast'} at {venue} for {fixture.home} v {fixture.away}: " + ", ".join(p for p in parts if p)
    detail = [snap.tactical_advisory or "", f"{snap.temp_c:.0f}°C" if snap.temp_c is not None else "",
              f"wind {snap.wind_speed_kmh:.0f} km/h" if snap.wind_speed_kmh is not None else "",
              f"rain {snap.precipitation_mmh:.1f} mm/h" if snap.precipitation_mmh else ""]
    strong = factor is not None and abs(factor - 1.0) >= 2 * settings.WIRE_BOARD_WEATHER_MIN_EFFECT
    fetched = _aware(snap.fetched_at)
    return {
        "id": f"weather:{fixture.fixture_id}", "kind": ib.Kind.WEATHER.value, "category": ib.Category.WEATHER_PITCH.value,
        "sport": ib.sport_label(fixture.family, settings.WIRE_BOARD_SPORTS), "headline": headline,
        "summary": ib.two_sentences(". ".join(d for d in detail if d) + "."), "impact_level": "HIGH" if strong or snap.dew_expected else "MEDIUM",
        "events": ["WEATHER"], "sentiment": None, "source": _source(snap.source or "Open-Meteo", ib.Tier.MEASURED, basis="the venue's forecast for the match window"),
        "published_at": fetched.isoformat(), "_epoch": fetched.timestamp(), "fixture": _fixture(fixture, now), "also_names": [],
        "market": _market(fixture.fixture_id, edges, recorded, settings, move=None, catalyst=None, lineup=None, steam=None,
                          weather={"friction_factor": factor, "dew_expected": bool(snap.dew_expected), "condition": snap.condition,
                                   "kickoff_at": None if snap.kickoff_at is None else _aware(snap.kickoff_at).isoformat()}),
    }


def _steam_item(alert: SteamAlert, fixture: Fixture, edges: Mapping[str, dict[str, Any]], recorded: Mapping[str, dict[str, Any]],
                settings: Settings, now: datetime) -> dict[str, Any]:
    shortened = alert.current_odds < alert.opening_odds
    books = ", ".join(alert.triggering_bookmakers)
    detected = _aware(alert.detected_at)
    big = abs(alert.implied_prob_delta_pct) >= 2 * settings.WIRE_BOARD_STEAM_MIN_PROB_DELTA
    return {
        "id": f"steam:{alert.match_id}|{alert.market_type}|{alert.selection_id}", "kind": ib.Kind.STEAM.value, "category": ib.Category.ODDS_STEAM.value,
        "sport": ib.sport_label(fixture.family, settings.WIRE_BOARD_SPORTS),
        "headline": f"{alert.selection_id} ({alert.market_type}) {'shortened' if shortened else 'drifted'} {alert.opening_odds:.2f} → {alert.current_odds:.2f} in {fixture.home} v {fixture.away}",
        "summary": f"Implied probability {alert.implied_prob_delta_pct:+.1f} points within {settings.WIRE_BOARD_STEAM_WINDOW_MINUTES} minutes, moved by {books or 'the books'}.",
        "impact_level": "HIGH" if big else "MEDIUM", "events": [], "sentiment": None,
        "source": _source(books or "odds ticks", ib.Tier.MEASURED, basis="BetDoc's odds ticks"),
        "published_at": detected.isoformat(), "_epoch": detected.timestamp(), "fixture": _fixture(fixture, now), "also_names": [],
        "market": _market(fixture.fixture_id, edges, recorded, settings, move=None, catalyst=None, lineup=None, weather=None,
                          steam={"selection": alert.selection_id, "market": alert.market_type, "opening_odds": alert.opening_odds,
                                 "current_odds": alert.current_odds, "delta_pts": round(alert.implied_prob_delta_pct, 2), "books": alert.triggering_bookmakers}),
    }


async def _picks(redis: Redis | None, settings: Settings, now: datetime) -> tuple[list[Any], str | None]:
    from app.services import smallcase  # noqa: PLC0415 - the oracle engine is heavy; only the API and the chain build boards

    if redis is None:
        return [], None
    try:
        picks, as_of, _ = await smallcase.scan(redis, settings, now)
    except (RedisError, OSError, TimeoutError):
        return [], None
    return picks, as_of


async def _steam(session: AsyncSession, settings: Settings, ids: list[str], now: datetime) -> list[SteamAlert]:
    if not ids:
        return []
    ticks = await SQLOddsTickRepository(session).fetch_ticks(now - timedelta(minutes=settings.WIRE_BOARD_STEAM_WINDOW_MINUTES), now,
                                                             match_ids=ids, odds_type=OddsType.BACK)
    return SteamDetectorEngine().detect(ticks, now, settings.WIRE_BOARD_STEAM_WINDOW_MINUTES, settings.WIRE_BOARD_STEAM_MIN_PROB_DELTA,
                                        settings.WIRE_BOARD_STEAM_MIN_LINE_SHIFT, settings.WIRE_BOARD_STEAM_MIN_SECONDS)


async def build(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, *, fresh: bool = False) -> dict[str, Any]:
    """Every item, ordered (fixture-linked first, then by impact, newest first), with the counts the filters show."""
    if not fresh and redis is not None:
        try:
            cached = await redis.get(_key(settings))
        except (RedisError, OSError):
            cached = None
        if cached:
            return json.loads(cached)
    fixtures = await tracked(redis, settings, now)
    by_id = {f.fixture_id: f for f in fixtures}
    ids = list(by_id)
    async with sessions() as session:
        articles = list((await session.execute(select(NewsArticleSentiment).where(NewsArticleSentiment.published_at >= now - timedelta(hours=settings.WIRE_BOARD_HOURS))
                                                .order_by(NewsArticleSentiment.published_at.desc()).limit(ARTICLE_CAP))).scalars())
        absences = list((await session.execute(select(InjuryRosterReport).where(InjuryRosterReport.fixture_id.in_(ids), InjuryRosterReport.is_active.is_(True))
                                                .order_by(InjuryRosterReport.fixture_id, InjuryRosterReport.side, InjuryRosterReport.player_name))).scalars()) if ids else []
        snaps = await weather.latest(session, ids)
        steam = [s for s in await _steam(session, settings, ids, now) if s.match_id in by_id]
    named = {f["fixture_id"] for a in articles for f in (a.fixtures or []) if f.get("fixture_id") in by_id}
    named |= {r.fixture_id for r in absences} | set(snaps) | {s.match_id for s in steam}
    consensus = await news.consensus(redis, settings, named, now) if named else {}
    picks, market_as_of = await _picks(redis, settings, now)
    edges = _edges(picks)
    recorded = await bus.read_values(redis, [bus.edge_key(settings, fid) for fid in sorted(named)])
    views = _lineup_views(absences, by_id, consensus, settings)
    rows_by_fixture: dict[str, list[InjuryRosterReport]] = defaultdict(list)
    for r in absences:
        rows_by_fixture[r.fixture_id].append(r)

    items = [_news_item(a, by_id, consensus, views, edges, recorded, settings, now) for a in articles]
    items += [_absence_item(fid, rows, views[fid], by_id[fid], edges, recorded, settings, now) for fid, rows in rows_by_fixture.items() if fid in views]
    items += [_weather_item(s, by_id[fid], edges, recorded, settings, now) for fid, s in snaps.items()
              if fid in by_id and ib.weather_is_news(s.pitch_impact_score, s.dew_expected, settings.WIRE_BOARD_WEATHER_MIN_EFFECT)]
    items += [_steam_item(s, by_id[s.match_id], edges, recorded, settings, now) for s in steam]
    items.sort(key=ib.order_key)
    for item in items:
        item.pop("_epoch", None)
    board = {"as_of": now.isoformat(), "market_as_of": market_as_of, "tracked_fixtures": len(fixtures), "items": items,
             "sports": list(settings.WIRE_BOARD_SPORTS), "categories": [c.value for c in ib.Category],
             "policy": {"hours": settings.WIRE_BOARD_HOURS, "steam_window_minutes": settings.WIRE_BOARD_STEAM_WINDOW_MINUTES,
                        "press_min_credibility": settings.WIRE_PRESS_MIN_CREDIBILITY, "weather_min_effect": settings.WIRE_BOARD_WEATHER_MIN_EFFECT,
                        "catalyst_impacts": list(settings.WIRE_CATALYST_IMPACTS), "refresh_seconds": settings.WIRE_BOARD_CACHE_SECONDS}}
    if redis is not None:
        try:
            await redis.set(_key(settings), json.dumps(board, default=str), ex=settings.WIRE_BOARD_CACHE_SECONDS)
        except (RedisError, OSError):
            logger.debug("VIDUR: board not cached")
    return board


def view(board: Mapping[str, Any], *, sport: str | None, category: str | None, query: str | None, linked_only: bool, limit: int) -> dict[str, Any]:
    """The board through the filters, with the counts each filter chip would show (before the sport and category filters)."""
    base = [i for i in board["items"] if ib.matches(i, sport=None, category=None, query=query, linked_only=linked_only)]
    shown = [i for i in base if ib.matches(i, sport=sport, category=category, query=None, linked_only=False)]
    by_sport: dict[str, int] = defaultdict(int)
    by_category: dict[str, int] = defaultdict(int)
    for i in base:
        if i.get("sport"):
            by_sport[i["sport"]] += 1
        by_category[i["category"]] += 1
    return {**{k: v for k, v in board.items() if k != "items"}, "items": shown[:limit], "total": len(shown),
            "counts": {"ALL": len(base), "by_sport": dict(by_sport), "by_category": dict(by_category)}}


def find(board: Mapping[str, Any], item_id: str) -> dict[str, Any] | None:
    return next((i for i in board["items"] if i["id"] == item_id), None)
