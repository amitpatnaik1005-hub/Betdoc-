"""PhantomManager tests: every engine checked against an independent reference implementation."""

import json
import math
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.domain.phantom.errors import PhantomDomainError
from app.domain.phantom.manager import CointegrationSignal, MatchedBettingMode
from app.models.phantom import PhantomCalculationLogModel
from app.schemas.phantom import (
    CointegrationRequest,
    PhantomCalculationLogRead,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- reference implementations


def ref_market_maker(s, q, gamma, sigma, big_t, t, k):
    dt = max(0.0, big_t - t)
    r = s - q * gamma * sigma**2 * dt
    spread = gamma * sigma**2 * dt + (2.0 / gamma) * math.log(1.0 + gamma / k)
    return {"r": r, "spread": spread, "ask": round(r + spread / 2, 2), "bid": round(r - spread / 2, 2)}


def _mb_tolerance(lay_odds: float) -> float:
    # Lay stake rounding (<= 0.005) propagated through the P&L formulas, plus P&L rounding (<= 0.01).
    return 0.005 * lay_odds + 0.01


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# ---------------------------------------------------------------- storage


@pytest.mark.parametrize(
    ("calc_type", "inputs_json"),
    [("DUTCHING", "{"), ("NOT_A_TYPE", "{}")],
)
async def test_storage_rejects_invalid_calculation_logs(session_factory, calc_type, inputs_json):
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(PhantomCalculationLogModel).values(
                    id=uuid4(), calc_type=calc_type, inputs_json=inputs_json, outputs_json="{}"
                )
            )
        await session.rollback()


async def test_integrity_error_is_rolled_back_and_wrapped(manager, db_session, monkeypatch):
    calls = {"rollback": 0}
    real_rollback = db_session.rollback

    async def failing_commit():
        raise IntegrityError("INSERT", {}, Exception("simulated constraint failure"))

    async def counting_rollback():
        calls["rollback"] += 1
        await real_rollback()

    monkeypatch.setattr(db_session, "commit", failing_commit)
    monkeypatch.setattr(db_session, "rollback", counting_rollback)
    with pytest.raises(PhantomDomainError, match="storage constraints"):
        await manager.calculate_dutching(db_session, 100.0, [2.0, 3.0])
    assert calls["rollback"] == 1


# ---------------------------------------------------------------- dutching


@pytest.mark.parametrize(
    ("odds", "target"),
    [([2.0, 4.0, 4.0], 100.0), ([2.5, 3.5, 6.0], 100.0), ([1.5, 5.0], 37.5), ([11.0] * 10, 250.0)],
)
async def test_dutching_equal_payout(manager, db_session, odds, target):
    outcome = await manager.calculate_dutching(db_session, target, odds)
    result = outcome["result"]
    total_implied = sum(1.0 / o for o in odds)

    assert result["total_implied_probability"] == pytest.approx(total_implied)
    assert result["guaranteed_return"] == round(target / total_implied, 2)
    assert result["stakes"] == [round((1.0 / o / total_implied) * target, 2) for o in odds]
    returns = [s * o for s, o in zip(result["stakes"], odds, strict=True)]
    assert max(returns) - min(returns) <= 0.01 * max(odds)
    assert result["profit"] == pytest.approx(result["guaranteed_return"] - target, abs=0.01)


async def test_dutching_persists_log_and_decodes_json(manager, db_session):
    outcome = await manager.calculate_dutching(db_session, 100.0, [2.0, 4.0, 4.0])
    log = PhantomCalculationLogRead.model_validate(outcome["log"])

    assert log.calc_type == "DUTCHING"
    assert log.inputs == {"target_total_stake": 100.0, "odds": [2.0, 4.0, 4.0]}
    assert log.outputs["stakes"] == [50.0, 25.0, 25.0]
    assert log.created_at is not None
    assert json.loads(outcome["log"].outputs_json)["guaranteed_return"] == 100.0


async def test_log_read_validator_handles_dicts_and_bad_json():
    parsed = PhantomCalculationLogRead.model_validate(
        {
            "id": uuid4(),
            "calc_type": "COINTEGRATION",
            "inputs_json": '{"a": 1}',
            "outputs_json": '{"b": 2}',
            "created_at": "2026-09-30T12:00:00+00:00",
        }
    )
    assert parsed.inputs == {"a": 1}
    assert parsed.outputs == {"b": 2}

    with pytest.raises(ValidationError):
        PhantomCalculationLogRead.model_validate(
            {
                "id": uuid4(),
                "calc_type": "COINTEGRATION",
                "inputs_json": "not-json",
                "outputs_json": "{}",
                "created_at": "2026-09-30T12:00:00+00:00",
            }
        )


@pytest.mark.parametrize(("odds", "target"), [([2.0], 100.0), ([2.0, 0.5], 100.0), ([2.0, 3.0], -1.0)])
async def test_dutching_rejects_invalid_inputs(manager, db_session, odds, target):
    with pytest.raises(PhantomDomainError):
        await manager.calculate_dutching(db_session, target, odds)
    assert await _count(db_session, PhantomCalculationLogModel) == 0


# ---------------------------------------------------------------- matched betting

MATCHED_CASES = [(10.0, 3.0, 3.2, 2.0), (25.0, 1.8, 1.85, 5.0), (100.0, 6.0, 6.4, 0.0), (50.0, 4.5, 4.4, 3.5)]


@pytest.mark.parametrize(("back_stake", "back_odds", "lay_odds", "commission"), MATCHED_CASES)
async def test_matched_betting_standard_equalises_profit(
    manager, db_session, back_stake, back_odds, lay_odds, commission
):
    outcome = await manager.calculate_matched_bet(
        db_session, back_stake, back_odds, lay_odds, commission, MatchedBettingMode.STANDARD
    )
    result = outcome["result"]
    c = commission / 100.0

    assert result["lay_stake"] == round((back_odds * back_stake) / (lay_odds - c), 2)
    assert abs(result["back_win_profit"] - result["lay_win_profit"]) <= _mb_tolerance(lay_odds)
    assert result["lay_liability"] == pytest.approx(result["lay_stake"] * (lay_odds - 1.0), abs=0.01)


@pytest.mark.parametrize(("back_stake", "back_odds", "lay_odds", "commission"), MATCHED_CASES)
async def test_matched_betting_underlay_zero_when_lay_wins(
    manager, db_session, back_stake, back_odds, lay_odds, commission
):
    outcome = await manager.calculate_matched_bet(
        db_session, back_stake, back_odds, lay_odds, commission, MatchedBettingMode.UNDERLAY
    )
    result = outcome["result"]

    assert result["lay_stake"] == round(back_stake / (1.0 - commission / 100.0), 2)
    assert abs(result["lay_win_profit"]) <= _mb_tolerance(lay_odds)


@pytest.mark.parametrize(("back_stake", "back_odds", "lay_odds", "commission"), MATCHED_CASES)
async def test_matched_betting_overlay_zero_when_back_wins(
    manager, db_session, back_stake, back_odds, lay_odds, commission
):
    outcome = await manager.calculate_matched_bet(
        db_session, back_stake, back_odds, lay_odds, commission, MatchedBettingMode.OVERLAY
    )
    result = outcome["result"]

    assert result["lay_stake"] == round((back_stake * (back_odds - 1.0)) / (lay_odds - 1.0), 2)
    assert abs(result["back_win_profit"]) <= _mb_tolerance(lay_odds)


async def test_matched_betting_mode_lay_stakes_are_ordered(manager, db_session):
    stakes = {}
    for mode in MatchedBettingMode:
        outcome = await manager.calculate_matched_bet(db_session, 10.0, 3.0, 3.2, 2.0, mode)
        stakes[mode] = outcome["result"]["lay_stake"]
    assert stakes[MatchedBettingMode.OVERLAY] < stakes[MatchedBettingMode.STANDARD]
    assert stakes[MatchedBettingMode.STANDARD] < stakes[MatchedBettingMode.UNDERLAY]


async def test_matched_betting_logs_inputs_and_outputs(manager, db_session):
    outcome = await manager.calculate_matched_bet(db_session, 10.0, 3.0, 3.2, 2.0, "STANDARD")
    log = PhantomCalculationLogRead.model_validate(outcome["log"])

    assert log.calc_type == "MATCHED_BETTING"
    assert log.inputs["mode"] == "STANDARD"
    assert log.inputs["lay_commission_pct"] == 2.0
    assert log.outputs["lay_stake"] == outcome["result"]["lay_stake"]


@pytest.mark.parametrize(
    ("back_stake", "back_odds", "lay_odds", "commission", "mode"),
    [
        (10.0, 3.0, 1.0, 2.0, "OVERLAY"),    # lay_odds == 1.0: division-by-zero guard
        (10.0, 3.0, 1.0, 2.0, "STANDARD"),
        (10.0, 3.0, 0.5, 2.0, "STANDARD"),
        (10.0, 3.0, 3.2, 100.0, "UNDERLAY"),
        (0.0, 3.0, 3.2, 2.0, "STANDARD"),
        (10.0, 1.0, 3.2, 2.0, "STANDARD"),
        (10.0, 3.0, 3.2, 2.0, "SIDEWAYS"),
        (10.0, float("inf"), 3.2, 2.0, "STANDARD"),
    ],
)
async def test_matched_betting_rejects_invalid_inputs(
    manager, db_session, back_stake, back_odds, lay_odds, commission, mode
):
    with pytest.raises(PhantomDomainError):
        await manager.calculate_matched_bet(db_session, back_stake, back_odds, lay_odds, commission, mode)
    assert await _count(db_session, PhantomCalculationLogModel) == 0


# ---------------------------------------------------------------- Avellaneda-Stoikov

MARKET_MAKER_CASES = [
    # mid, inventory, gamma, sigma, T, t, k
    (2.0, 0.0, 0.1, 0.2, 1.0, 0.0, 1.5),
    (2.0, 5.0, 0.1, 0.2, 1.0, 0.0, 1.5),
    (2.0, -5.0, 0.1, 0.2, 1.0, 0.0, 1.5),
    (3.5, 10.0, 0.5, 0.05, 10.0, 2.5, 0.8),
    (101.25, -3.0, 0.01, 1.2, 1.0, 0.25, 140.0),
    (1.8, 2.0, 2.0, 0.3, 5.0, 5.0, 3.0),     # dt == 0
    (1.8, 2.0, 2.0, 0.3, 5.0, 9.0, 3.0),     # t beyond horizon -> dt clamps to 0
    (4.0, 1.0, 1e-9, 0.2, 1.0, 0.0, 1.5),    # tiny gamma: log1p keeps precision
]


@pytest.mark.parametrize(("mid", "q", "gamma", "sigma", "big_t", "t", "k"), MARKET_MAKER_CASES)
async def test_market_maker_matches_reference(manager, db_session, mid, q, gamma, sigma, big_t, t, k):
    outcome = await manager.calculate_market_maker_quotes(db_session, mid, q, gamma, sigma, big_t, t, k)
    result = outcome["result"]
    ref = ref_market_maker(mid, q, gamma, sigma, big_t, t, k)

    assert result["time_remaining"] == pytest.approx(max(0.0, big_t - t))
    assert result["reservation_price"] == pytest.approx(ref["r"], rel=1e-9)
    assert result["optimal_spread"] == pytest.approx(ref["spread"], rel=1e-6)
    assert result["optimal_ask"] == pytest.approx(ref["ask"], abs=0.01)
    assert result["optimal_bid"] == pytest.approx(ref["bid"], abs=0.01)
    assert result["optimal_ask"] >= result["optimal_bid"]
    assert result["inventory_skew"] == pytest.approx(ref["r"] - mid, rel=1e-9, abs=1e-12)


@pytest.mark.parametrize(("inventory", "direction"), [(5.0, -1), (-5.0, 1), (0.0, 0)])
async def test_market_maker_inventory_skews_reservation_price(manager, db_session, inventory, direction):
    outcome = await manager.calculate_market_maker_quotes(db_session, 2.0, inventory, 0.1, 0.2, 1.0, 0.0, 1.5)
    skew = outcome["result"]["reservation_price"] - 2.0
    assert (skew > 0) - (skew < 0) == direction


async def test_market_maker_spread_widens_with_volatility(manager, db_session):
    spreads = []
    for sigma in (0.0, 0.1, 0.5, 1.0, 2.0):
        outcome = await manager.calculate_market_maker_quotes(db_session, 2.0, 0.0, 0.3, sigma, 2.0, 0.0, 1.5)
        spreads.append(outcome["result"]["optimal_spread"])
    assert spreads == sorted(spreads)
    assert len(set(spreads)) == len(spreads)


async def test_market_maker_zero_time_remaining_uses_liquidity_term_only(manager, db_session):
    outcome = await manager.calculate_market_maker_quotes(db_session, 2.0, 7.0, 0.4, 0.9, 1.0, 1.0, 2.0)
    result = outcome["result"]
    assert result["reservation_price"] == pytest.approx(2.0)
    assert result["optimal_spread"] == pytest.approx((2.0 / 0.4) * math.log(1.0 + 0.4 / 2.0))


async def test_market_maker_persists_log(manager, db_session):
    outcome = await manager.calculate_market_maker_quotes(db_session, 2.0, 1.0, 0.1, 0.2, 1.0, 0.0, 1.5)
    log = PhantomCalculationLogRead.model_validate(outcome["log"])
    assert log.calc_type == "AVELLANEDA"
    assert log.inputs["liquidity_k"] == 1.5
    assert log.outputs["optimal_ask"] == outcome["result"]["optimal_ask"]


@pytest.mark.parametrize(
    ("gamma", "sigma", "k"),
    [(0.0, 0.2, 1.5), (-0.1, 0.2, 1.5), (0.1, 0.2, 0.0), (0.1, 0.2, -1.0), (0.1, -0.2, 1.5), (0.1, 1e200, 1.5)],
)
async def test_market_maker_rejects_invalid_or_overflowing_inputs(manager, db_session, gamma, sigma, k):
    with pytest.raises(PhantomDomainError):
        await manager.calculate_market_maker_quotes(db_session, 2.0, 1.0, gamma, sigma, 1.0, 0.0, k)
    assert await _count(db_session, PhantomCalculationLogModel) == 0


# ---------------------------------------------------------------- cointegration

ENTRY, EXIT, STOP = 2.0, 0.5, 3.0


@pytest.mark.parametrize(
    ("z_score", "expected"),
    [
        (3.5, CointegrationSignal.STOP_LOSS_LIQUIDATE),
        (-3.5, CointegrationSignal.STOP_LOSS_LIQUIDATE),
        (3.0, CointegrationSignal.STOP_LOSS_LIQUIDATE),   # boundary is inclusive
        (-3.0, CointegrationSignal.STOP_LOSS_LIQUIDATE),
        (2.5, CointegrationSignal.SELL_SPREAD),
        (-2.5, CointegrationSignal.BUY_SPREAD),
        (2.0, CointegrationSignal.HOLD),                  # exactly at entry: not strictly greater
        (1.0, CointegrationSignal.HOLD),
        (-1.0, CointegrationSignal.HOLD),
        (0.5, CointegrationSignal.CLOSE_POSITION),        # boundary is inclusive
        (-0.3, CointegrationSignal.CLOSE_POSITION),
        (0.0, CointegrationSignal.CLOSE_POSITION),
    ],
)
async def test_cointegration_signals(manager, db_session, z_score, expected):
    outcome = await manager.evaluate_cointegration(db_session, z_score, ENTRY, EXIT, STOP)
    result = outcome["result"]
    assert result["signal"] == expected.value
    assert result["abs_z_score"] == pytest.approx(abs(z_score))
    assert result["distance_to_stop_loss"] == pytest.approx(STOP - abs(z_score))


async def test_cointegration_thresholds_are_fully_dynamic(manager, db_session):
    tight = await manager.evaluate_cointegration(db_session, 1.5, 1.0, 0.2, 1.4)
    loose = await manager.evaluate_cointegration(db_session, 1.5, 1.0, 0.2, 4.0)
    assert tight["result"]["signal"] == "STOP_LOSS_LIQUIDATE"
    assert loose["result"]["signal"] == "SELL_SPREAD"


async def test_cointegration_persists_log(manager, db_session):
    outcome = await manager.evaluate_cointegration(db_session, 3.2, ENTRY, EXIT, STOP)
    log = PhantomCalculationLogRead.model_validate(outcome["log"])
    assert log.calc_type == "COINTEGRATION"
    assert log.outputs["signal"] == "STOP_LOSS_LIQUIDATE"
    assert log.inputs["stop_loss_threshold"] == STOP


@pytest.mark.parametrize(
    ("entry", "exit_", "stop"),
    [(2.0, 2.0, 3.0), (2.0, 2.5, 3.0), (3.0, 0.5, 3.0), (4.0, 0.5, 3.0), (2.0, -0.1, 3.0)],
)
async def test_cointegration_rejects_invalid_threshold_ordering(manager, db_session, entry, exit_, stop):
    with pytest.raises(PhantomDomainError):
        await manager.evaluate_cointegration(db_session, 1.0, entry, exit_, stop)
    with pytest.raises(ValidationError):
        CointegrationRequest(
            current_z_score=1.0, entry_threshold=entry, exit_threshold=exit_, stop_loss_threshold=stop
        )
