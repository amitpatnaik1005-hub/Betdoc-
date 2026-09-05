"""Mock Data Generator for infinite, zero-cost arbitrage testing."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from uuid import uuid4

from betdoc.application.ports.bookmaker_client import BaseBookmakerAdapter, RawPayload
from betdoc.domain.models.odds import (
    MarketType,
    MoneylineMarket,
    MoneylineSelection,
    OddsTick,
    OutcomeSide,
    SourceTransport,
    utc_now,
)

class MockAdapter(BaseBookmakerAdapter):
    """
    Generates fake, mathematically coherent OddsTicks infinitely.
    Costs 0 API calls. Perfect for testing arbitrage logic and UI.
    """

    def __init__(self, bookmaker_name: str = "mock_bookie", poll_interval: float = 2.0):
        super().__init__(bookmaker=bookmaker_name, transport=SourceTransport.REPLAY)
        self._poll_interval = poll_interval
        self._is_connected = False
        self._event_id = str(uuid4())

    async def connect(self) -> None:
        self._is_connected = True

    async def close(self) -> None:
        self._is_connected = False

    async def is_connected(self) -> bool:
        return self._is_connected

    def normalize_payload(self, payload: RawPayload) -> OddsTick:
        raise NotImplementedError("MockAdapter generates its own ticks directly.")

    async def stream_live_ticks(
        self,
        *,
        sports: Sequence[str] | None = None,
        markets: Sequence[str] | None = None,
    ) -> AsyncIterator[OddsTick]:
        
        await self.connect()
        
        while self._is_connected:
            now = utc_now()
            
            # Generate fluctuating fake odds
            home_prob = random.uniform(0.4, 0.6)
            draw_prob = random.uniform(0.2, 0.3)
            away_prob = 1.0 - (home_prob + draw_prob)
            
            # Add a random bookmaker margin (overround) between 2% and 6%
            margin = random.uniform(1.02, 1.06)
            
            home_odds = round((1.0 / home_prob) / margin, 2)
            draw_odds = round((1.0 / draw_prob) / margin, 2)
            away_odds = round((1.0 / away_prob) / margin, 2)

            # Ensure minimum odds of 1.01
            home_odds = max(1.01, home_odds)
            draw_odds = max(1.01, draw_odds)
            away_odds = max(1.01, away_odds)

            # Build a perfect Fable Hexagonal Market object
            market = MoneylineMarket(
                key="h2h",
                last_update=now,
                runners=(
                    MoneylineSelection(name="Arsenal", outcome=OutcomeSide.HOME, price=home_odds),
                    MoneylineSelection(name="Draw", outcome=OutcomeSide.DRAW, price=draw_odds),
                    MoneylineSelection(name="Chelsea", outcome=OutcomeSide.AWAY, price=away_odds),
                )
            )

            # Build the Master Tick Payload
            tick = OddsTick(
                bookmaker=self.bookmaker,
                transport=self.transport,
                event_id=self._event_id,
                sport_key="soccer_epl",
                home_team="Arsenal",
                away_team="Chelsea",
                commence_time=now + timedelta(hours=24),
                is_live=False,
                markets=(market,),
                bookmaker_timestamp=now,
                received_at=now,
                received_monotonic_ns=time.monotonic_ns()
            )

            yield tick
            await asyncio.sleep(self._poll_interval)
