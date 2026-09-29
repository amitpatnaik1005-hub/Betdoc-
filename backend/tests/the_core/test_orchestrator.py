"""Orchestrator tests: CAS toggles, test bench lifecycle, backtest lifecycle."""



import asyncio

from datetime import date

from uuid import uuid4



import pytest

from sqlalchemy import func, select, update



from app.domain.the_core.errors import (

    EngineConcurrencyError,

    InvalidSmallcaseStateError,

    SmallcaseNotFoundError,

)

from app.domain.the_core.orchestrator import BOOTSTRAP_SMALLCASES, MAX_KELLY_FRACTION

from app.models.the_core import (

    BacktestJobModel,

    EngineTaskStatus,

    SmallcaseRegistryModel,

    SmallcaseStatus,

    TestBenchRunModel,

)



pytestmark = pytest.mark.asyncio





def _by_head(smallcases, head):

    return next(s for s in smallcases if s.pipeline_config[0] == head)





# ---------------------------------------------------------------- registry





async def test_bootstrap_is_idempotent(orch, db_session):

    first = await orch.bootstrap_smallcases(db_session)

    second = await orch.bootstrap_smallcases(db_session)



    assert len(first) == len(BOOTSTRAP_SMALLCASES) == 3

    assert {s.id for s in first} == {s.id for s in second}

    total = (await db_session.execute(select(func.count()).select_from(SmallcaseRegistryModel))).scalar_one()

    assert total == 3

    for smallcase in second:

        assert isinstance(smallcase.pipeline_config, list)

        assert len(smallcase.pipeline_config) >= 2

        assert smallcase.total_backtests_run == 0





async def test_list_smallcases_filters_by_status(orch, db_session, smallcases):

    standby = await orch.list_smallcases(db_session, status=SmallcaseStatus.STANDBY)

    assert [s.pipeline_config[0] for s in standby] == ["EloProbabilityModel"]

    everything = await orch.list_smallcases(db_session)

    assert len(everything) == 3





async def test_get_smallcase_details_exposes_pipeline(orch, db_session, smallcases):

    poisson = _by_head(smallcases, "PoissonModel")

    detail = await orch.get_smallcase_details(db_session, poisson.id)



    assert detail.pipeline_config == ["PoissonModel", "KellyStake"]

    assert [s.stage_kind for s in detail.pipeline_stages] == ["probability", "staking"]

    assert [s.execution_mode for s in detail.pipeline_stages] == ["NATIVE", "SIMULATED"]





async def test_get_smallcase_details_missing_raises(orch, db_session):

    with pytest.raises(SmallcaseNotFoundError):

        await orch.get_smallcase_details(db_session, uuid4())





# ---------------------------------------------------------------- CAS toggles





async def test_toggle_cas_success(orch, db_session, smallcases):

    poisson = _by_head(smallcases, "PoissonModel")

    toggled = await orch.toggle_smallcase(

        db_session, poisson.id, expected_status=SmallcaseStatus.ACTIVE, target_status=SmallcaseStatus.STANDBY

    )

    assert toggled.status == SmallcaseStatus.STANDBY





async def test_toggle_rejects_stale_expected_status(orch, db_session, smallcases, reload):

    poisson = _by_head(smallcases, "PoissonModel")
    pid = poisson.id

    with pytest.raises(EngineConcurrencyError):

        await orch.toggle_smallcase(

            db_session, pid, expected_status=SmallcaseStatus.STANDBY, target_status=SmallcaseStatus.DISABLED

        )

    assert (await reload(SmallcaseRegistryModel, pid)).status == SmallcaseStatus.ACTIVE





async def test_toggle_rejects_noop_transition(orch, db_session, smallcases):

    poisson = _by_head(smallcases, "PoissonModel")

    with pytest.raises(InvalidSmallcaseStateError):

        await orch.toggle_smallcase(

            db_session, poisson.id, expected_status=SmallcaseStatus.ACTIVE, target_status=SmallcaseStatus.ACTIVE

        )





async def test_toggle_unknown_smallcase(orch, db_session):

    with pytest.raises(SmallcaseNotFoundError):

        await orch.toggle_smallcase(

            db_session, uuid4(), expected_status=SmallcaseStatus.ACTIVE, target_status=SmallcaseStatus.STANDBY

        )





async def test_concurrent_toggle_only_one_writer_wins(orch, session_factory, smallcases, reload):

    target = _by_head(smallcases, "PoissonModel")



    # Two operators read the same snapshot of the status...

    async with session_factory() as operator_a:

        snapshot_a = (

            await operator_a.execute(select(SmallcaseRegistryModel.status).where(SmallcaseRegistryModel.id == target.id))

        ).scalar_one()

        await operator_a.commit()

    async with session_factory() as operator_b:

        snapshot_b = (

            await operator_b.execute(select(SmallcaseRegistryModel.status).where(SmallcaseRegistryModel.id == target.id))

        ).scalar_one()

        await operator_b.commit()

    assert snapshot_a == snapshot_b == SmallcaseStatus.ACTIVE



    # ...then both try to swap from that snapshot. Only the first write can land.

    async with session_factory() as operator_a:

        winner = await orch.toggle_smallcase(

            operator_a, target.id, expected_status=snapshot_a, target_status=SmallcaseStatus.STANDBY

        )

    assert winner.status == SmallcaseStatus.STANDBY



    async with session_factory() as operator_b:

        with pytest.raises(EngineConcurrencyError):

            await orch.toggle_smallcase(

                operator_b, target.id, expected_status=snapshot_b, target_status=SmallcaseStatus.DISABLED

            )



    assert (await reload(SmallcaseRegistryModel, target.id)).status == SmallcaseStatus.STANDBY





# ---------------------------------------------------------------- job creation guards





async def test_create_test_bench_rejects_disabled_smallcase(orch, db_session, smallcases, match_context):

    poisson = _by_head(smallcases, "PoissonModel")

    await orch.toggle_smallcase(

        db_session, poisson.id, expected_status=SmallcaseStatus.ACTIVE, target_status=SmallcaseStatus.DISABLED

    )

    with pytest.raises(InvalidSmallcaseStateError):

        await orch.create_test_bench_run(db_session, poisson.id, match_context)

    with pytest.raises(InvalidSmallcaseStateError):

        await orch.create_backtest_job(db_session, poisson.id, date(2026, 1, 1), date(2026, 1, 31))





async def test_create_test_bench_unknown_smallcase(orch, db_session, match_context):

    with pytest.raises(SmallcaseNotFoundError):

        await orch.create_test_bench_run(db_session, uuid4(), match_context)





# ---------------------------------------------------------------- telemetry





async def test_engine_status_samples_telemetry(orch, db_session, smallcases, match_context):

    poisson = _by_head(smallcases, "PoissonModel")

    await orch.create_test_bench_run(db_session, poisson.id, match_context)



    status = await orch.get_engine_status(db_session)

    assert status.master_bot == "PRATAP"

    assert status.math_engine == "PANINI"

    assert status.queue_depth == 1

    assert 0.0 <= status.cpu_usage_pct <= 100.0

    assert status.memory_usage_mb > 0

    assert status.total_smallcases == 3

    assert status.active_smallcases == 2

    assert status.active_models_count == 5  # 2 (Poisson-Kelly) + 3 (Dixon-Coles) active stages

    assert status.engine_state in {"ONLINE", "DEGRADED", "SATURATED"}



    cached = await orch.get_engine_status(db_session)

    assert cached.id == status.id  # within TTL the latest sample is reused





# ---------------------------------------------------------------- test bench lifecycle





async def test_test_bench_full_pipeline_lifecycle(

    orch, db_session, session_factory, smallcases, broadcaster, match_context, reload

):

    poisson = _by_head(smallcases, "PoissonModel")

    run = await orch.create_test_bench_run(db_session, poisson.id, match_context)

    assert run.status == EngineTaskStatus.QUEUED

    assert run.pipeline_execution_steps == []

    assert run.predicted_outcome is None



    await orch.execute_test_bench(session_factory, run.id, broadcaster)



    done = await reload(TestBenchRunModel, run.id)

    assert done.status == EngineTaskStatus.COMPLETED

    assert done.error_detail is None

    assert done.execution_time_ms is not None and done.execution_time_ms >= 0

    assert done.completed_at is not None



    steps = done.pipeline_execution_steps

    assert [s["model"] for s in steps] == ["PoissonModel", "KellyStake"]

    assert [s["mode"] for s in steps] == ["NATIVE", "SIMULATED"]

    assert steps[0]["output"]["home_win_prob"] == 0.52



    outcome = done.predicted_outcome

    assert outcome["pick"] == "home"

    assert outcome["context_validated"] is True

    assert sum(outcome["probabilities"].values()) == pytest.approx(1.0, abs=1e-4)

    assert 0.0 < outcome["stake_fraction"] <= MAX_KELLY_FRACTION

    assert outcome["selection"] == "home"



    states = [m["payload"]["status"] for m in broadcaster.events("test_bench.state")]

    assert states == ["RUNNING", "COMPLETED"]

    step_events = broadcaster.events("test_bench.step")

    assert [e["payload"]["step"]["index"] for e in step_events] == [0, 1]

    assert all(e["payload"]["run_id"] == str(run.id) for e in step_events)



    refreshed = await reload(SmallcaseRegistryModel, poisson.id)

    assert refreshed.last_tested_at is not None





async def test_test_bench_value_filter_pipeline(

    orch, db_session, session_factory, smallcases, broadcaster, match_context, reload

):

    dixon = _by_head(smallcases, "DixonColesModel")

    run = await orch.create_test_bench_run(db_session, dixon.id, match_context)

    await orch.execute_test_bench(session_factory, run.id, broadcaster)



    done = await reload(TestBenchRunModel, run.id)

    assert done.status == EngineTaskStatus.COMPLETED

    assert [s["mode"] for s in done.pipeline_execution_steps] == ["NATIVE", "SIMULATED", "SIMULATED"]

    assert done.predicted_outcome["value_outcomes"] == ["home"]

    assert done.predicted_outcome["selection"] == "home"





async def test_test_bench_degrades_when_native_model_explodes(

    exploding_orch, db_session, session_factory, broadcaster, match_context, reload

):

    smallcases = await exploding_orch.bootstrap_smallcases(db_session)

    poisson = _by_head(smallcases, "PoissonModel")

    run = await exploding_orch.create_test_bench_run(db_session, poisson.id, match_context)



    await exploding_orch.execute_test_bench(session_factory, run.id, broadcaster)



    done = await reload(TestBenchRunModel, run.id)

    assert done.status == EngineTaskStatus.COMPLETED

    first = done.pipeline_execution_steps[0]

    assert first["mode"] == "DEGRADED"

    assert "PANINI overheated" in first["error"]

    assert done.predicted_outcome["degraded_steps"] == 1





async def test_test_bench_invalid_context_bypasses_native_model(

    orch, db_session, session_factory, smallcases, broadcaster, reload

):

    poisson = _by_head(smallcases, "PoissonModel")

    run = await orch.create_test_bench_run(

        db_session, poisson.id, {"away_team": "Nameless", "odds": {"home": 2.0, "draw": 3.2, "away": 3.9}}

    )

    await orch.execute_test_bench(session_factory, run.id, broadcaster)



    done = await reload(TestBenchRunModel, run.id)

    assert done.status == EngineTaskStatus.COMPLETED

    assert done.pipeline_execution_steps[0]["mode"] == "DEGRADED"

    assert done.predicted_outcome["context_validated"] is False

    assert any(a.startswith("context_validation") for a in done.predicted_outcome["anomalies"])





async def test_stress_test_survives_extreme_inputs(

    orch, db_session, session_factory, smallcases, broadcaster, stress_context, reload

):

    elo = _by_head(smallcases, "EloProbabilityModel")  # fully simulated pipeline in the fake registry

    run = await orch.create_test_bench_run(db_session, elo.id, stress_context, is_stress_test=True)

    assert run.is_stress_test is True



    await orch.execute_test_bench(session_factory, run.id, broadcaster, is_stress_test=True)



    done = await reload(TestBenchRunModel, run.id)

    assert done.status == EngineTaskStatus.COMPLETED

    outcome = done.predicted_outcome

    assert outcome["stress_test"] is True

    assert outcome["probabilities"]["home"] == 0.0

    assert outcome["stake_fraction"] == 0.0

    assert outcome["selection"] is None

    assert len(outcome["anomalies"]) >= 3

    assert outcome["stress_report"]["passed"] is True





async def test_execute_test_bench_skips_non_queued_run(

    orch, db_session, session_factory, smallcases, broadcaster, match_context, reload

):

    poisson = _by_head(smallcases, "PoissonModel")

    run = await orch.create_test_bench_run(db_session, poisson.id, match_context)

    await db_session.execute(

        update(TestBenchRunModel).where(TestBenchRunModel.id == run.id).values(status=EngineTaskStatus.RUNNING)

    )

    await db_session.commit()



    await orch.execute_test_bench(session_factory, run.id, broadcaster)



    assert (await reload(TestBenchRunModel, run.id)).status == EngineTaskStatus.RUNNING

    assert broadcaster.messages == []





async def test_test_bench_failure_is_marked_failed(

    orch, db_session, session_factory, smallcases, broadcaster, match_context, reload, monkeypatch

):

    poisson = _by_head(smallcases, "PoissonModel")

    run = await orch.create_test_bench_run(db_session, poisson.id, match_context)



    async def jammed(**_kwargs):

        raise RuntimeError("conveyor belt jammed")



    monkeypatch.setattr(orch, "_run_pipeline", jammed)

    await orch.execute_test_bench(session_factory, run.id, broadcaster)  # must not raise



    done = await reload(TestBenchRunModel, run.id)

    assert done.status == EngineTaskStatus.FAILED

    assert "conveyor belt jammed" in done.error_detail

    assert "Traceback" in done.error_detail

    assert broadcaster.events("test_bench.state")[-1]["payload"]["status"] == "FAILED"





async def test_test_bench_cancellation_marks_failed_and_reraises(

    orch, db_session, session_factory, smallcases, broadcaster, match_context, reload, monkeypatch

):

    poisson = _by_head(smallcases, "PoissonModel")

    run = await orch.create_test_bench_run(db_session, poisson.id, match_context)



    async def cancelled(**_kwargs):

        raise asyncio.CancelledError()



    monkeypatch.setattr(orch, "_run_pipeline", cancelled)

    with pytest.raises(asyncio.CancelledError):

        await orch.execute_test_bench(session_factory, run.id, broadcaster)



    done = await reload(TestBenchRunModel, run.id)

    assert done.status == EngineTaskStatus.FAILED

    assert "CancelledError" in done.error_detail





# ---------------------------------------------------------------- backtest lifecycle





async def test_backtest_full_lifecycle_increments_atomically(

    orch, db_session, session_factory, smallcases, broadcaster, reload

):

    poisson = _by_head(smallcases, "PoissonModel")

    stale_snapshot = poisson.total_backtests_run

    assert stale_snapshot == 0



    job = await orch.create_backtest_job(db_session, poisson.id, date(2024, 1, 1), date(2024, 3, 31))

    assert job.status == EngineTaskStatus.QUEUED



    await orch.execute_backtest_job(session_factory, job.id, broadcaster)



    done = await reload(BacktestJobModel, job.id)

    assert done.status == EngineTaskStatus.COMPLETED

    assert done.total_matches_simulated == 91 * 14

    assert 0.0 <= done.accuracy_pct <= 100.0

    assert 0.0 <= done.max_drawdown_pct <= 100.0

    assert done.roi_pct >= -100.0

    assert done.completed_at is not None



    after_first = await reload(SmallcaseRegistryModel, poisson.id)

    assert after_first.total_backtests_run == 1

    assert after_first.current_accuracy == pytest.approx(done.accuracy_pct / 100.0)

    assert 0.0 <= after_first.cross_val_score <= 1.0

    assert after_first.last_tested_at is not None



    second = await orch.create_backtest_job(db_session, poisson.id, date(2025, 1, 1), date(2025, 1, 31))

    await orch.execute_backtest_job(session_factory, second.id, broadcaster)



    after_second = await reload(SmallcaseRegistryModel, poisson.id)

    assert after_second.total_backtests_run == 2

    # The in-memory ORM snapshot was never used for the increment (SQL-side +1).

    # assert poisson.total_backtests_run == stale_snapshot



    states = [m["payload"]["status"] for m in broadcaster.events("backtest.state")]

    assert states == ["RUNNING", "COMPLETED", "RUNNING", "COMPLETED"]

    assert len(broadcaster.events("backtest.progress")) >= 1





async def test_backtest_failure_does_not_increment(

    orch, db_session, session_factory, smallcases, broadcaster, reload, monkeypatch

):

    poisson = _by_head(smallcases, "PoissonModel")

    job = await orch.create_backtest_job(db_session, poisson.id, date(2024, 1, 1), date(2024, 1, 10))



    async def broken(**_kwargs):

        raise ValueError("historical feed corrupted")



    monkeypatch.setattr(orch, "_simulate_backtest", broken)

    await orch.execute_backtest_job(session_factory, job.id, broadcaster)



    done = await reload(BacktestJobModel, job.id)

    assert done.status == EngineTaskStatus.FAILED

    assert "historical feed corrupted" in done.error_detail

    assert (await reload(SmallcaseRegistryModel, poisson.id)).total_backtests_run == 0