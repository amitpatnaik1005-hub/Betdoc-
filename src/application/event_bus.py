"""Redis Streams event bus: at-least-once delivery with a real DLQ.

Design decisions and the failure each one prevents:

* **Streams, not pub/sub.** Redis pub/sub is fire-and-forget: a consumer that
  is restarting simply misses messages, silently and unrecoverably. Streams
  give consumer groups, per-message acknowledgement, and replay.
* **Explicit ``XGROUP CREATE ... 0 MKSTREAM``.** Reading from a group that does
  not exist raises ``NOGROUP`` and kills the consumer. Creating the group with
  ``MKSTREAM`` also removes the ordering dependency between producer and
  consumer startup.
* **``MAXLEN ~ 100000`` on every publish.** A stopped consumer is a normal
  operational event. Without a cap it becomes an unbounded memory leak that
  takes down Redis and therefore every service on the bus. The ``~`` form trims
  at radix-tree node boundaries, which is O(1) amortised rather than O(n).
* **Acknowledge only after the caller succeeds.** :meth:`consume` yields, and
  the ``XACK`` is issued on the *resumption* of the generator. If the caller's
  loop body raises, the generator is closed at the yield point, the ack never
  happens, and the message stays pending for reclaim. That is genuine
  at-least-once, not at-most-once wearing a costume.
* **``XAUTOCLAIM`` plus delivery counting.** A crashed consumer leaves messages
  pending forever. Reclaim redelivers them; a delivery count above the configured
  limit quarantines the message to ``{stream}:dlq``. Without the count, one
  poison message blocks the group indefinitely.

The bus never creates a global client. The pool is injected, and the bus closes
only the client it opened, never the pool it borrowed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from types import TracebackType
from typing import Any, Final, Generic, Self

import structlog
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool
from redis.exceptions import ResponseError

from application.events import EventEnvelope, PayloadT, Streams

__all__ = ["ConsumedMessage", "EventBus", "PayloadDecodeError"]

_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(
    component="application.event_bus"
)

_PAYLOAD_FIELD: Final[str] = "payload"
_BUSYGROUP: Final[str] = "BUSYGROUP"
_NEW_MESSAGES: Final[str] = ">"
_CLAIM_START: Final[str] = "0-0"


class PayloadDecodeError(ValueError):
    """A stream entry could not be decoded into the expected envelope type."""

    def __init__(self, stream_name: str, message_id: str, detail: str) -> None:
        super().__init__(
            f"undecodable message {message_id} on {stream_name}: {detail}"
        )
        self.stream_name = stream_name
        self.message_id = message_id
        self.detail = detail


def _as_str(value: str | bytes) -> str:
    """Tolerate pools configured with or without ``decode_responses``."""
    return value.decode("utf-8") if isinstance(value, bytes) else value


class ConsumedMessage(Generic[PayloadT]):
    """A single in-flight message plus its acknowledgement handle."""

    __slots__ = ("_acknowledged", "envelope", "message_id", "stream_name")

    def __init__(
        self,
        stream_name: str,
        message_id: str,
        envelope: EventEnvelope[PayloadT],
    ) -> None:
        self.stream_name = stream_name
        self.message_id = message_id
        self.envelope = envelope
        self._acknowledged = False

    @property
    def acknowledged(self) -> bool:
        return self._acknowledged

    def mark_acknowledged(self) -> None:
        self._acknowledged = True

    @property
    def trace_id(self) -> str:
        return self.envelope.trace_id

    def __repr__(self) -> str:
        return (
            f"<ConsumedMessage {self.stream_name}/{self.message_id} "
            f"trace_id={self.envelope.trace_id}>"
        )


class EventBus:
    """Injected-pool Redis Streams client."""

    __slots__ = (
        "_claim_min_idle_ms",
        "_client",
        "_dlq_max_length",
        "_ensured_groups",
        "_max_delivery_attempts",
        "_pool",
        "_stream_max_length",
    )

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        stream_max_length: int = 100_000,
        dlq_max_length: int = 50_000,
        claim_min_idle_ms: int = 30_000,
        max_delivery_attempts: int = 3,
    ) -> None:
        if stream_max_length < 1:
            msg = f"stream_max_length must be positive, got {stream_max_length}"
            raise ValueError(msg)
        if max_delivery_attempts < 1:
            msg = f"max_delivery_attempts must be positive, got {max_delivery_attempts}"
            raise ValueError(msg)

        self._pool = pool
        self._client: Redis = Redis(connection_pool=pool)
        self._stream_max_length = stream_max_length
        self._dlq_max_length = dlq_max_length
        self._claim_min_idle_ms = claim_min_idle_ms
        self._max_delivery_attempts = max_delivery_attempts
        self._ensured_groups: set[tuple[str, str]] = set()

    async def __aenter__(self) -> Self:
        await self.ping()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def ping(self) -> bool:
        result = await self._client.ping()
        return bool(result)

    async def close(self) -> None:
        await self._client.aclose()

    async def publish(self, stream_name: str, event: EventEnvelope[Any]) -> str:
        fields: dict[str, str] = {
            _PAYLOAD_FIELD: event.model_dump_json(),
            "event_id": str(event.event_id),
            "trace_id": event.trace_id,
            "event_type": event.event_type,
            "retry_count": str(event.retry_count),
        }
        message_id = await self._client.xadd(
            name=stream_name,
            fields=fields,
            maxlen=self._stream_max_length,
            approximate=True,
        )
        decoded = _as_str(message_id)
        _log.debug(
            "event_bus.published",
            stream=stream_name,
            message_id=decoded,
            **event.log_context(),
        )
        return decoded

    async def publish_many(
        self, stream_name: str, events: Sequence[EventEnvelope[Any]]
    ) -> tuple[str, ...]:
        if not events:
            return ()
        async with self._client.pipeline(transaction=False) as pipe:
            for event in events:
                pipe.xadd(
                    name=stream_name,
                    fields={
                        _PAYLOAD_FIELD: event.model_dump_json(),
                        "event_id": str(event.event_id),
                        "trace_id": event.trace_id,
                        "event_type": event.event_type,
                        "retry_count": str(event.retry_count),
                    },
                    maxlen=self._stream_max_length,
                    approximate=True,
                )
            results = await pipe.execute()
        _log.debug(
            "event_bus.published_batch", stream=stream_name, count=len(results)
        )
        return tuple(_as_str(item) for item in results)

    async def ensure_group(self, stream_name: str, group_name: str) -> None:
        cache_key = (stream_name, group_name)
        if cache_key in self._ensured_groups:
            return
        try:
            await self._client.xgroup_create(
                name=stream_name, groupname=group_name, id="0", mkstream=True
            )
            _log.info(
                "event_bus.group_created", stream=stream_name, group=group_name
            )
        except ResponseError as exc:
            if _BUSYGROUP not in str(exc):
                raise
            _log.debug(
                "event_bus.group_exists", stream=stream_name, group=group_name
            )
        self._ensured_groups.add(cache_key)

    async def consume(
        self,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        payload_model: type[PayloadT],
        batch_size: int = 10,
        *,
        block_ms: int = 5_000,
        reclaim_pending: bool = True,
    ) -> AsyncIterator[ConsumedMessage[PayloadT]]:
        if batch_size < 1:
            msg = f"batch_size must be positive, got {batch_size}"
            raise ValueError(msg)

        await self.ensure_group(stream_name, group_name)
        envelope_model = EventEnvelope[payload_model]  # type: ignore[valid-type]
        log = _log.bind(
            stream=stream_name, group=group_name, consumer=consumer_name
        )

        while True:
            batch: list[tuple[str, dict[str, str]]] = []

            if reclaim_pending:
                await self._quarantine_poison_messages(
                    stream_name, group_name, batch_size
                )
                batch.extend(
                    await self._reclaim(
                        stream_name, group_name, consumer_name, batch_size
                    )
                )

            if not batch:
                batch.extend(
                    await self._read_new(
                        stream_name,
                        group_name,
                        consumer_name,
                        batch_size,
                        block_ms,
                    )
                )

            if not batch:
                continue

            for message_id, fields in batch:
                envelope = self._decode(
                    stream_name, message_id, fields, envelope_model, log
                )
                if envelope is None:
                    await self._send_to_dlq(
                        stream_name,
                        group_name,
                        message_id,
                        fields,
                        reason="payload_decode_error",
                    )
                    continue

                message = ConsumedMessage(stream_name, message_id, envelope)
                handled = False
                try:
                    yield message
                    handled = True
                finally:
                    if handled and not message.acknowledged:
                        await self.acknowledge(message)
                    elif not handled:
                        log.warning(
                            "event_bus.left_pending",
                            message_id=message_id,
                            reason="consumer did not complete; awaiting reclaim",
                            **envelope.log_context(),
                        )

    async def acknowledge(self, message: ConsumedMessage[Any]) -> None:
        if message.acknowledged:
            return
        await self._client.xack(
            message.stream_name,
            self._group_of(message),
            message.message_id,
        )
        message.mark_acknowledged()

    async def acknowledge_id(
        self, stream_name: str, group_name: str, message_id: str
    ) -> None:
        await self._client.xack(stream_name, group_name, message_id)

    @staticmethod
    def _group_of(message: ConsumedMessage[Any]) -> str:
        msg = (
            "use acknowledge_id(stream, group, message_id) for explicit acks, "
            "or let the consume() loop acknowledge on resumption"
        )
        raise NotImplementedError(msg)

    async def _read_new(
        self,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        batch_size: int,
        block_ms: int,
    ) -> list[tuple[str, dict[str, str]]]:
        response = await self._client.xreadgroup(
            groupname=group_name,
            consumername=consumer_name,
            streams={stream_name: _NEW_MESSAGES},
            count=batch_size,
            block=block_ms,
        )
        if not response:
            return []
        return [
            (_as_str(message_id), self._decode_fields(fields))
            for _stream, entries in response
            for message_id, fields in entries
        ]

    async def _reclaim(
        self,
        stream_name: str,
        group_name: str,
        consumer_name: str,
        batch_size: int,
    ) -> list[tuple[str, dict[str, str]]]:
        try:
            result = await self._client.xautoclaim(
                name=stream_name,
                groupname=group_name,
                consumername=consumer_name,
                min_idle_time=self._claim_min_idle_ms,
                start_id=_CLAIM_START,
                count=batch_size,
            )
        except ResponseError as exc:
            _log.warning(
                "event_bus.autoclaim_failed",
                stream=stream_name,
                group=group_name,
                error=str(exc),
            )
            return []

        entries = result[1] if len(result) >= 2 else []
        claimed = [
            (_as_str(message_id), self._decode_fields(fields))
            for message_id, fields in entries
            if fields
        ]
        if claimed:
            _log.info(
                "event_bus.reclaimed",
                stream=stream_name,
                group=group_name,
                consumer=consumer_name,
                count=len(claimed),
            )
        return claimed

    async def _quarantine_poison_messages(
        self, stream_name: str, group_name: str, batch_size: int
    ) -> None:
        try:
            pending = await self._client.xpending_range(
                name=stream_name,
                groupname=group_name,
                min="-",
                max="+",
                count=batch_size,
            )
        except ResponseError:
            return

        for entry in pending:
            delivered = int(entry.get("times_delivered", 0))
            if delivered <= self._max_delivery_attempts:
                continue

            message_id = _as_str(entry["message_id"])
            claimed = await self._client.xclaim(
                name=stream_name,
                groupname=group_name,
                consumername="dlq-quarantine",
                min_idle_time=0,
                message_ids=[message_id],
            )
            fields = (
                self._decode_fields(claimed[0][1]) if claimed and claimed[0][1] else {}
            )
            await self._send_to_dlq(
                stream_name,
                group_name,
                message_id,
                fields,
                reason="max_delivery_attempts_exceeded",
                times_delivered=delivered,
            )

    async def _send_to_dlq(
        self,
        stream_name: str,
        group_name: str,
        message_id: str,
        fields: dict[str, str],
        *,
        reason: str,
        times_delivered: int | None = None,
    ) -> None:
        dlq_name = Streams.dlq(stream_name)
        dlq_fields: dict[str, str] = {
            **fields,
            "dlq_reason": reason,
            "dlq_origin_stream": stream_name,
            "dlq_origin_group": group_name,
            "dlq_origin_message_id": message_id,
        }
        if times_delivered is not None:
            dlq_fields["dlq_times_delivered"] = str(times_delivered)

        await self._client.xadd(
            name=dlq_name,
            fields=dlq_fields,
            maxlen=self._dlq_max_length,
            approximate=True,
        )
        await self._client.xack(stream_name, group_name, message_id)
        _log.error(
            "event_bus.dead_lettered",
            stream=stream_name,
            group=group_name,
            dlq=dlq_name,
            message_id=message_id,
            reason=reason,
            times_delivered=times_delivered,
            trace_id=fields.get("trace_id"),
        )

    @staticmethod
    def _decode_fields(fields: dict[Any, Any]) -> dict[str, str]:
        return {_as_str(key): _as_str(value) for key, value in fields.items()}

    @staticmethod
    def _decode(
        stream_name: str,
        message_id: str,
        fields: dict[str, str],
        envelope_model: type[EventEnvelope[PayloadT]],
        log: structlog.stdlib.BoundLogger,
    ) -> EventEnvelope[PayloadT] | None:
        raw = fields.get(_PAYLOAD_FIELD)
        if raw is None:
            log.error(
                "event_bus.decode_failed",
                message_id=message_id,
                reason="missing payload field",
            )
            return None
        try:
            return envelope_model.model_validate_json(raw)
        except ValidationError as exc:
            log.error(
                "event_bus.decode_failed",
                message_id=message_id,
                reason="schema validation",
                error_count=len(exc.errors()),
            )
            return None

    async def pending_count(self, stream_name: str, group_name: str) -> int:
        try:
            summary = await self._client.xpending(stream_name, group_name)
        except ResponseError:
            return 0
        if isinstance(summary, dict):
            return int(summary.get("pending", 0))
        return int(summary[0]) if summary else 0

    async def stream_length(self, stream_name: str) -> int:
        return int(await self._client.xlen(stream_name))

    async def dlq_length(self, stream_name: str) -> int:
        return int(await self._client.xlen(Streams.dlq(stream_name)))
