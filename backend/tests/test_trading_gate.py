"""Control Panel limits are enforced on every order (emergency stop + max bet size)."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.execution_service import _enforce_trading_gate


class FakeDb:
    def __init__(self, controls: object | None) -> None:
        self.controls = controls

    async def get(self, model, key):  # noqa: ANN001
        return self.controls


def _controls(*, max_daily_exposure: float = 500.0, max_bet_size: float = 50.0) -> SimpleNamespace:
    return SimpleNamespace(max_daily_exposure=max_daily_exposure, max_bet_size=max_bet_size)


@pytest.mark.asyncio
async def test_emergency_stop_blocks_orders() -> None:
    with pytest.raises(HTTPException) as exc:
        await _enforce_trading_gate(FakeDb(_controls(max_daily_exposure=0.0)), Decimal("10"))
    assert exc.value.status_code == 423


@pytest.mark.asyncio
async def test_stake_above_max_bet_is_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        await _enforce_trading_gate(FakeDb(_controls(max_bet_size=50.0)), Decimal("50.01"))
    assert exc.value.status_code == 400 and "max bet size" in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("controls", [None, _controls()])
async def test_orders_within_limits_pass(controls: object | None) -> None:
    await _enforce_trading_gate(FakeDb(controls), Decimal("50"))
