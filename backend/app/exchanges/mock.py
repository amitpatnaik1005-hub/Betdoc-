import asyncio
import random
import uuid
from decimal import Decimal

from app.exchanges.base import BaseExchangeAdapter, ExchangeRejectionError


class MockExchangeAdapter(BaseExchangeAdapter):
    async def place_bet(
        self,
        match_id: str,
        selection: str,
        odds: Decimal,
        stake: Decimal,
    ) -> str:
        await asyncio.sleep(0.2)

        roll = random.random()
        if roll < 0.10:
            raise TimeoutError("Exchange API timed out")
        if roll < 0.20:
            raise ExchangeRejectionError("Insufficient exchange balance")

        return f"mock_{uuid.uuid4().hex[:8]}"
