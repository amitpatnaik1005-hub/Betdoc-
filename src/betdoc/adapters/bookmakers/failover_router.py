"""The Failover Router: Seamlessly switches between APIs when quotas run out."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

from betdoc.application.ports.bookmaker_client import (
    AuthenticationError,
    BaseBookmakerAdapter,
    RateLimitedError,
    RawPayload,
)
from betdoc.domain.models.odds import OddsTick, SourceTransport

logger = logging.getLogger(__name__)

class FailoverRouter(BaseBookmakerAdapter):
    """
    Acts as a single 'Bookmaker Adapter' to the rest of the application, 
    but internally manages a list of fallback adapters.
    
    If the active adapter hits a rate limit (429) or an auth error (quota expired),
    this router catches the error, silently flips the switch to the next adapter
    in the chain, and continues streaming data without crashing the engine.
    """

    def __init__(self, adapters: Sequence[BaseBookmakerAdapter]) -> None:
        if not adapters:
            raise ValueError("FailoverRouter requires at least one adapter.")
            
        super().__init__(bookmaker="failover_router", transport=SourceTransport.REST)
        self._adapters = list(adapters)
        self._active_index = 0

    async def connect(self) -> None:
        if self._active_index < len(self._adapters):
            await self._adapters[self._active_index].connect()

    async def close(self) -> None:
        for adapter in self._adapters:
            await adapter.close()

    async def is_connected(self) -> bool:
        if self._active_index < len(self._adapters):
            return await self._adapters[self._active_index].is_connected()
        return False

    def normalize_payload(self, payload: RawPayload) -> OddsTick:
        # Pass the payload to the currently active adapter
        return self._adapters[self._active_index].normalize_payload(payload)

    async def stream_live_ticks(
        self,
        *,
        sports: Sequence[str] | None = None,
        markets: Sequence[str] | None = None,
    ) -> AsyncIterator[OddsTick]:
        
        # Loop through our chain of APIs
        while self._active_index < len(self._adapters):
            active_adapter = self._adapters[self._active_index]
            logger.info("==================================================")
            logger.info(f"🔄 ROUTER SWITCH: Now routing traffic to -> {active_adapter.bookmaker.upper()}")
            logger.info("==================================================")
            
            try:
                # Stream data from the current API
                async for tick in active_adapter.stream_live_ticks(sports=sports, markets=markets):
                    yield tick
                    
            except (RateLimitedError, AuthenticationError) as exc:
                # The API ran out of quota! Flip the switch.
                logger.warning(f"⚠️ QUOTA EXHAUSTED for {active_adapter.bookmaker}: {exc}")
                logger.warning("🔌 Flipping switch to the next available API in the chain...")
                self._active_index += 1
                
            except Exception as exc:
                # Some other terminal error occurred with this API.
                logger.error(f"❌ API {active_adapter.bookmaker} failed critically: {exc}")
                logger.error("🔌 Flipping switch to the next available API...")
                self._active_index += 1
                
        # If we exit the while loop, it means ALL APIs in the chain are dead.
        logger.error("🛑 FATAL: All APIs in the failover chain have been exhausted.")
        raise RuntimeError("Failover Router exhausted all available API adapters.")
