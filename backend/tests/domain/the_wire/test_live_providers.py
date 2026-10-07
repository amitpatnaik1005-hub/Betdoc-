"""Live Wire providers parse real feed formats (no network: transports are mocked)."""

import httpx
import pytest

from app.domain.the_wire import live_providers
from app.domain.the_wire.live_providers import OddsApiScoreProvider, RssNewsProvider

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>BBC Sport</title>
<item><title>Late winner &amp; drama</title><description><![CDATA[<p>Report</p>]]></description>
<link>https://bbc.co.uk/sport/1</link><pubDate>Tue, 06 Oct 2026 20:00:00 GMT</pubDate></item>
<item><title>Earlier story</title><description>x</description><link>https://bbc.co.uk/sport/2</link>
<pubDate>Tue, 06 Oct 2026 09:00:00 GMT</pubDate></item></channel></rss>"""

SCORES = [
    {"id": "e1", "home_team": "Arsenal", "away_team": "Chelsea", "completed": True, "scores": [{"name": "Arsenal", "score": "2"}, {"name": "Chelsea", "score": "1"}]},
    {"id": "e2", "home_team": "Spurs", "away_team": "Leeds", "completed": False, "scores": None},
]


@pytest.fixture
def mock_http(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"scores": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/scores"):
            calls["scores"] += 1
            return httpx.Response(200, json=SCORES)
        return httpx.Response(200, content=RSS)

    real = httpx.AsyncClient
    monkeypatch.setattr(live_providers.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return calls


@pytest.mark.asyncio
async def test_rss_news_is_parsed_sorted_and_cleaned(mock_http: dict[str, int]) -> None:
    news = await RssNewsProvider(["https://feeds.example/rss.xml"]).fetch_latest_news()
    assert [n.title for n in news] == ["Late winner & drama", "Earlier story"]
    assert news[0].source == "BBC Sport" and news[0].summary == "Report" and news[0].url.startswith("https://bbc.co.uk")


@pytest.mark.asyncio
async def test_scores_map_status_filter_ids_and_cache(mock_http: dict[str, int]) -> None:
    provider = OddsApiScoreProvider("k", "https://api.example/v4", ["soccer_epl"])
    scores = await provider.fetch_scores(["e1", "e2"])
    by_id = {s.match_id: s for s in scores}
    assert (by_id["e1"].status, by_id["e1"].home_score, by_id["e1"].away_score) == ("FT", 2, 1)
    assert by_id["e2"].status == "SCHEDULED"
    assert [s.match_id for s in await provider.fetch_scores(["e2"])] == ["e2"]
    assert mock_http["scores"] == 1  # second call served from the provider cache (quota-friendly)


@pytest.mark.asyncio
async def test_scores_without_api_key_are_empty() -> None:
    assert await OddsApiScoreProvider(None, "https://api.example/v4", ["soccer_epl"]).fetch_scores(["e1"]) == []
