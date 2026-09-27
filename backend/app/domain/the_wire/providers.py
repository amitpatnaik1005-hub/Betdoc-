from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from app.schemas.the_wire import MatchScore, NewsItem, WeatherReport


@runtime_checkable
class NewsProvider(Protocol):
    async def fetch_latest_news(self) -> list[NewsItem]: ...


@runtime_checkable
class ScoreProvider(Protocol):
    async def fetch_scores(self, match_ids: Sequence[str]) -> list[MatchScore]: ...


@runtime_checkable
class WeatherProvider(Protocol):
    async def fetch_weather(self, match_ids: Sequence[str]) -> list[WeatherReport]: ...


def _seeded_rng(*parts: str) -> random.Random:
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


_TEAMS: tuple[str, ...] = (
    "Arsenal", "Chelsea", "Liverpool", "Man City", "Tottenham", "Newcastle",
    "Real Madrid", "Barcelona", "Atletico", "Bayern", "Dortmund", "Leipzig",
    "Inter", "Milan", "Juventus", "Napoli", "PSG", "Marseille", "Benfica", "Porto",
)
_HEADLINES: tuple[tuple[str, str], ...] = (
    ("Star striker ruled out with hamstring injury", "Medical staff expect a three-week absence."),
    ("Manager confirms rotation ahead of cup tie", "Several first-team regulars will be rested."),
    ("Heavy rain forecast for weekend fixtures", "Pitch inspections scheduled for Saturday morning."),
    ("Captain returns to full training", "The midfielder is in contention for selection."),
    ("Sharp money moves on Sunday derby", "Exchange liquidity spiked overnight on the away side."),
    ("Goalkeeper suspended after red card appeal fails", "The backup keeper will start the next two games."),
    ("Club confirms new head coach appointment", "The incoming coach favours a high-press system."),
)
_SOURCES: tuple[str, ...] = ("Wire Sports", "Pitchside Daily", "Matchday Insider", "Touchline Report")
_CONDITIONS: tuple[str, ...] = ("CLEAR", "CLOUDY", "LIGHT_RAIN", "HEAVY_RAIN", "WINDY", "SNOW", "FOG")


class MockNewsProvider:
    latency_seconds: float = 0.2
    rotation_minutes: int = 5

    async def fetch_latest_news(self) -> list[NewsItem]:
        await asyncio.sleep(self.latency_seconds)
        now = datetime.now(timezone.utc)
        bucket = str(int(now.timestamp()) // (self.rotation_minutes * 60))
        rng = _seeded_rng("news", bucket)
        picks = rng.sample(_HEADLINES, k=rng.randint(3, 5))
        items: list[NewsItem] = []
        for idx, (title, summary) in enumerate(picks):
            item_id = hashlib.sha256(f"{bucket}|{title}".encode()).hexdigest()[:16]
            items.append(
                NewsItem(
                    id=item_id,
                    source=rng.choice(_SOURCES),
                    title=title,
                    summary=summary,
                    url=f"https://news.example.com/articles/{item_id}",
                    published_at=now - timedelta(minutes=5 * idx + rng.randint(0, 4)),
                )
            )
        return items


class MockScoreProvider:
    latency_seconds: float = 0.15

    async def fetch_scores(self, match_ids: Sequence[str]) -> list[MatchScore]:
        await asyncio.sleep(self.latency_seconds)
        scores: list[MatchScore] = []
        for match_id in dict.fromkeys(match_ids):
            rng = _seeded_rng("score", match_id)
            home, away = rng.sample(_TEAMS, k=2)
            status = rng.choices(("SCHEDULED", "LIVE", "HT", "FT"), weights=(3, 4, 1, 2))[0]
            if status == "SCHEDULED":
                home_score, away_score, clock = 0, 0, None
            else:
                home_score, away_score = rng.randint(0, 4), rng.randint(0, 3)
                clock = f"{rng.randint(1, 90)}'" if status == "LIVE" else ("HT" if status == "HT" else None)
            scores.append(
                MatchScore(
                    match_id=match_id, home_team=home, away_team=away,
                    home_score=home_score, away_score=away_score, status=status, clock=clock,
                )
            )
        return scores


class MockWeatherProvider:
    latency_seconds: float = 0.3

    async def fetch_weather(self, match_ids: Sequence[str]) -> list[WeatherReport]:
        await asyncio.sleep(self.latency_seconds)
        reports: list[WeatherReport] = []
        for match_id in dict.fromkeys(match_ids):
            rng = _seeded_rng("weather", match_id)
            reports.append(
                WeatherReport(
                    match_id=match_id,
                    temperature_c=round(rng.uniform(-5.0, 35.0), 1),
                    condition=rng.choice(_CONDITIONS),
                    wind_speed_kmh=round(rng.uniform(0.0, 45.0), 1),
                )
            )
        return reports
