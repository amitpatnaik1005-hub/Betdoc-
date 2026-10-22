"""The live smallcases, pushed (Group 111, closing Group 110's pillar 9 gap). Developed for Amit Ashok Kumar Patnaik.

A fresh read of the market announces itself on the smallcase channel only when what the baskets draw from changed (the
legs, their books, prices and fair chances); the same read again says nothing. The ``/ws/smallcases`` socket greets,
sends the viewer's board at once, sends it again only when it changed (a market announcement, or its own clock through
the shared scan), answers pings, and refuses without Redis. The frontend reads the push into the board everyone uses,
polls only while the socket is down, and keeps a rebalanced slip live.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import redis as sync_redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.websockets import WebSocketDisconnect

from app.api.deps import get_ws_user
from app.api.v1 import smallcase as smallcase_api
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.smallcase import baskets as bk
from app.models import User
from app.services import smallcase
from tests.test_backtest_and_parlay import redis  # noqa: F401 - fixture
from tests.test_ultra_vetting import PINNACLE_NO_ARB, SOFT, seed

TEST_REDIS_URL = os.environ["TEST_REDIS_URL"]
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya_push", "TWIN_PREFIX": "test_twin_push", "SMALLCASE_SCAN_SECONDS": 30})


def _pick(leg_id: str, price: float = 2.1, fair: float = 0.5) -> bk.Pick:
    return bk.Pick(leg_id, leg_id.split("|")[0], "A v B", "1X2: A", "Match Odds", "MATCH_ODDS", "soccer_epl", "Football", "EPL", None, False, "1xbet",
                   price, fair, fair * price - 1, {"1xbet": price})


def test_the_digest_is_what_the_baskets_draw_from() -> None:
    a, b = _pick("f1|Match Odds|HOME"), _pick("f2|Match Odds|AWAY", 3.4, 0.3)
    assert smallcase.digest([a, b]) == smallcase.digest([b, a])  # order does not matter
    assert smallcase.digest([a, b]) != smallcase.digest([_pick("f1|Match Odds|HOME", 2.15), b])  # a price moved
    assert smallcase.digest([a, b]) != smallcase.digest([_pick("f1|Match Odds|HOME", 2.1, 0.52), b])  # BetDoc's fair chance moved
    assert smallcase.digest([a]) != smallcase.digest([a, b])  # a leg came or went


async def _messages(pubsub: Any, wait: float = 0.5) -> list[dict[str, Any]]:
    out, deadline = [], asyncio.get_running_loop().time() + wait
    while asyncio.get_running_loop().time() < deadline:
        message = await pubsub.get_message(timeout=0.1)
        if message and message.get("type") == "message":
            out.append(json.loads(message["data"]))
    return out


@pytest.mark.asyncio
async def test_a_fresh_read_announces_itself_only_when_the_market_changed(redis: Redis, settings: Settings) -> None:  # noqa: F811
    await seed(redis, settings, "fx-push", "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB, soft=SOFT)
    pubsub = redis.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(smallcase.live_channel(settings))
    try:
        picks, as_of, _ = await smallcase.scan(redis, settings, datetime.now(UTC), fresh=True)
        (first,) = await _messages(pubsub)
        assert first == {"type": "scan", "as_of": as_of, "digest": smallcase.digest(picks), "valued": len(picks)} and len(picks) >= 3
        await smallcase.scan(redis, settings, datetime.now(UTC), fresh=True)
        assert await _messages(pubsub) == []  # the same market: nothing to push
        await smallcase.scan(redis, settings, datetime.now(UTC))  # a cached read never announces
        assert await _messages(pubsub) == []
        await seed(redis, settings, "fx-push", "Arsenal", "Chelsea", sharp=PINNACLE_NO_ARB,
                   soft={**SOFT, "onexbet": {"HOME": 2.55, "DRAW": 3.10, "AWAY": 3.40}})  # 1xBet moves Arsenal out
        moved, _, _ = await smallcase.scan(redis, settings, datetime.now(UTC), fresh=True)
        (second,) = await _messages(pubsub)
        assert second["digest"] == smallcase.digest(moved) != first["digest"]
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()


def _socket_app(settings: Settings, boards: list[dict[str, Any]], calls: list[str]) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.redis = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        yield
        await app.state.redis.aclose()

    async def fake_baskets(sessions: Any, redis: Any, settings_: Settings, user_id: uuid.UUID, *, bankroll: Any, now: datetime, fresh: bool = False) -> dict[str, Any]:
        calls.append(str(user_id))
        return boards[min(len(calls), len(boards)) - 1]

    app = FastAPI(lifespan=lifespan)
    app.include_router(smallcase_api.ws_router, prefix="/api/v1/ws")
    app.dependency_overrides[get_ws_user] = lambda: User(id=uuid.UUID(int=7), username="viewer", hashed_password="x")
    app.dependency_overrides[get_session_factory] = lambda: None
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.fake_baskets = fake_baskets
    return app


def test_the_socket_pushes_the_viewers_board_and_only_when_it_changed(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = sync_redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        probe.ping()
    except (RedisError, OSError):
        pytest.skip(f"Redis not reachable at {TEST_REDIS_URL}")
    board_a = {"as_of": "2026-10-23T10:00:00+00:00", "computed_at": "t1", "baskets": [{"id": "SAFE_COMPOUNDER", "stake_inr": "500.00"}]}
    board_a_again = {**board_a, "computed_at": "t2"}  # only the computation time differs: not pushed again
    board_b = {"as_of": "2026-10-23T10:00:30+00:00", "computed_at": "t3", "baskets": [{"id": "SAFE_COMPOUNDER", "stake_inr": "650.00"}]}
    calls: list[str] = []
    app = _socket_app(settings, [board_a, board_a_again, board_b], calls)
    monkeypatch.setattr(smallcase, "baskets", app.state.fake_baskets)
    channel = smallcase.live_channel(settings)
    try:
        with TestClient(app) as client, client.websocket_connect("/api/v1/ws/smallcases?token=x") as ws:
            assert ws.receive_json() == {"type": "connection_ack", "channel": channel, "refresh_seconds": 30}
            first = ws.receive_json()
            assert first["type"] == "board" and first["reason"] == "connected" and first["board"] == board_a and calls == [str(uuid.UUID(int=7))]
            probe.publish(channel, json.dumps({"type": "scan"}))  # the market moved, but this viewer's board did not
            probe.publish(channel, json.dumps({"type": "scan"}))  # and now it did
            second = ws.receive_json()
            assert second == {"type": "board", "reason": "market", "board": board_b} and len(calls) == 3
            ws.send_text("ping")
            assert ws.receive_json() == {"type": "pong"}
    finally:
        probe.close()


def test_the_socket_refuses_without_redis(settings: Settings) -> None:
    app = FastAPI()
    app.include_router(smallcase_api.ws_router, prefix="/api/v1/ws")
    app.state.redis = None
    app.dependency_overrides[get_ws_user] = lambda: User(id=uuid.uuid4(), username="viewer", hashed_password="x")
    app.dependency_overrides[get_session_factory] = lambda: None
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as closed, client.websocket_connect("/api/v1/ws/smallcases?token=x") as ws:
        ws.receive_json()
    assert closed.value.code == 1011


def test_the_socket_is_mounted_beside_the_other_live_channels() -> None:
    """Under /api/v1/ws, where /ws/the-wire and /ws/neural-bus live (routers are included lazily: read the mount itself)."""
    assert [getattr(r, "path", None) for r in smallcase_api.ws_router.routes] == ["/smallcases"]
    source = (Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "router.py").read_text(encoding="utf-8")
    assert 'api_router.include_router(smallcase.ws_router, prefix="/ws")' in source


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
def test_the_frontend_reads_the_push() -> None:
    live = (FRONTEND / "lib" / "smallcase_live.ts").read_text(encoding="utf-8")
    lib = (FRONTEND / "lib" / "smallcase.ts").read_text(encoding="utf-8")
    ticker = (FRONTEND / "components" / "smallcase" / "LiveSmallcaseTickerBar.tsx").read_text(encoding="utf-8")
    slip = (FRONTEND / "components" / "smallcase" / "SmallcaseSlipRebalancer.tsx").read_text(encoding="utf-8")
    assert '"/ws/smallcases"' in live and "publish(BOARD_KEY" in live and "MOCK_AUTH" in live
    assert "useSmallcaseFeed()" in lib and "live ? 300_000 : 30_000" in lib  # poll only as a safety net while the push is up
    assert "useSmallcaseFeed()" in ticker and "pushed live as it moves" in ticker and "polled every" in ticker
    assert "useMarketPulse()" in slip and "Stop live" in slip and "SETTLE_MS" in slip
