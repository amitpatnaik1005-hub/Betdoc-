from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from app.domain.the_wire import (
    CoalescingTTLCache,
    MockNewsProvider,
    MockScoreProvider,
    MockWeatherProvider,
    NewsProvider,
    ScoreProvider,
    WeatherProvider,
    WireAggregatorService,
)
from app.schemas.the_wire import MatchScore, NewsItem, WeatherReport

pytestmark = pytest.mark.asyncio


class StubNews:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch_latest_news(self) -> list[NewsItem]:
        self.calls += 1
        return [
            NewsItem(
                id="n1", source="Test", title="Headline", summary="Summary",
                url="https://news.example.com/n1", published_at=datetime.now(timezone.utc),
            )
        ]


class RecordingScores:
    def __init__(self, *, fail: bool = False, leak: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail, self.leak = fail, leak

    async def fetch_scores(self, match_ids: Sequence[str]) -> list[MatchScore]:
        self.calls.append(list(match_ids))
        if self.fail:
            raise ConnectionError("scores down")
        if self.leak:
            return [{"match_id": m, "home_score": 1} for m in match_ids]  # type: ignore[misc]
        return [
            MatchScore(match_id=m, home_team="H", away_team="A", home_score=1, away_score=0,
                       status="LIVE", clock="10'")
            for m in match_ids
        ]


class RecordingWeather:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    async def fetch_weather(self, match_ids: Sequence[str]) -> list[WeatherReport]:
        self.calls.append(list(match_ids))
        if self.fail:
            raise TimeoutError("weather down")
        return [
            WeatherReport(match_id=m, temperature_c=12.0, condition="CLEAR", wind_speed_kmh=5.0)
            for m in match_ids
        ]


def make_service(news=None, scores=None, weather=None) -> WireAggregatorService:
    return WireAggregatorService(
        news or StubNews(),
        scores or RecordingScores(),
        weather or RecordingWeather(),
        score_cache=CoalescingTTLCache("scores-test"),
        weather_cache=CoalescingTTLCache("weather-test"),
        news_cache=CoalescingTTLCache("news-test"),
    )


async def test_mock_providers_satisfy_protocols() -> None:
    assert isinstance(MockNewsProvider(), NewsProvider)
    assert isinstance(MockScoreProvider(), ScoreProvider)
    assert isinstance(MockWeatherProvider(), WeatherProvider)


async def test_empty_match_ids_short_circuit() -> None:
    scores, weather = RecordingScores(), RecordingWeather()
    dashboard = await make_service(scores=scores, weather=weather).fetch_dashboard([])
    assert dashboard.scores == [] and dashboard.weather == {}
    assert scores.calls == [] and weather.calls == []
    assert len(dashboard.news) == 1


async def test_scatter_gather_across_requests() -> None:
    scores = RecordingScores()
    service = make_service(scores=scores)
    await service.fetch_dashboard(["m1"])
    dashboard = await service.fetch_dashboard(["m1", "m2"])
    assert scores.calls == [["m1"], ["m2"]]
    assert [s.match_id for s in dashboard.scores] == ["m1", "m2"]


async def test_dedupes_and_preserves_order() -> None:
    scores = RecordingScores()
    dashboard = await make_service(scores=scores).fetch_dashboard(["m2", "m1", "m2"])
    assert scores.calls == [["m2", "m1"]]
    assert [s.match_id for s in dashboard.scores] == ["m2", "m1"]


async def test_partial_failure_returns_partial_data_and_negative_caches() -> None:
    weather = RecordingWeather(fail=True)
    service = make_service(weather=weather)
    first = await service.fetch_dashboard(["m1", "m2"])
    second = await service.fetch_dashboard(["m1", "m2"])
    assert [s.match_id for s in first.scores] == ["m1", "m2"]
    assert first.weather == {} and second.weather == {}
    assert weather.calls == [["m1", "m2"]]  # circuit breaker: no second hit


async def test_raw_dicts_from_provider_are_rejected() -> None:
    dashboard = await make_service(scores=RecordingScores(leak=True)).fetch_dashboard(["m1"])
    assert dashboard.scores == []


async def test_news_is_cached_globally() -> None:
    news = StubNews()
    service = make_service(news=news)
    await service.fetch_dashboard(["m1"])
    await service.fetch_dashboard([])
    assert news.calls == 1
