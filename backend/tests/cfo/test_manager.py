"""CfoManager tests: dynamic advisory, stress maths, tax race recovery, alerts."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from app.domain.cfo.errors import AlertNotFoundError, CfoDomainError
from app.models.cfo import CfoAdvisoryModel, CfoAlertModel, StressTestResultModel, TaxRecordModel
from app.schemas.cfo import AdvisoryResponse, AlertRead, StressTestResult, TaxRecordRead

pytestmark = pytest.mark.asyncio


async def _count(session_factory, model, *criteria) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    async with session_factory() as session:
        return (await session.execute(statement)).scalar_one()


# ---------------------------------------------------------------- advisory


async def test_advisory_stable_health_score_and_json_roundtrip(manager, db_session, session_factory):
    advisory = await manager.generate_advisory_report(
        db_session, user_id=None, current_bankroll=1000.0, active_exposure=100.0, variance_threshold_pct=30.0
    )
    response = AdvisoryResponse.model_validate(advisory)

    assert response.capital_health_score == pytest.approx(85.0)  # 100 - (10% * 1.5)
    assert response.variance_status == "STABLE"
    assert response.suggestions == json.loads(advisory.suggestions_json)
    assert any("10.00%" in s and "30.00%" in s for s in response.suggestions)
    assert any("200.00" in s for s in response.suggestions)  # remaining capacity: 300 - 100
    assert response.created_at is not None
    assert await _count(session_factory, CfoAlertModel) == 0


async def test_advisory_high_variance_creates_warning_alert(manager, db_session, session_factory):
    advisory = await manager.generate_advisory_report(
        db_session, user_id=None, current_bankroll=1000.0, active_exposure=400.0, variance_threshold_pct=30.0
    )
    response = AdvisoryResponse.model_validate(advisory)

    assert response.capital_health_score == pytest.approx(40.0)  # 100 - (40% * 1.5)
    assert response.variance_status == "HIGH"
    assert any("100.00" in s for s in response.suggestions)  # cut 400 - 300
    assert await _count(session_factory, CfoAlertModel, CfoAlertModel.level == "WARNING") == 1


async def test_advisory_threshold_is_injected_not_hardcoded(manager, db_session):
    tight = await manager.generate_advisory_report(
        db_session, user_id=None, current_bankroll=1000.0, active_exposure=400.0, variance_threshold_pct=30.0
    )
    tight_status = tight.variance_status
    loose = await manager.generate_advisory_report(
        db_session, user_id=None, current_bankroll=1000.0, active_exposure=400.0, variance_threshold_pct=50.0
    )
    assert tight_status == "HIGH"
    assert loose.variance_status == "STABLE"


@pytest.mark.parametrize(
    ("bankroll", "exposure", "expected_score"),
    [(1000.0, 0.0, 100.0), (1000.0, 1000.0, 0.0), (500.0, 5000.0, 0.0), (2000.0, 200.0, 85.0)],
)
async def test_advisory_health_score_formula(manager, db_session, bankroll, exposure, expected_score):
    advisory = await manager.generate_advisory_report(
        db_session, user_id=None, current_bankroll=bankroll, active_exposure=exposure, variance_threshold_pct=100.0
    )
    assert advisory.capital_health_score == pytest.approx(expected_score)


@pytest.mark.parametrize(
    ("bankroll", "exposure", "threshold"),
    [(0.0, 10.0, 30.0), (-5.0, 10.0, 30.0), (100.0, -1.0, 30.0), (100.0, 10.0, 150.0), (float("nan"), 1.0, 30.0)],
)
async def test_advisory_rejects_invalid_inputs(manager, db_session, session_factory, bankroll, exposure, threshold):
    with pytest.raises(CfoDomainError):
        await manager.generate_advisory_report(
            db_session, user_id=None, current_bankroll=bankroll, active_exposure=exposure,
            variance_threshold_pct=threshold,
        )
    assert await _count(session_factory, CfoAdvisoryModel) == 0


async def test_advisory_response_validator_handles_dicts():
    now = datetime.now(UTC)
    parsed = AdvisoryResponse.model_validate(
        {"id": uuid4(), "capital_health_score": 50.0, "variance_status": "STABLE",
         "suggestions_json": '["a", "b"]', "created_at": now}
    )
    assert parsed.suggestions == ["a", "b"]

    with pytest.raises(ValidationError):
        AdvisoryResponse.model_validate(
            {"id": uuid4(), "capital_health_score": 50.0, "variance_status": "STABLE",
             "suggestions_json": "not-json", "created_at": now}
        )


# ---------------------------------------------------------------- stress test


async def test_stress_test_survives_below_threshold(manager, db_session, session_factory):
    result = await manager.run_stress_test(
        db_session, user_id=None, scenario="Weekend meltdown", portfolio_value=10_000.0,
        shock_pct=25.0, survival_threshold_pct=30.0,
    )
    snapshot = StressTestResult.model_validate(result)

    assert snapshot.simulated_pnl == pytest.approx(-2_500.0)
    assert snapshot.simulated_drawdown_pct == pytest.approx(25.0)
    assert snapshot.portfolio_value_before == pytest.approx(10_000.0)
    assert snapshot.survived is True
    assert snapshot.recommendation == "Buffer sufficient."
    assert snapshot.created_at is not None
    assert await _count(session_factory, CfoAlertModel) == 0


async def test_stress_test_fails_above_threshold_and_alerts(manager, db_session, session_factory):
    result = await manager.run_stress_test(
        db_session, user_id=None, scenario="Black swan", portfolio_value=10_000.0,
        shock_pct=40.0, survival_threshold_pct=30.0,
    )
    assert result.simulated_pnl == pytest.approx(-4_000.0)
    assert result.survived is False
    assert result.recommendation == "Catastrophic risk detected. Shock exceeds threshold of 30.0%."
    assert await _count(session_factory, CfoAlertModel, CfoAlertModel.level == "CRITICAL") == 1


async def test_stress_test_threshold_boundary_is_not_survived(manager, db_session):
    result = await manager.run_stress_test(
        db_session, user_id=None, scenario="Edge", portfolio_value=1_000.0, shock_pct=30.0,
        survival_threshold_pct=30.0,
    )
    assert result.survived is False


async def test_stress_test_zero_shock_has_zero_pnl(manager, db_session):
    result = await manager.run_stress_test(
        db_session, user_id=None, scenario="Calm", portfolio_value=1_000.0, shock_pct=0.0,
        survival_threshold_pct=10.0,
    )
    assert result.simulated_pnl == 0.0
    assert result.survived is True


@pytest.mark.parametrize(
    ("scenario", "portfolio", "shock", "threshold"),
    [("X", 0.0, 10.0, 30.0), ("X", 100.0, 101.0, 30.0), ("X", 100.0, 10.0, -1.0), ("   ", 100.0, 10.0, 30.0)],
)
async def test_stress_test_rejects_invalid_inputs(manager, db_session, session_factory, scenario, portfolio, shock,
                                                  threshold):
    with pytest.raises(CfoDomainError):
        await manager.run_stress_test(
            db_session, user_id=None, scenario=scenario, portfolio_value=portfolio, shock_pct=shock,
            survival_threshold_pct=threshold,
        )
    assert await _count(session_factory, StressTestResultModel) == 0


# ---------------------------------------------------------------- taxes


async def test_tax_applies_exact_dynamic_parameters(manager, db_session):
    record = await manager.calculate_tax(
        db_session, user_id=None, year=2026, total_profit=50_000.0, tax_allowance=12_570.0, tax_rate_pct=20.0
    )
    snapshot = TaxRecordRead.model_validate(record)
    assert snapshot.taxable_amount == pytest.approx(37_430.0)
    assert snapshot.estimated_tax == pytest.approx(7_486.0)
    assert snapshot.last_calculated_at is not None


async def test_tax_loss_produces_zero_liability(manager, db_session):
    record = await manager.calculate_tax(
        db_session, user_id=None, year=2026, total_profit=-500.0, tax_allowance=1_000.0, tax_rate_pct=45.0
    )
    assert record.taxable_amount == 0.0
    assert record.estimated_tax == 0.0


async def test_tax_recalculation_updates_same_row(manager, session_factory):
    user_id = uuid4()
    async with session_factory() as session:
        first = await manager.calculate_tax(session, user_id, 2026, 10_000.0, 2_000.0, 10.0)
        first_id = first.id
    async with session_factory() as session:
        second = await manager.calculate_tax(session, user_id, 2026, 20_000.0, 2_000.0, 25.0)
        second_id, estimated = second.id, second.estimated_tax

    assert second_id == first_id
    assert estimated == pytest.approx(4_500.0)
    assert await _count(session_factory, TaxRecordModel) == 1


async def test_tax_records_are_scoped_by_user_and_year(manager, db_session, session_factory):
    user_a, user_b = uuid4(), uuid4()
    await manager.calculate_tax(db_session, user_a, 2025, 1_000.0, 0.0, 10.0)
    await manager.calculate_tax(db_session, user_a, 2026, 1_000.0, 0.0, 10.0)
    await manager.calculate_tax(db_session, user_b, 2026, 1_000.0, 0.0, 10.0)
    await manager.calculate_tax(db_session, None, 2026, 1_000.0, 0.0, 10.0)
    assert await _count(session_factory, TaxRecordModel) == 4


@pytest.mark.parametrize("user_id", [uuid4(), None], ids=["known-user", "anonymous"])
async def test_tax_recovers_from_integrity_error_race(manager, session_factory, monkeypatch, user_id):
    winner_id = uuid4()
    async with session_factory() as winner:
        winner.add(
            TaxRecordModel(id=winner_id, user_id=user_id, year=2026, total_profit=1.0, taxable_amount=0.0,
                           estimated_tax=0.0)
        )
        await winner.commit()

    real_find = manager._find_tax_record
    calls = {"count": 0}

    async def stale_read_then_real(db, uid, year):
        calls["count"] += 1
        if calls["count"] == 1:
            return None  # read happened before the winner committed
        return await real_find(db, uid, year)

    monkeypatch.setattr(manager, "_find_tax_record", stale_read_then_real)

    async with session_factory() as loser:
        record = await manager.calculate_tax(loser, user_id, 2026, 50_000.0, 10_000.0, 20.0)
        snapshot = TaxRecordRead.model_validate(record)

    assert calls["count"] == 2  # stale read, IntegrityError, rollback, re-fetch
    assert snapshot.id == winner_id
    assert snapshot.total_profit == pytest.approx(50_000.0)
    assert snapshot.taxable_amount == pytest.approx(40_000.0)
    assert snapshot.estimated_tax == pytest.approx(8_000.0)
    assert await _count(session_factory, TaxRecordModel) == 1


async def test_anonymous_tax_rows_are_unique_per_year_at_storage_level(session_factory):
    async with session_factory() as session:
        await session.execute(
            insert(TaxRecordModel).values(id=uuid4(), user_id=None, year=2026, total_profit=1.0,
                                          taxable_amount=0.0, estimated_tax=0.0)
        )
        await session.commit()
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(TaxRecordModel).values(id=uuid4(), user_id=None, year=2026, total_profit=2.0,
                                              taxable_amount=0.0, estimated_tax=0.0)
            )
        await session.rollback()
    assert await _count(session_factory, TaxRecordModel) == 1


@pytest.mark.parametrize(("allowance", "rate"), [(-1.0, 20.0), (0.0, 101.0), (0.0, -5.0)])
async def test_tax_rejects_invalid_parameters(manager, db_session, allowance, rate):
    with pytest.raises(CfoDomainError):
        await manager.calculate_tax(db_session, None, 2026, 1_000.0, allowance, rate)


# ---------------------------------------------------------------- alerts


async def test_unread_alerts_empty_without_seeding(manager, db_session):
    assert await manager.get_unread_alerts(db_session, user_id=None) == []


async def test_unread_alerts_filters_orders_and_scopes(manager, db_session, insert_alert):
    user_id = uuid4()
    base = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    oldest = await insert_alert(user_id=user_id, created_at=base)
    newest = await insert_alert(user_id=user_id, level="CRITICAL", created_at=base + timedelta(minutes=2))
    middle = await insert_alert(user_id=user_id, level="INFO", created_at=base + timedelta(minutes=1))
    await insert_alert(user_id=user_id, is_read=True, created_at=base + timedelta(minutes=3))
    await insert_alert(user_id=uuid4(), created_at=base + timedelta(minutes=4))
    await insert_alert(user_id=None, created_at=base + timedelta(minutes=5))

    alerts = await manager.get_unread_alerts(db_session, user_id=user_id)
    assert [a.id for a in alerts] == [newest, middle, oldest]
    assert all(not AlertRead.model_validate(a).is_read for a in alerts)


async def test_mark_alert_read_persists_flag(manager, session_factory, insert_alert):
    alert_id = await insert_alert()
    async with session_factory() as session:
        alert = await manager.mark_alert_read(session, alert_id=alert_id, user_id=None)
        assert alert.is_read is True

    async with session_factory() as session:
        stored = (await session.execute(select(CfoAlertModel.is_read).where(CfoAlertModel.id == alert_id))).scalar_one()
        assert stored is True
        assert await manager.get_unread_alerts(session, user_id=None) == []


async def test_mark_alert_read_unknown_id_raises(manager, db_session):
    missing = uuid4()
    with pytest.raises(AlertNotFoundError) as exc_info:
        await manager.mark_alert_read(db_session, alert_id=missing, user_id=None)
    assert exc_info.value.alert_id == missing


async def test_mark_alert_read_other_users_alert_raises(manager, db_session, insert_alert):
    alert_id = await insert_alert(user_id=uuid4())
    with pytest.raises(AlertNotFoundError):
        await manager.mark_alert_read(db_session, alert_id=alert_id, user_id=uuid4())
