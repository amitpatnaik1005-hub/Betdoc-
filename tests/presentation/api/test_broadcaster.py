"""Fan-out isolation and slow-client containment.

Two properties are proven here, and both are memory-safety properties rather
than feature tests:

* A failing child notifier cannot prevent a healthy one from delivering.
* A slow WebSocket client cannot block the orchestrator, cannot grow its queue
  past the configured bound, and is evicted once it is permanently behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, Final, cast

import pytest
from fastapi import WebSocket
from starlette.websockets import WebSocketState

from betdoc.application.ports.notifiers import (
    DebounceDecision,
    NotificationChannel,
)
from betdoc.presentation.api.broadcaster import (
    AlwaysAllowDebouncer,
    BroadcasterConfig,
    CompositeNotifier,
    WebsocketBroadcaster,
    WsMessage,
    WsMessageType,
    _ClientChannel,  # noqa: PLC2701 - white-box test of the enqueue path
)
from tests.conftest import (
    DEFAULT_TIMEOUT,
    FailingNotifier,
    FakeWebSocket,
    RecordingNotifier,
    make_forecast,
    make_recommendation,
    wait_for,
)

pytestmark = pytest.mark.asyncio

_QUEUE_SIZE: Final[int] = 10
_MAX_DROPS: Final[int] = 3


def make_channel(
    *,
    client_id: str = "client-1",
    queue_size: int = _QUEUE_SIZE,
    websocket: FakeWebSocket | None = None,
) -> _ClientChannel:
    """Build a channel with no sender task.

    Deliberately task-free: a live sender would drain the queue between
    enqueues and make overflow behaviour non-deterministic. This isolates
    ``_enqueue`` exactly as the directive requires.
    """
    return _ClientChannel(
        client_id=client_id,
        websocket=cast(WebSocket, websocket or FakeWebSocket()),
        queue=asyncio.Queue(maxsize=queue_size),
        connected_at=time.monotonic(),
        task=None,
    )


def drain(channel: _ClientChannel) -> list[str]:
    """Non-destructively read the queue contents in FIFO order."""
    items: list[str] = []
    while True:
        try:
            items.append(channel.queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    for item in items:
        channel.queue.put_nowait(item)
    return items


# --------------------------------------------------------------------------- #
# Queue overflow integrity
# --------------------------------------------------------------------------- #


async def test_queue_fills_to_capacity_without_dropping(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """Exactly ``client_queue_size`` messages fit with zero drops."""
    channel = make_channel()

    for index in range(_QUEUE_SIZE):
        broadcaster._enqueue(channel, f"msg-{index}")  # noqa: SLF001

    assert channel.queue.qsize() == _QUEUE_SIZE
    assert channel.dropped == 0
    assert channel.consecutive_drops == 0
    assert channel.closing is False
    assert drain(channel) == [f"msg-{index}" for index in range(_QUEUE_SIZE)]


async def test_eleventh_message_drops_the_oldest_and_counts_it(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """The overflow contract, asserted precisely.

    Queue stays at its bound, the *oldest* message is the one discarded, the
    newest is retained, and the drop is counted. Dropping the oldest is correct
    for a live market view: a client shown stale recommendations is worse off
    than one shown a gap.
    """
    channel = make_channel()
    for index in range(_QUEUE_SIZE):
        broadcaster._enqueue(channel, f"msg-{index}")  # noqa: SLF001

    broadcaster._enqueue(channel, "msg-10")  # noqa: SLF001

    assert channel.queue.qsize() == _QUEUE_SIZE, "queue exceeded its hard bound"
    assert channel.dropped == 1
    assert channel.consecutive_drops == 1
    assert channel.closing is False, "one drop must not evict a client"

    contents = drain(channel)
    assert "msg-0" not in contents, "the oldest message was not the one discarded"
    assert contents[0] == "msg-1"
    assert contents[-1] == "msg-10", "the newest message was not retained"
    assert len(contents) == _QUEUE_SIZE


async def test_enqueue_never_blocks_under_extreme_overflow(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """Ten thousand enqueues against a 10-slot queue, in bounded time.

    This is the property that keeps a slow browser tab off the orchestrator's
    critical path. ``_enqueue`` performs no ``await``, so its cost is
    independent of how slow the client's socket is.
    """
    channel = make_channel(queue_size=_QUEUE_SIZE)
    loop = asyncio.get_running_loop()

    started = loop.time()
    for index in range(10_000):
        broadcaster._enqueue(channel, f"flood-{index}")  # noqa: SLF001
        # Keep the channel eligible so the loop measures pure enqueue cost
        # rather than the early-return path after eviction.
        channel.closing = False
    elapsed = loop.time() - started

    assert elapsed < 1.0, f"_enqueue took {elapsed:.3f}s for 10k messages; it blocked"
    assert channel.queue.qsize() == _QUEUE_SIZE, "memory bound was breached"
    assert channel.dropped == 10_000 - _QUEUE_SIZE


async def test_consecutive_drops_evict_the_slow_client(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """At ``max_consecutive_drops`` the client is marked closing and cancelled.

    A client permanently behind is rendering a false picture of the market,
    which is more dangerous than forcing it to reconnect and resynchronise.
    """
    sender_started = asyncio.Event()

    async def park() -> None:
        sender_started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(park(), name="fake-sender")
    await asyncio.wait_for(sender_started.wait(), timeout=DEFAULT_TIMEOUT)

    channel = make_channel()
    channel.task = task
    for index in range(_QUEUE_SIZE):
        broadcaster._enqueue(channel, f"msg-{index}")  # noqa: SLF001

    for drop_index in range(1, _MAX_DROPS + 1):
        channel.closing = False  # re-arm so each overflow is exercised
        broadcaster._enqueue(channel, f"overflow-{drop_index}")  # noqa: SLF001
        assert channel.dropped == drop_index
        assert channel.consecutive_drops == drop_index
        if drop_index < _MAX_DROPS:
            assert channel.closing is False, (
                f"evicted after {drop_index} drops, before the "
                f"{_MAX_DROPS} threshold"
            )

    assert channel.closing is True, "client was not evicted at the drop threshold"
    assert await wait_for(lambda: task.cancelled() or task.done(), timeout=DEFAULT_TIMEOUT)
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled(), "the sender task was not cancelled on eviction"


async def test_enqueue_is_a_noop_once_the_channel_is_closing(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """A closing channel accepts nothing further.

    Without this guard, a client already being torn down would keep accruing
    queue entries that nobody will ever send.
    """
    channel = make_channel()
    channel.closing = True

    broadcaster._enqueue(channel, "ignored")  # noqa: SLF001

    assert channel.queue.qsize() == 0
    assert channel.dropped == 0


async def test_successful_send_resets_the_consecutive_drop_counter(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """Drops must be *consecutive* to evict.

    A client that occasionally falls behind and then catches up is healthy. If
    the counter never reset, a long-lived connection would accumulate isolated
    drops and eventually be evicted for no reason.
    """
    socket = FakeWebSocket()
    client_id = await broadcaster.register(cast(WebSocket, socket))
    assert client_id is not None

    channel = broadcaster._clients[client_id]  # noqa: SLF001
    channel.consecutive_drops = 2

    broadcaster._broadcast_raw(  # noqa: SLF001
        WsMessage(type=WsMessageType.SYSTEM, payload={"probe": True})
    )

    assert await wait_for(lambda: channel.sent >= 1, timeout=DEFAULT_TIMEOUT)
    assert channel.consecutive_drops == 0, "counter did not reset after a send"
    assert channel.closing is False
    await broadcaster.unregister(client_id)


# --------------------------------------------------------------------------- #
# Broadcast semantics
# --------------------------------------------------------------------------- #


async def test_notify_recommendation_returns_immediately_with_a_slow_client(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """One client on a five-second socket must not delay the notifier.

    This is the exact scenario the queue-per-client design exists for: if the
    broadcaster awaited ``send_text`` inline, this assertion would fail by
    roughly the send delay, and the orchestrator's semaphore would be held for
    the same duration.
    """
    slow = FakeWebSocket(send_delay=5.0)
    fast = FakeWebSocket()
    slow_id = await broadcaster.register(cast(WebSocket, slow))
    fast_id = await broadcaster.register(cast(WebSocket, fast))
    assert slow_id is not None
    assert fast_id is not None

    loop = asyncio.get_running_loop()
    started = loop.time()
    decision = await broadcaster.notify_recommendation(make_recommendation())
    elapsed = loop.time() - started

    assert decision.allow is True
    assert elapsed < 0.25, f"notify_recommendation blocked for {elapsed:.3f}s"

    assert await wait_for(lambda: len(fast.sent) >= 1, timeout=DEFAULT_TIMEOUT), (
        "the fast client was starved by the slow one"
    )
    assert slow.sent == [], "the slow client should still be mid-send"

    await broadcaster.unregister(fast_id)
    await broadcaster.unregister(slow_id)


async def test_payload_is_encoded_once_and_shared_across_clients(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """Every client receives the byte-identical frame.

    Encoding per client would multiply serialisation cost by the tab count on
    a path that runs on every evaluation cycle.
    """
    sockets = [FakeWebSocket() for _ in range(3)]
    ids: list[str] = []
    for socket in sockets:
        client_id = await broadcaster.register(cast(WebSocket, socket))
        assert client_id is not None
        ids.append(client_id)

    await broadcaster.notify_forecast(make_forecast())

    assert await wait_for(
        lambda: all(len(socket.sent) >= 2 for socket in sockets),
        timeout=DEFAULT_TIMEOUT,
    ), "not every client received the forecast"

    forecasts = []
    for socket in sockets:
        frames = [
            frame
            for frame in socket.sent
            if json.loads(frame)["type"] == WsMessageType.FORECAST.value
        ]
        assert len(frames) == 1
        forecasts.append(frames[0])

    assert len(set(forecasts)) == 1, "clients received different serialisations"

    for client_id in ids:
        await broadcaster.unregister(client_id)


async def test_rejections_are_sent_as_a_single_batched_frame(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """Thirty refusals must cost one queue slot, not thirty.

    Per-rejection frames would evict genuinely actionable recommendations from
    a bounded 10-slot buffer.
    """
    socket = FakeWebSocket()
    client_id = await broadcaster.register(cast(WebSocket, socket))
    assert client_id is not None

    rejections = tuple(
        make_recommendation(
            recommendation_id=f"pass-{index}",
            opportunity_id=f"opp-{index}",
            action_type=__import__(
                "betdoc.domain.intelligence.advisor_models", fromlist=["ActionType"]
            ).ActionType.PASS,
        )
        for index in range(30)
    )
    count = await broadcaster.notify_rejections(rejections)
    assert count == 30

    assert await wait_for(
        lambda: any(
            json.loads(frame)["type"] == WsMessageType.REJECTION_BATCH.value
            for frame in socket.sent
        ),
        timeout=DEFAULT_TIMEOUT,
    )
    batches = [
        json.loads(frame)
        for frame in socket.sent
        if json.loads(frame)["type"] == WsMessageType.REJECTION_BATCH.value
    ]
    assert len(batches) == 1, "rejections were not batched into one frame"
    assert batches[0]["payload"]["count"] == 30
    assert len(batches[0]["payload"]["rejections"]) == 30

    await broadcaster.unregister(client_id)


async def test_broadcast_with_no_clients_is_a_noop(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """No clients means no work and no exception."""
    assert broadcaster.client_count == 0
    decision = await broadcaster.notify_recommendation(make_recommendation())
    assert decision.allow is True
    await broadcaster.notify_forecast(make_forecast())
    assert await broadcaster.notify_rejections(()) == 0


async def test_registration_is_refused_past_the_client_cap(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """Fan-out is O(clients) on the hot path, so the cap must be enforced.

    Returning ``None`` rather than raising lets the router close with an
    explicit policy code, so the client backs off instead of reconnecting in a
    tight loop.
    """
    accepted: list[str] = []
    for _ in range(8):
        client_id = await broadcaster.register(cast(WebSocket, FakeWebSocket()))
        assert client_id is not None
        accepted.append(client_id)

    assert broadcaster.client_count == 8
    refused = await broadcaster.register(cast(WebSocket, FakeWebSocket()))
    assert refused is None, "the client cap was not enforced"
    assert broadcaster.client_count == 8

    for client_id in accepted:
        await broadcaster.unregister(client_id)
    assert broadcaster.client_count == 0


async def test_unregister_is_idempotent_and_stops_the_sender(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """Double unregistration must be harmless; the task must be awaited.

    An abandoned sender task holding a half-written frame leaves the peer
    waiting on a socket that will never complete.
    """
    socket = FakeWebSocket()
    client_id = await broadcaster.register(cast(WebSocket, socket))
    assert client_id is not None
    channel = broadcaster._clients[client_id]  # noqa: SLF001
    task = channel.task
    assert task is not None

    await broadcaster.unregister(client_id)
    assert broadcaster.client_count == 0
    assert task.done()

    await broadcaster.unregister(client_id)
    assert broadcaster.client_count == 0


async def test_close_disconnects_every_client() -> None:
    """Teardown must close sockets politely so clients reconnect, not error."""
    instance = WebsocketBroadcaster(
        BroadcasterConfig(client_queue_size=10, max_clients=4, max_consecutive_drops=3)
    )
    await instance.start()

    sockets = [FakeWebSocket() for _ in range(3)]
    for socket in sockets:
        assert await instance.register(cast(WebSocket, socket)) is not None

    await asyncio.wait_for(instance.close(), timeout=DEFAULT_TIMEOUT)

    assert instance.client_count == 0
    for socket in sockets:
        assert socket.closed_with is not None, "a client socket was left open"
        assert socket.closed_with[0] == 1001, "clients must get a going-away code"


async def test_client_snapshot_reports_queue_depth_and_drops(
    broadcaster: WebsocketBroadcaster,
) -> None:
    """The health projection must expose the memory-safety counters."""
    socket = FakeWebSocket(send_delay=5.0)
    client_id = await broadcaster.register(cast(WebSocket, socket))
    assert client_id is not None

    for index in range(_QUEUE_SIZE + 5):
        broadcaster._broadcast_raw(  # noqa: SLF001
            WsMessage(type=WsMessageType.SYSTEM, payload={"index": index})
        )

    snapshots = {item.client_id: item for item in broadcaster.snapshot()}
    assert client_id in snapshots
    entry = snapshots[client_id]
    assert entry.queue_depth <= _QUEUE_SIZE
    assert entry.dropped >= 1
    assert entry.connected_seconds >= 0.0

    await broadcaster.unregister(client_id)


# --------------------------------------------------------------------------- #
# Serialisation contract
# --------------------------------------------------------------------------- #


async def test_encode_produces_json_safe_enums_and_datetimes() -> None:
    """Domain enums and datetimes must survive the wire as primitives.

    ``model_dump(mode="json")`` coerces the nested model; ``model_dump_json()``
    stringifies the envelope. Both halves are exercised, and the result must be
    parseable by a plain JSON reader with no custom decoder.
    """
    recommendation = make_recommendation()
    message = WsMessage.of(
        WsMessageType.RECOMMENDATION,
        recommendation,
        trace_id=recommendation.trace_id,
        sequence=7,
    )

    decoded: dict[str, Any] = json.loads(message.encode())

    assert decoded["type"] == "recommendation"
    assert decoded["sequence"] == 7
    assert decoded["trace_id"] == recommendation.trace_id
    assert isinstance(decoded["sent_at"], str)
    assert decoded["payload"]["action_type"] == "single"
    assert decoded["payload"]["bookmaker"] == "pinnacle"
    assert isinstance(decoded["payload"]["generated_at"], str)
    assert isinstance(decoded["payload"]["suggested_stake_paise"], int)
    assert decoded["payload"]["rejection_reason"] is None


async def test_model_dump_json_rejects_a_mode_argument() -> None:
    """Pins the Pydantic V2 API so a future refactor cannot reintroduce the bug.

    ``model_dump_json()`` is already JSON mode and takes no ``mode`` keyword;
    passing one raises ``TypeError``. The ``mode="json"`` keyword belongs to
    ``model_dump()``. Encoding the envelope with the wrong call would fail at
    runtime on the first broadcast rather than at import, so it is asserted.
    """
    message = WsMessage(type=WsMessageType.HEARTBEAT)
    with pytest.raises(TypeError):
        message.model_dump_json(mode="json")  # type: ignore[call-arg]

    assert isinstance(message.model_dump(mode="json"), dict)
    assert isinstance(message.model_dump_json(), str)


# --------------------------------------------------------------------------- #
# CompositeNotifier fan-out
# --------------------------------------------------------------------------- #


async def test_composite_delivers_to_every_child() -> None:
    """Both children receive the same session."""
    first = RecordingNotifier(channel=NotificationChannel.CONSOLE)
    second = RecordingNotifier(channel=NotificationChannel.WEBSOCKET)
    composite = CompositeNotifier(first, second)

    forecast = make_forecast()
    recommendations = (
        make_recommendation(recommendation_id="rec-1"),
        make_recommendation(
            recommendation_id="rec-2",
            opportunity_id="opp-2",
            action_type=__import__(
                "betdoc.domain.intelligence.advisor_models", fromlist=["ActionType"]
            ).ActionType.PASS,
        ),
    )
    delivered, rejected = await composite.notify_session(
        forecast=forecast, recommendations=recommendations
    )

    assert delivered == 1
    assert rejected == 1
    assert len(first.sessions) == 1
    assert len(second.sessions) == 1
    assert first.sessions[0][0].forecast_id == forecast.forecast_id
    assert second.sessions[0][0].forecast_id == forecast.forecast_id


async def test_failing_child_does_not_prevent_healthy_child_delivery() -> None:
    """The isolation guarantee, stated as an assertion.

    A dead WebSocket must not stop the console from rendering, and neither must
    stop the orchestrator. ``asyncio.gather(..., return_exceptions=True)`` is
    what makes this hold, and this test fails immediately if it is removed.
    """
    broken = FailingNotifier(RuntimeError("websocket transport is down"))
    healthy = RecordingNotifier(channel=NotificationChannel.CONSOLE)
    composite = CompositeNotifier(broken, healthy)

    forecast = make_forecast()
    recommendations = (make_recommendation(),)

    delivered, _ = await composite.notify_session(
        forecast=forecast, recommendations=recommendations
    )

    assert broken.attempts == 1, "the failing child was never attempted"
    assert len(healthy.sessions) == 1, "the healthy child was starved by the failure"
    assert healthy.sessions[0][0].forecast_id == forecast.forecast_id
    assert delivered == 1, "the successful child's count was discarded"


async def test_failure_order_does_not_matter() -> None:
    """Isolation must hold whether the failing child is first or last.

    Sequential ``await``s would pass with the failure last and fail with it
    first, so both orders are asserted.
    """
    for failing_first in (True, False):
        broken = FailingNotifier()
        healthy = RecordingNotifier()
        children = (broken, healthy) if failing_first else (healthy, broken)
        composite = CompositeNotifier(*children)

        await composite.notify_session(
            forecast=make_forecast(), recommendations=(make_recommendation(),)
        )

        assert len(healthy.sessions) == 1, (
            f"healthy child starved with failing_first={failing_first}"
        )
        assert broken.attempts == 1


async def test_children_are_invoked_concurrently_not_sequentially() -> None:
    """Wall-clock proof that ``gather`` is used rather than a loop of awaits.

    Two children each sleeping 150ms complete in roughly 150ms together. A
    sequential implementation would take roughly 300ms, so the bound
    discriminates between the two designs with a wide safety margin.
    """
    first = RecordingNotifier(delay=0.15)
    second = RecordingNotifier(delay=0.15)
    composite = CompositeNotifier(first, second)

    loop = asyncio.get_running_loop()
    started = loop.time()
    await composite.notify_session(
        forecast=make_forecast(), recommendations=(make_recommendation(),)
    )
    elapsed = loop.time() - started

    assert elapsed < 0.28, f"children ran sequentially ({elapsed:.3f}s for 2 x 150ms)"
    assert len(first.sessions) == 1
    assert len(second.sessions) == 1


async def test_composite_reports_allowed_when_any_child_delivers() -> None:
    """Aggregate decision means "reached at least one channel".

    Reporting suppression because the console debounced would understate
    delivery when the dashboard did in fact receive the frame.
    """
    broken = FailingNotifier()
    healthy = RecordingNotifier()
    composite = CompositeNotifier(broken, healthy)

    decision = await composite.notify_recommendation(make_recommendation())

    assert isinstance(decision, DebounceDecision)
    assert decision.allow is True
    assert len(healthy.recommendations) == 1


async def test_composite_reports_suppressed_when_every_child_fails() -> None:
    """All children down must surface as a non-delivery, not a false success."""
    composite = CompositeNotifier(FailingNotifier(), FailingNotifier())

    decision = await composite.notify_recommendation(make_recommendation())

    assert decision.allow is False
    assert "every child notifier failed" in decision.detail


async def test_composite_close_closes_every_child_despite_failures() -> None:
    """Teardown must be total.

    A child skipped because an earlier one raised leaks whatever it owns:
    for the broadcaster, that is every connected socket and sender task.
    """
    broken = FailingNotifier()
    healthy_first = RecordingNotifier()
    healthy_second = RecordingNotifier()
    composite = CompositeNotifier(healthy_first, broken, healthy_second)

    await composite.close()

    assert healthy_first.closed == 1
    assert healthy_second.closed == 1, "a child after the failing one was skipped"
    assert broken.closed == 1


async def test_composite_start_tolerates_a_failing_child() -> None:
    """A channel that cannot initialise must not block the others."""
    broken = FailingNotifier()
    healthy = RecordingNotifier()
    composite = CompositeNotifier(broken, healthy)

    await composite.start()

    assert healthy.started == 1


async def test_composite_requires_at_least_one_child() -> None:
    """An empty composite would silently discard every alert."""
    with pytest.raises(ValueError, match="at least one child"):
        CompositeNotifier()


async def test_composite_exposes_its_children() -> None:
    """Introspection for the health endpoint and for teardown ordering."""
    first = RecordingNotifier()
    second = RecordingNotifier()
    composite = CompositeNotifier(first, second)

    assert composite.children == (first, second)
    assert isinstance(composite.debouncer, AlwaysAllowDebouncer), (
        "the composite must not double-debounce; children own their own policy"
    )


async def test_composite_with_a_real_broadcaster_child() -> None:
    """End-to-end: composite plus a real broadcaster plus a real client."""
    instance = WebsocketBroadcaster(
        BroadcasterConfig(client_queue_size=16, max_clients=2, max_consecutive_drops=3)
    )
    await instance.start()
    console_double = RecordingNotifier(channel=NotificationChannel.CONSOLE)
    composite = CompositeNotifier(console_double, instance)

    socket = FakeWebSocket()
    client_id = await instance.register(cast(WebSocket, socket))
    assert client_id is not None

    try:
        await composite.notify_session(
            forecast=make_forecast(), recommendations=(make_recommendation(),)
        )
        assert len(console_double.sessions) == 1
        assert await wait_for(
            lambda: any(
                json.loads(frame)["type"] == WsMessageType.RECOMMENDATION.value
                for frame in socket.sent
            ),
            timeout=DEFAULT_TIMEOUT,
        ), "the broadcaster child never delivered the recommendation"
    finally:
        await asyncio.wait_for(composite.close(), timeout=DEFAULT_TIMEOUT)

    assert socket.client_state is WebSocketState.DISCONNECTED
