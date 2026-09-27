from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Final, TypeVar

from app.domain.the_wire.cache import CoalescingTTLCache
from app.domain.the_wire.providers import NewsProvider, ScoreProvider, WeatherProvider
from app.schemas.the_wire import MatchScore, NewsItem, WeatherReport, WireDashboard

logger = logging.getLogger("betdoc.the_wire.aggregator")

SCORES_TTL_SECONDS: Final[float] = 15.0
WEATHER_TTL_SECONDS: Final[float] = 1800.0
NEWS_TTL_SECONDS: Final[float] = 300.0
NEGATIVE_TTL_SECONDS: Final[float] = 10.0
NEWS_CACHE_KEY: Final[str] = "news:latest"

# Process-global caches (one per domain, shared by every service instance).
SCORE_CACHE: CoalescingTTLCache[str, MatchScore] = CoalescingTTLCache(
    "scores", negative_ttl_seconds=NEGATIVE_TTL_SECONDS
)
WEATHER_CACHE: CoalescingTTLCache[str, WeatherReport] = CoalescingTTLCache(
    "weather", negative_ttl_seconds=NEGATIVE_TTL_SECONDS
)
NEWS_CACHE: CoalescingTTLCache[str, list[NewsItem]] = CoalescingTTLCache(
    "news", max_entries=16, negative_ttl_seconds=NEGATIVE_TTL_SECONDS
)

M = TypeVar("M", MatchScore, WeatherReport)
T = TypeVar("T")


def _index_by_match_id(rows: object, model: type[M], requested: set[str], label: str) -> dict[str, M]:
    """Provider boundary guard: accept only validated models for requested IDs."""
    if not isinstance(rows, list):
        raise TypeError(f"{label} provider returned {type(rows).__name__}, expected list")
    indexed: dict[str, M] = {}
    for row in rows:
        if not isinstance(row, model):
            logger.error("%s provider leaked non-%s row: %r", label, model.__name__, type(row))
            continue
        if row.match_id in requested:
            indexed[row.match_id] = row
    return indexed


def _unwrap(result: T | BaseException, default: T, label: str) -> T:
    if isinstance(result, Exception):
        logger.error("The Wire: %s aggregation failed; serving partial data", label, exc_info=result)
        return default
    if isinstance(result, BaseException):
        raise result
    return result


class WireAggregatorService:
    def __init__(
        self,
        news_provider: NewsProvider,
        score_provider: ScoreProvider,
        weather_provider: WeatherProvider,
        *,
        score_cache: CoalescingTTLCache[str, MatchScore] | None = None,
        weather_cache: CoalescingTTLCache[str, WeatherReport] | None = None,
        news_cache: CoalescingTTLCache[str, list[NewsItem]] | None = None,
    ) -> None:
        self._news_provider = news_provider
        self._score_provider = score_provider
        self._weather_provider = weather_provider
        # `is not None` (not `or`): an empty cache is falsy because it defines __len__.
        self._score_cache = score_cache if score_cache is not None else SCORE_CACHE
        self._weather_cache = weather_cache if weather_cache is not None else WEATHER_CACHE
        self._news_cache = news_cache if news_cache is not None else NEWS_CACHE

    async def fetch_dashboard(self, match_ids: Sequence[str]) -> WireDashboard:
        ids = list(dict.fromkeys(m for m in match_ids if m))
        if not ids:
            # Short-circuit: no score/weather cache or provider calls for empty input.
            news = _unwrap((await asyncio.gather(self._fetch_news(), return_exceptions=True))[0], [], "news")
            return WireDashboard(news=news, scores=[], weather={})

        news_res, scores_res, weather_res = await asyncio.gather(
            self._fetch_news(),
            self._fetch_scores(ids),
            self._fetch_weather(ids),
            return_exceptions=True,
        )
        return WireDashboard(
            news=_unwrap(news_res, [], "news"),
            scores=_unwrap(scores_res, [], "scores"),
            weather=_unwrap(weather_res, {}, "weather"),
        )

    # ----------------------------------------------------------------- news
    async def _fetch_news(self) -> list[NewsItem]:
        news = await self._news_cache.get_or_fetch(NEWS_CACHE_KEY, NEWS_TTL_SECONDS, self._load_news)
        return list(news) if news else []

    async def _load_news(self) -> list[NewsItem]:
        rows = await self._news_provider.fetch_latest_news()
        if not isinstance(rows, list):
            raise TypeError(f"news provider returned {type(rows).__name__}, expected list")
        items = [r for r in rows if isinstance(r, NewsItem)]
        if len(items) != len(rows):
            logger.error("news provider leaked %d non-NewsItem rows", len(rows) - len(items))
        return sorted(items, key=lambda n: n.published_at, reverse=True)

    # --------------------------------------------------------------- scores
    async def _fetch_scores(self, ids: list[str]) -> list[MatchScore]:
        cached = await self._score_cache.get_many_or_fetch(ids, SCORES_TTL_SECONDS, self._bulk_scores)
        return [s for s in (cached.get(m) for m in ids) if s is not None]

    async def _bulk_scores(self, missing: list[str]) -> dict[str, MatchScore]:
        rows = await self._score_provider.fetch_scores(missing)
        return _index_by_match_id(rows, MatchScore, set(missing), "score")

    # -------------------------------------------------------------- weather
    async def _fetch_weather(self, ids: list[str]) -> dict[str, WeatherReport]:
        cached = await self._weather_cache.get_many_or_fetch(ids, WEATHER_TTL_SECONDS, self._bulk_weather)
        return {m: w for m in ids if (w := cached.get(m)) is not None}

    async def _bulk_weather(self, missing: list[str]) -> dict[str, WeatherReport]:
        rows = await self._weather_provider.fetch_weather(missing)
        return _index_by_match_id(rows, WeatherReport, set(missing), "weather")
