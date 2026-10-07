"""Async WebSocket connection manager for PROTOCOL/MESSAGE providers (DEVRAYA HFT lane).

Every frame is (1) published to Redis Pub/Sub immediately and (2) batched into the immutable
raw landing zone. Run: ``python -m app.services.omni_ws_client``
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import signal
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from redis.asyncio import ConnectionPool, Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.typing import Subprotocol

from app.adapters.base_adapter import AdapterError, build_adapter
from app.core.config import Settings, get_settings
from app.core.logging import setup_json_logging
from app.core.security_vault import UnsafeTargetError, VaultCrypto, VaultDecryptionError, assert_public_target, get_vault_crypto
from app.models.omni_vault import (
    WS_STRATEGIES,
    OmniAuthStrategy,
    OmniProviderConfig,
    OmniRawPayload,
    SourceTimestampOrigin,
)

logger = logging.getLogger(__name__)

WS_HTTP_STATUS = 101  # Switching Protocols: marks WebSocket-origin rows in the landing zone


@dataclass(frozen=True, slots=True)
class _WsProvider:
    id: UUID
    name: str
    url: str
    strategy: OmniAuthStrategy
    api_key: str
    auth_payload: dict[str, Any] | None
    subscribe_payloads: list[Any]
    adapter_key: str | None
    normalization_spec: dict[str, Any] | None
    fingerprint: str


def inject_secret(template: object, placeholder: str, secret: str) -> object:
    """Recursively replace ``placeholder`` inside every string of a JSON template."""
    if isinstance(template, str):
        return template.replace(placeholder, secret)
    if isinstance(template, list):
        return [inject_secret(item, placeholder, secret) for item in template]
    if isinstance(template, dict):
        return {key: inject_secret(value, placeholder, secret) for key, value in template.items()}
    return template


class OmniWebSocketManager:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        vault: VaultCrypto,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._redis = redis
        self._vault = vault
        self._connections: dict[UUID, tuple[str, asyncio.Task[None]]] = {}
        self._buffer: asyncio.Queue[OmniRawPayload] = asyncio.Queue(maxsize=settings.omni_ws_queue_max)

    async def run(self, stop: asyncio.Event) -> None:
        writer = asyncio.create_task(self._writer_loop(stop), name="omni-ws-writer")
        try:
            while not stop.is_set():
                try:
                    self._reconcile(await self._load_providers())
                except SQLAlchemyError:
                    logger.exception("WS provider refresh failed; keeping current connections.")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._settings.omni_ws_refresh_seconds)
                except TimeoutError:
                    pass
        finally:
            tasks = [task for _, task in self._connections.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._connections.clear()
            await writer

    async def _load_providers(self) -> list[_WsProvider]:
        stmt = select(OmniProviderConfig).where(
            OmniProviderConfig.is_active.is_(True),
            OmniProviderConfig.auth_strategy.in_(list(WS_STRATEGIES)),
            OmniProviderConfig.ws_url.is_not(None),
            OmniProviderConfig.encrypted_api_key.is_not(None),
        )
        async with self._session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        providers: list[_WsProvider] = []
        for row in rows:
            try:
                key = self._vault.decrypt_key(row.encrypted_api_key or "")
            except VaultDecryptionError:
                logger.error("Cannot decrypt API key for WS provider=%s; skipping.", row.provider_name)
                continue
            providers.append(
                _WsProvider(
                    id=row.id,
                    name=row.provider_name,
                    url=row.ws_url or "",
                    strategy=row.auth_strategy,
                    api_key=key,
                    auth_payload=row.ws_auth_payload,
                    subscribe_payloads=list(row.ws_subscribe_payloads or []),
                    adapter_key=row.adapter_key,
                    normalization_spec=row.normalization_spec,
                    fingerprint=row.updated_at.isoformat(),
                )
            )
        return providers

    def _reconcile(self, providers: list[_WsProvider]) -> None:
        desired = {p.id: p for p in providers}
        for pid in list(self._connections):
            fingerprint, task = self._connections[pid]
            if pid not in desired or desired[pid].fingerprint != fingerprint or task.done():
                task.cancel()
                del self._connections[pid]
        for pid, provider in desired.items():
            if pid not in self._connections:
                task = asyncio.create_task(self._connection_loop(provider), name=f"omni-ws-{provider.name}")
                self._connections[pid] = (provider.fingerprint, task)

    async def _connection_loop(self, provider: _WsProvider) -> None:
        settings = self._settings
        schemes = frozenset({"wss", "ws"} if settings.omni_allow_insecure_http else {"wss"})
        placeholder = settings.omni_ws_key_placeholder
        attempts = 0
        while True:
            try:
                await asyncio.to_thread(
                    assert_public_target, provider.url, allowed_schemes=schemes, allow_private=settings.omni_allow_private_networks
                )
                subprotocols = [Subprotocol(provider.api_key)] if provider.strategy is OmniAuthStrategy.PROTOCOL else None
                async with connect(
                    provider.url,
                    subprotocols=subprotocols,
                    open_timeout=settings.omni_ws_open_timeout_seconds,
                    ping_interval=settings.omni_ws_ping_interval_seconds,
                    max_size=settings.omni_ws_max_message_bytes,
                    user_agent_header=settings.omni_user_agent,
                ) as ws:
                    attempts = 0
                    logger.info("WS connected provider=%s", provider.name)
                    if provider.strategy is OmniAuthStrategy.MESSAGE:
                        if provider.auth_payload is None:
                            logger.error("MESSAGE strategy without ws_auth_payload for provider=%s", provider.name)
                            return
                        await ws.send(json.dumps(inject_secret(provider.auth_payload, placeholder, provider.api_key)))
                    for frame in provider.subscribe_payloads:
                        await ws.send(json.dumps(inject_secret(frame, placeholder, provider.api_key)))
                    async for message in ws:
                        await self._handle(provider, message)
                logger.warning("WS closed by server provider=%s", provider.name)
            except asyncio.CancelledError:
                raise
            except UnsafeTargetError as exc:
                logger.error("Blocked unsafe WS target provider=%s: %s", provider.name, exc)
                return
            except (ConnectionClosed, InvalidHandshake, InvalidURI, OSError, TimeoutError) as exc:
                logger.warning("WS error provider=%s: %s", provider.name, type(exc).__name__)
            ceiling = min(settings.omni_ws_reconnect_max_seconds, settings.omni_ws_reconnect_base_seconds * 2**attempts)
            attempts += 1
            await asyncio.sleep(ceiling / 2 + random.random() * ceiling / 2)

    async def _handle(self, provider: _WsProvider, message: str | bytes) -> None:
        raw = message.encode("utf-8") if isinstance(message, str) else bytes(message)
        try:
            payload: object = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Dropping non-JSON WS frame from provider=%s", provider.name)
            return
        if not isinstance(payload, (dict, list)):
            return

        ingestion_ts = datetime.now(UTC)
        source_ts: datetime | None = None
        event_json: dict[str, Any] | None = None
        try:
            adapter = build_adapter(provider.adapter_key, provider.id, provider.normalization_spec)
            if adapter is not None:
                source_ts = adapter.extract_source_timestamp(payload, {})
                if isinstance(payload, dict):
                    event = adapter.normalize_payload(payload)
                    event_json = event.model_copy(
                        update={"source_timestamp": source_ts or ingestion_ts, "provider_id": provider.id}
                    ).model_dump(mode="json")
        except AdapterError as exc:
            logger.debug("WS normalisation failed provider=%s: %s", provider.name, exc)

        origin = SourceTimestampOrigin.PAYLOAD if source_ts else SourceTimestampOrigin.INGESTION
        record = OmniRawPayload(
            provider_id=provider.id,
            endpoint_id=None,
            endpoint_path=urlsplit(provider.url).path or "/",
            transport="ws",
            raw_payload=payload,
            is_json=True,
            content_type="application/json",
            payload_hash=hashlib.sha256(raw).hexdigest(),
            source_timestamp=source_ts or ingestion_ts,
            source_timestamp_origin=origin.value,
            ingestion_timestamp=ingestion_ts,
            latency_ms=None,
            http_status=WS_HTTP_STATUS,
        )
        await self._buffer.put(record)  # backpressure instead of silent drops

        envelope = {
            "kind": "omni.ws",
            "provider_id": str(provider.id),
            "provider_name": provider.name,
            "payload_hash": record.payload_hash,
            "source_timestamp": record.source_timestamp.isoformat(),
            "ingestion_timestamp": ingestion_ts.isoformat(),
            "event": event_json,
            "payload": payload if len(raw) <= self._settings.omni_publish_max_bytes else None,
            "payload_truncated": len(raw) > self._settings.omni_publish_max_bytes,
        }
        try:
            await self._redis.publish(self._settings.omni_live_channel, json.dumps(envelope, default=str, separators=(",", ":")))
        except RedisError:
            logger.exception("WS publish failed provider=%s (frame is buffered for persistence)", provider.name)

    async def _writer_loop(self, stop: asyncio.Event) -> None:
        pending: list[OmniRawPayload] = []
        while not (stop.is_set() and self._buffer.empty() and not pending):
            try:
                record = await asyncio.wait_for(self._buffer.get(), timeout=self._settings.omni_ws_flush_interval_seconds)
                pending.append(record)
                while len(pending) < self._settings.omni_ws_batch_size and not self._buffer.empty():
                    pending.append(self._buffer.get_nowait())
            except TimeoutError:
                pass
            if pending and (len(pending) >= self._settings.omni_ws_batch_size or self._buffer.empty()):
                try:
                    async with self._session_factory() as session:
                        session.add_all(pending)
                        await session.commit()
                    pending = []
                except SQLAlchemyError:
                    logger.exception("WS batch persist failed (%d rows); retrying next flush.", len(pending))
                    if stop.is_set():
                        logger.error("Shutdown with %d unpersisted WS frames.", len(pending))
                        return
                    await asyncio.sleep(self._settings.omni_ws_flush_interval_seconds)


async def main() -> None:
    settings = get_settings()
    setup_json_logging()
    engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), pool_pre_ping=True)
    pool: ConnectionPool = ConnectionPool.from_url(settings.celery_broker_url.get_secret_value(), decode_responses=True)
    redis = Redis(connection_pool=pool)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    import sys
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    else:
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        await OmniWebSocketManager(
            settings=settings,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            redis=redis,
            vault=get_vault_crypto(),
        ).run(stop)
    finally:
        await redis.aclose()
        await pool.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
