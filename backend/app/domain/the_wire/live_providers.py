"""Live Wire providers: real sports news (RSS) and real scores (The Odds API).

Venue weather comes from the Wire's own scan since Group 78 (``app/services/the_wire/weather.py``: venues from the
seed, ESPN and Open-Meteo's geocoder, forecasts from Open-Meteo); the dashboard reads its snapshots through
``app/services/the_wire/providers.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from app.schemas.the_wire import MatchScore, NewsItem

logger = logging.getLogger("betdoc.vidur.wire")

HTTP_TIMEOUT_SECONDS = 8.0
MAX_NEWS_ITEMS = 40
# The Odds API charges quota per scores call; one fetch per sport every two minutes is plenty.
SCORES_PROVIDER_TTL_SECONDS = 120.0
_TAG_RE = re.compile(r"<[^>]+>")


def _text(node: ElementTree.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _plain(raw: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", raw)).split())


class RssNewsProvider:
    """Sports headlines from public RSS 2.0 feeds (no API key needed)."""

    def __init__(self, feeds: Sequence[str]) -> None:
        self._feeds = tuple(feeds)

    async def fetch_latest_news(self) -> list[NewsItem]:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": "BetDoc-Wire/1.0"}) as client:
            batches = await asyncio.gather(*(self._feed(client, url) for url in self._feeds), return_exceptions=True)
        items: dict[str, NewsItem] = {}
        for url, batch in zip(self._feeds, batches, strict=True):
            if isinstance(batch, BaseException):
                logger.warning("VIDUR: news feed %s unavailable: %s", url, batch)
                continue
            for item in batch:
                items.setdefault(item.id, item)
        return sorted(items.values(), key=lambda n: n.published_at, reverse=True)[:MAX_NEWS_ITEMS]

    async def _feed(self, client: httpx.AsyncClient, url: str) -> list[NewsItem]:
        response = await client.get(url)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
        channel = root.find("channel")
        source = _text(channel.find("title")) if channel is not None else url
        out: list[NewsItem] = []
        for node in root.iter("item"):
            link = _text(node.find("link"))
            title = _plain(_text(node.find("title")))
            if not link or not title:
                continue
            try:
                published = parsedate_to_datetime(_text(node.find("pubDate"))).astimezone(UTC)
            except (TypeError, ValueError):
                published = datetime.now(UTC)
            out.append(
                NewsItem(
                    id=hashlib.sha1(link.encode()).hexdigest()[:16],  # noqa: S324 - identifier, not security
                    source=source or "RSS",
                    title=title,
                    summary=_plain(_text(node.find("description")))[:400],
                    url=link,
                    published_at=published,
                )
            )
        return out


class OddsApiScoreProvider:
    """Live and recent results for the fixtures the odds poller tracks (same event ids)."""

    def __init__(self, api_key: str | None, base_url: str, sport_keys: Sequence[str]) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._sport_keys = tuple(sport_keys)
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._lock = asyncio.Lock()

    async def fetch_scores(self, match_ids: Sequence[str]) -> list[MatchScore]:
        if not self._api_key or not self._sport_keys:
            return []
        wanted = set(match_ids)
        events: list[dict] = []
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            for sport in self._sport_keys:
                events.extend(await self._sport_events(client, sport))
        scores = [s for s in (self._to_score(e) for e in events) if s is not None]
        return [s for s in scores if not wanted or s.match_id in wanted]

    async def _sport_events(self, client: httpx.AsyncClient, sport: str) -> list[dict]:
        async with self._lock:
            hit = self._cache.get(sport)
            if hit and time.monotonic() - hit[0] < SCORES_PROVIDER_TTL_SECONDS:
                return hit[1]
            try:
                response = await client.get(f"{self._base_url}/sports/{sport}/scores", params={"apiKey": self._api_key, "daysFrom": 1})
                response.raise_for_status()
                events = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                # Never log the URL: it carries the API key as a query parameter.
                logger.warning("VIDUR: scores unavailable for %s (%s)", sport, type(exc).__name__)
                return hit[1] if hit else []
            self._cache[sport] = (time.monotonic(), events)
            return events

    @staticmethod
    def _to_score(event: dict) -> MatchScore | None:
        try:
            home, away = event["home_team"], event["away_team"]
            by_team = {s["name"]: s["score"] for s in (event.get("scores") or [])}
            started = bool(by_team)
            status = "FT" if event.get("completed") else "LIVE" if started else "SCHEDULED"
            return MatchScore(
                match_id=event["id"],
                home_team=home,
                away_team=away,
                home_score=int(float(by_team.get(home, 0) or 0)),
                away_score=int(float(by_team.get(away, 0) or 0)),
                status=status,
                clock=None,
            )
        except (KeyError, TypeError, ValueError):
            return None
