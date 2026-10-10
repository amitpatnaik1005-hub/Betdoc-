"""The dashboard's providers (Group 78), plugged into the Group 39 aggregator (``app/domain/the_wire/aggregator.py``).

    StoredNewsProvider     the scored articles of ``wire_news_sentiment_articles``; before the first scan has stored
                           any, the live RSS headlines read in memory (sentiment, impact, credibility; no fixtures)
    EspnScoreProvider      The Odds API's scores, with ESPN's (score, clock, status line) wherever the sync paired the
                           fixture with an ESPN event
    SnapshotWeatherProvider  the latest ``wire_weather_snapshots`` row per fixture: what the fortress was given

They are built per request around the request's session factory and Redis; the aggregator's caches stay process-wide.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.the_wire import sentiment_engine as se
from app.domain.the_wire.live_providers import OddsApiScoreProvider, RssNewsProvider
from app.models.the_wire import NewsArticleSentiment
from app.schemas.the_wire import MatchScore, NewsItem, WeatherReport
from app.services.the_wire import news, weather
from app.services.the_wire.espn_sync import read_links


class StoredNewsProvider:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings, rss: RssNewsProvider) -> None:
        self._sessions, self._settings, self._rss = sessions, settings, rss

    async def fetch_latest_news(self) -> list[NewsItem]:
        s, now = self._settings, datetime.now(UTC)
        async with self._sessions() as session:
            rows = list((await session.execute(
                select(NewsArticleSentiment).where(NewsArticleSentiment.published_at >= now - timedelta(hours=s.WIRE_NEWS_MAX_AGE_HOURS))
                .order_by(NewsArticleSentiment.published_at.desc()).limit(s.WIRE_NEWS_LIMIT))).scalars())
        if rows:
            return [news.news_item(r) for r in rows]
        out = []
        for item in (await self._rss.fetch_latest_news())[: s.WIRE_NEWS_LIMIT]:
            reading = se.read(item.title, item.summary, (), now, critical_window=timedelta(hours=s.WIRE_CRITICAL_WINDOW_HOURS), medium=s.WIRE_SENTIMENT_MEDIUM)
            out.append(item.model_copy(update={"sentiment_score": reading.sentiment, "tactical_impact": reading.impact.value,
                                               "source_credibility": se.credibility(item.url, None, s.WIRE_SOURCE_CREDIBILITY, s.WIRE_SOURCE_DEFAULT_CREDIBILITY)}))
        return out


class EspnScoreProvider:
    def __init__(self, odds: OddsApiScoreProvider, redis: Redis | None, settings: Settings) -> None:
        self._odds, self._redis, self._settings = odds, redis, settings

    async def fetch_scores(self, match_ids: Sequence[str]) -> list[MatchScore]:
        base = {s.match_id: s for s in await self._odds.fetch_scores(match_ids)}
        links = await read_links(self._redis, self._settings, list(match_ids))
        out = []
        for match_id in match_ids:
            link = links.get(match_id)
            odds = base.get(match_id)
            if link is not None:
                out.append(MatchScore(match_id=match_id, home_team=odds.home_team if odds else link["home"], away_team=odds.away_team if odds else link["away"],
                                      home_score=max(0, int(link["home_score"])), away_score=max(0, int(link["away_score"])), status=link["status"],
                                      clock=link.get("clock"), detail=link.get("detail"), source="espn"))
            elif odds is not None:
                out.append(odds.model_copy(update={"source": "odds-api"}))
        return out


class SnapshotWeatherProvider:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def fetch_weather(self, match_ids: Sequence[str]) -> list[WeatherReport]:
        async with self._sessions() as session:
            rows = await weather.latest(session, list(match_ids))
        return [weather.report_of(r) for r in rows.values()]
