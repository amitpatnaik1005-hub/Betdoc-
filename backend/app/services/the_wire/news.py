"""The news scan, every ``WIRE_NEWS_SCAN_MINUTES``: ingest, read, alert, and catch the market following.

1. Fetch the RSS feeds (``WIRE_NEWS_FEEDS``) and newsapi.org's sports headlines (with a key); drop what is older
   than ``WIRE_NEWS_MAX_AGE_HOURS`` or already stored (one row per URL).
2. Read each article (``app/domain/the_wire/sentiment_engine.py``) against the tracked fixtures: polarity, events,
   tactical impact, credibility, the fixtures and recorded absentees it names.
3. For an article at a catalyst impact naming a fixture, record the fixture's de-vigged Match Odds consensus now.
4. News at an alerting impact (``WIRE_ALERT_IMPACTS``) on a tracked fixture pages the Sentinel (WIRE_BREAKING_NEWS).
5. Every recent article with a recorded consensus is re-measured: a qualifying move the news's way inside the window
   makes it a catalyst (``app/domain/the_wire/steam_catalyst.py``), recorded and paged (WIRE_STEAM_CATALYST).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.wire import newsapi
from app.core.config import Settings
from app.domain.oracle.markets import MarketKind
from app.domain.the_wire import sentiment_engine as se
from app.domain.the_wire import steam_catalyst as sc
from app.domain.the_wire.live_providers import RssNewsProvider
from app.models.sentinel import Severity
from app.models.the_wire import InjuryRosterReport, NewsArticleSentiment
from app.schemas.the_wire import NewsItem, SteamCatalystAlert
from app.services.sentinel_bus import AlertKind, SentinelAlert, emit_alert
from app.services.the_wire import live
from app.services.the_wire.fixtures import Fixture, tracked

logger = logging.getLogger("betdoc.vidur.news")


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)  # SQLite returns naive UTC


def fingerprint(url: str) -> str:
    return hashlib.sha256(url.strip().casefold().rstrip("/").encode()).hexdigest()


async def fetch(settings: Settings, now: datetime, http: httpx.AsyncClient | None = None) -> list[NewsItem]:
    rss = RssNewsProvider(settings.WIRE_NEWS_FEEDS)
    own = http is None
    http = http or httpx.AsyncClient(timeout=settings.WIRE_HTTP_TIMEOUT_SECONDS, headers={"User-Agent": settings.WIRE_USER_AGENT}, follow_redirects=True)
    try:
        results = await asyncio.gather(rss.fetch_latest_news(), newsapi.headlines(http, settings, now), return_exceptions=True)
    finally:
        if own:
            await http.aclose()
    items: list[NewsItem] = []
    for label, result in zip(("rss", "newsapi"), results, strict=True):
        if isinstance(result, BaseException):
            logger.warning("VIDUR: %s news unavailable (%s)", label, type(result).__name__)
            continue
        items.extend(result)
    return items


async def consensus(redis: Redis | None, settings: Settings, fixture_ids: Iterable[str], now: datetime) -> dict[str, dict[str, float]]:
    """fixture -> HOME / DRAW / AWAY -> the de-vigged consensus probability on the live board."""
    from app.services.ashoka_market import load_candidates  # noqa: PLC0415 - the oracle engine is heavy
    from app.services.twin.vetting import LIVE_LOOKBACK  # noqa: PLC0415

    ids = set(fixture_ids)
    if redis is None or not ids:
        return {}
    try:
        legs, _ = await load_candidates(redis, settings, now - LIVE_LOOKBACK, fixtures=ids)
    except (RedisError, OSError, TimeoutError):
        return {}
    out: dict[str, dict[str, float]] = {}
    for leg in legs:
        if leg.market.kind is MarketKind.MATCH_ODDS and leg.consensus is not None:
            out.setdefault(leg.fixture_id, {})[leg.selection] = round(float(leg.consensus), 4)
    return out


def article_dict(a: NewsArticleSentiment) -> dict[str, Any]:
    return {"id": str(a.id), "title": a.title, "summary": a.summary, "source": a.source, "url": a.url, "source_credibility": a.source_credibility, "sport": a.sport,
            "teams_mentioned": a.teams_mentioned, "players_mentioned": a.players_mentioned, "fixtures": a.fixtures, "subject": a.subject, "events": a.events,
            "sentiment_score": a.sentiment_score, "tactical_impact": a.tactical_impact, "associated_steam_move_id": a.associated_steam_move_id,
            "catalyst": a.catalyst, "latency_seconds": a.latency_seconds, "published_at": _aware(a.published_at).isoformat(), "ingested_at": _aware(a.ingested_at).isoformat()}


def news_item(a: NewsArticleSentiment) -> NewsItem:
    return NewsItem(id=str(a.id), source=a.source, title=a.title, summary=a.summary, url=a.url, published_at=_aware(a.published_at), sentiment_score=a.sentiment_score,
                    tactical_impact=a.tactical_impact, source_credibility=a.source_credibility,  # type: ignore[arg-type]
                    fixture_ids=[f["fixture_id"] for f in a.fixtures or []], associated_steam_move_id=a.associated_steam_move_id)


def catalyst_alert(a: NewsArticleSentiment) -> SteamCatalystAlert | None:
    c = a.catalyst
    if not c:
        return None
    return SteamCatalystAlert(article_id=str(a.id), headline=a.title, match_id=c["fixture_id"], selection=c["selection"], probability_before=c["probability_before"],
                              probability_after=c["probability_after"], shift_pct=round(100 * c["shift"], 2), latency_seconds=c["latency_seconds"],
                              tactical_impact=a.tactical_impact, source_credibility=a.source_credibility, published_at=_aware(a.published_at))


async def _absentees(session: AsyncSession, fixture_ids: list[str]) -> dict[str, list[str]]:
    if not fixture_ids:
        return {}
    rows = (await session.execute(select(InjuryRosterReport.fixture_id, InjuryRosterReport.player_name).where(
        InjuryRosterReport.fixture_id.in_(fixture_ids), InjuryRosterReport.is_active.is_(True)))).all()
    out: dict[str, list[str]] = {}
    for fixture_id, player in rows:
        out.setdefault(fixture_id, []).append(player)
    return out


def _alert(kind: AlertKind, a: NewsArticleSentiment, title: str, body: str, detail: dict[str, Any]) -> SentinelAlert:
    return SentinelAlert(kind=kind, severity=Severity.WARNING, source="vidur", title=title[:200], body=body[:4000],
                         dedupe_key=f"vidur:{kind.value}:{a.fingerprint[:24]}", detail={"article_id": str(a.id), "url": a.url, **detail})


async def ingest(session: AsyncSession, redis: Redis | None, settings: Settings, items: Sequence[NewsItem], fixtures: Sequence[Fixture], now: datetime,
                 developer: str) -> tuple[list[NewsArticleSentiment], list[SentinelAlert]]:
    fresh = [i for i in items if now - i.published_at <= timedelta(hours=settings.WIRE_NEWS_MAX_AGE_HOURS)]
    by_print = {fingerprint(i.url): i for i in fresh}
    if not by_print:
        return [], []
    seen = set((await session.execute(select(NewsArticleSentiment.fingerprint).where(NewsArticleSentiment.fingerprint.in_(list(by_print))))).scalars())
    rows = [f.row for f in fixtures]
    families = {f.fixture_id: f.family for f in fixtures}
    window, medium = timedelta(hours=settings.WIRE_CRITICAL_WINDOW_HOURS), settings.WIRE_SENTIMENT_MEDIUM
    readings = {fp: se.read(i.title, i.summary, rows, now, critical_window=window, medium=medium) for fp, i in by_print.items() if fp not in seen}
    named = sorted({m.fixture_id for r in readings.values() for m in r.mentions})
    absentees = await _absentees(session, named)
    wants_baseline = {m.fixture_id for r in readings.values() if r.impact.value in settings.WIRE_CATALYST_IMPACTS for m in r.mentions}
    probs = await consensus(redis, settings, wants_baseline, now)
    created, alerts = [], []
    for fp, reading in readings.items():
        item = by_print[fp]
        fixture_ids = list(dict.fromkeys(m.fixture_id for m in reading.mentions))
        text = f" {' '.join(se.tokens(item.title + ' ' + item.summary))} "
        players = sorted({p for f in fixture_ids for p in absentees.get(f, []) if f" {' '.join(se.tokens(p))} " in text})
        row = NewsArticleSentiment(
            id=uuid.uuid4(), fingerprint=fp, title=item.title[:512], summary=item.summary, source=item.source[:128], url=item.url[:1024],
            source_credibility=se.credibility(item.url, None, settings.WIRE_SOURCE_CREDIBILITY, settings.WIRE_SOURCE_DEFAULT_CREDIBILITY),
            sport=families.get(fixture_ids[0]) if fixture_ids else None, teams_mentioned=sorted({m.team for m in reading.mentions}), players_mentioned=players,
            fixtures=[{"fixture_id": m.fixture_id, "team": m.team, "side": m.side} for m in reading.mentions],
            subject=None if reading.subject is None else {"fixture_id": reading.subject.fixture_id, "team": reading.subject.team, "side": reading.subject.side},
            events=[e.value for e in reading.events], sentiment_score=reading.sentiment, tactical_impact=reading.impact.value,
            baseline_probabilities={f: probs[f] for f in fixture_ids if f in probs} if reading.impact.value in settings.WIRE_CATALYST_IMPACTS else {},
            published_at=item.published_at, ingested_at=now,
        )
        if fixture_ids and reading.impact.value in settings.WIRE_ALERT_IMPACTS:
            row.alerted = True
            alerts.append(_alert(AlertKind.WIRE_BREAKING_NEWS, row, f"VIDUR {reading.impact.value}: {item.title}",
                                 f"{item.source} (credibility {row.source_credibility:.2f}): {item.summary[:600]}\nFixtures: {', '.join(row.teams_mentioned)}\nDeveloper: {developer}",
                                 {"impact": reading.impact.value, "fixtures": fixture_ids, "sentiment": reading.sentiment}))
        session.add(row)
        created.append(row)
    await session.flush()
    return created, alerts


async def catalysts(session: AsyncSession, redis: Redis | None, settings: Settings, now: datetime, developer: str) -> tuple[list[NewsArticleSentiment], list[SentinelAlert]]:
    since = now - timedelta(seconds=settings.WIRE_CATALYST_WINDOW_SECONDS)
    open_rows = [a for a in (await session.execute(select(NewsArticleSentiment).where(
        NewsArticleSentiment.published_at >= since, NewsArticleSentiment.catalyst.is_(None),
        NewsArticleSentiment.tactical_impact.in_(list(settings.WIRE_CATALYST_IMPACTS))))).scalars() if a.baseline_probabilities]
    if not open_rows:
        return [], []
    current = await consensus(redis, settings, {f for a in open_rows for f in a.baseline_probabilities}, now)
    found, alerts = [], []
    for a in open_rows:
        sides = {f["fixture_id"]: f["side"] for f in a.fixtures or []}
        if a.subject:
            sides[a.subject["fixture_id"]] = a.subject["side"]
        for fixture_id, before in a.baseline_probabilities.items():
            hit = sc.evaluate(published_at=_aware(a.published_at), observed_at=now, impact=a.tactical_impact, sentiment=a.sentiment_score, side=sides.get(fixture_id),
                              before=before, after=current.get(fixture_id, {}), window_seconds=settings.WIRE_CATALYST_WINDOW_SECONDS,
                              min_shift=settings.WIRE_CATALYST_MIN_PROB_SHIFT, impacts=settings.WIRE_CATALYST_IMPACTS)
            if hit is None:
                continue
            a.catalyst = {"fixture_id": fixture_id, **hit.as_dict()}
            a.associated_steam_move_id = f"{fixture_id}|Match Odds|{hit.selection}"[:160]
            a.latency_seconds = hit.latency_seconds
            found.append(a)
            alerts.append(_alert(AlertKind.WIRE_STEAM_CATALYST, a, f"VIDUR catalyst: {a.title}",
                                 f"{hit.selection} {hit.probability_before:.1%} -> {hit.probability_after:.1%} ({hit.shift:+.1%}) within {hit.latency_seconds:.0f}s of "
                                 f"{a.source}'s report.\nDeveloper: {developer}", {"fixture_id": fixture_id, **hit.as_dict()}))
            break
    await session.flush()
    return found, alerts


async def scan(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, *,
               items: Sequence[NewsItem] | None = None, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    from app.services.twin.vetting import developer_credit  # noqa: PLC0415 - pulls in the oracle engine

    items = items if items is not None else await fetch(settings, now, http)
    fixtures = await tracked(redis, settings, now)
    async with sessions() as session:
        developer = await developer_credit(session)
        created, alerts = await ingest(session, redis, settings, items, fixtures, now, developer)
        found, more = await catalysts(session, redis, settings, now, developer)
        await session.commit()
    for alert in alerts + more:
        await emit_alert(redis, settings, alert)
    frames = [{"type": "news", **news_item(a).model_dump(mode="json")} for a in created]
    frames += [{"type": "catalyst", **c.model_dump(mode="json")} for a in found if (c := catalyst_alert(a)) is not None]
    await live.publish(redis, settings, frames)
    return {"fetched": len(items), "ingested": len(created), "alerts": len(alerts), "catalysts": len(found), "fixtures": len(fixtures)}
