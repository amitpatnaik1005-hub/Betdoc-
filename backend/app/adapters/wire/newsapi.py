"""newsapi.org top sports headlines (Group 78), when ``NEWSAPI_ORG_API_KEY`` is set. The key travels in a header,
never the URL, and never reaches a log line."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import httpx

from app.core.config import Settings
from app.schemas.the_wire import NewsItem


def parse(payload: dict, now: datetime) -> list[NewsItem]:
    out: list[NewsItem] = []
    for row in payload.get("articles") or []:
        url, title = (row.get("url") or "").strip(), (row.get("title") or "").strip()
        if not url or not title or title == "[Removed]":
            continue
        try:
            published = datetime.fromisoformat(str(row.get("publishedAt")).replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            published = now
        out.append(NewsItem(id=hashlib.sha1(url.encode()).hexdigest()[:16], source=((row.get("source") or {}).get("name") or "NewsAPI")[:64],  # noqa: S324 - an identifier
                            title=title, summary=(row.get("description") or "")[:400], url=url, published_at=published))
    return out


async def headlines(http: httpx.AsyncClient, settings: Settings, now: datetime) -> list[NewsItem]:
    if settings.NEWSAPI_ORG_API_KEY is None:
        return []
    response = await http.get(settings.WIRE_NEWSAPI_URL, params=settings.WIRE_NEWSAPI_PARAMS, headers={"X-Api-Key": settings.NEWSAPI_ORG_API_KEY.get_secret_value()})
    response.raise_for_status()
    return parse(response.json(), now)
