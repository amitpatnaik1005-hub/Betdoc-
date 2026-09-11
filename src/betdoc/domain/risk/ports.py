from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from betdoc.domain.volatility.models import VolatilityAssessment


@runtime_checkable
class ExposureLedgerPort(Protocol):
    async def realized_loss_today_paise(self, *, profile_id: str, as_of: datetime) -> int: ...
    async def open_exposure_paise(self, *, profile_id: str, sport_type: str) -> int: ...

    async def record_placement(self, *, profile_id: str, sport_type: str, stake_paise: int, idempotency_key: str) -> None: ...
    async def record_settlement(self, *, profile_id: str, sport_type: str, stake_paise: int, payout_paise: int, settled_at: datetime, idempotency_key: str) -> None: ...

@runtime_checkable
class VolatilityOraclePort(Protocol):
    def assess(self, instrument_key: str) -> VolatilityAssessment | None: ...
