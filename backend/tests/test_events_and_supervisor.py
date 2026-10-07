"""Cross-section event bus and commander supervisor."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import events
from app.core.events import EVENTS_CHANNEL, mutation_event_middleware, section_of
from app.models.the_hive import BotStatus, LegendaryBot
from app.services import commander_supervisor as supervisor


class FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, json.loads(payload)))
        return 1


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/v1/execution/place-bet", "execution"),
        ("/api/v1/the-vault/cfo/alerts/1/read", "the-vault"),
        ("/api/v1/control-panel", "control-panel"),
        ("/health", None),
        ("/api/v1/", None),
    ],
)
def test_section_of(path: str, expected: str | None) -> None:
    assert section_of(path) == expected


def _app(redis: FakeRedis) -> FastAPI:
    app = FastAPI()
    app.state.redis = redis
    app.middleware("http")(mutation_event_middleware)

    @app.post("/api/v1/execution/place-bet")
    async def place() -> dict:
        return {"ok": True}

    @app.get("/api/v1/vault/overview")
    async def overview() -> dict:
        return {}

    @app.post("/api/v1/control-panel/emergency-stop", status_code=409)
    async def refused() -> dict:
        return {}

    @app.post("/api/v1/auth/login")
    async def login() -> dict:
        return {}

    return app


async def _drain_background() -> None:
    await asyncio.gather(*list(events._background))


@pytest.mark.asyncio
async def test_only_successful_writes_publish_events() -> None:
    redis = FakeRedis()
    async with AsyncClient(transport=ASGITransport(app=_app(redis)), base_url="http://t") as client:
        assert (await client.post("/api/v1/execution/place-bet")).status_code == 200
        await client.get("/api/v1/vault/overview")  # reads never publish
        await client.post("/api/v1/control-panel/emergency-stop")  # failed writes never publish
        await client.post("/api/v1/auth/login")  # auth is silent
        await _drain_background()

    assert len(redis.published) == 1
    channel, event = redis.published[0]
    assert channel == EVENTS_CHANNEL
    assert event["type"] == "mutation" and event["section"] == "execution" and event["method"] == "POST"
    assert "at" in event


@pytest.mark.asyncio
async def test_publish_without_redis_is_a_no_op() -> None:
    await events.publish_event(None, {"type": "mutation"})


class FakeHive:
    def __init__(self) -> None:
        self.beats: dict[LegendaryBot, tuple[BotStatus, dict]] = {}

    async def record_heartbeat(self, db, bot, *, status, uptime_seconds, resource_metrics):  # noqa: ANN001
        assert uptime_seconds >= 0
        self.beats[bot] = (status, dict(resource_metrics))


class FakeSession:
    async def rollback(self) -> None:
        return None


@asynccontextmanager
async def _session():
    yield FakeSession()


@pytest.mark.asyncio
async def test_sweep_isolates_failing_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def healthy(db, ctx):  # noqa: ANN001
        return BotStatus.WORKING, {"open_positions": 2}

    async def broken(db, ctx):  # noqa: ANN001
        raise RuntimeError("table missing")

    monkeypatch.setattr(supervisor, "PROBES", {LegendaryBot.BAJIRAO: healthy, LegendaryBot.KUMBHA: broken})
    hive = FakeHive()
    ctx = supervisor.ProbeContext(redis=None, vault_configured=True, ws_clients=lambda: 0)

    result = await supervisor.sweep(_session, hive, ctx, started_at=0.0)

    assert result == {LegendaryBot.BAJIRAO: BotStatus.WORKING, LegendaryBot.KUMBHA: BotStatus.FATAL}
    assert hive.beats[LegendaryBot.BAJIRAO][1]["open_positions"] == 2
    assert "RuntimeError: table missing" in hive.beats[LegendaryBot.KUMBHA][1]["error"]
    assert all("probed_at" in metrics for _, metrics in hive.beats.values())


def test_every_commander_has_a_probe() -> None:
    assert set(supervisor.PROBES) == set(LegendaryBot)
