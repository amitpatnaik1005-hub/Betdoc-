import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain.the_lab import (
    MONITORED_SOURCES,
    ApiHealthMonitor,
    ExperimentManager,
    ExperimentNotFoundError,
    ExperimentStateConflictError,
    ExperimentStatus,
    InvalidExperimentWinnerError,
)
from app.models.the_lab import ExperimentModel
from app.schemas.the_lab import ExperimentCreate, ResearchCreate

pytestmark = pytest.mark.asyncio

VALID = {
    "name": "Goal model shootout",
    "hypothesis": "Dixon-Coles beats Poisson on low-scoring leagues",
    "model_a_name": "Poisson",
    "model_b_name": "Dixon-Coles",
}


async def test_create_sets_running(db) -> None:
    experiment = await ExperimentManager().create_experiment(db, VALID)
    assert experiment.status == ExperimentStatus.RUNNING
    assert experiment.winner is None and experiment.metrics is None


async def test_create_rejects_unknown_or_missing_fields(db) -> None:
    with pytest.raises(ValueError, match="Unexpected"):
        await ExperimentManager().create_experiment(db, {**VALID, "status": "CONCLUDED"})
    with pytest.raises(ValueError, match="Missing"):
        await ExperimentManager().create_experiment(db, {"name": "x"})


async def test_conclude_persists_json_metrics(db, session_factory) -> None:
    manager = ExperimentManager()
    experiment = await manager.create_experiment(db, VALID)
    metrics = {"log_loss": {"Poisson": 0.981, "Dixon-Coles": 0.962}, "n_matches": 380}
    concluded = await manager.conclude_experiment(db, experiment.id, "Dixon-Coles", metrics)
    assert concluded.status == ExperimentStatus.CONCLUDED
    assert concluded.concluded_at is not None

    async with session_factory() as fresh:
        reloaded = await fresh.get(ExperimentModel, experiment.id)
        assert reloaded is not None and reloaded.metrics == metrics


async def test_conclude_twice_is_state_conflict(db) -> None:
    manager = ExperimentManager()
    experiment = await manager.create_experiment(db, VALID)
    await manager.conclude_experiment(db, experiment.id, "Poisson", {})
    with pytest.raises(ExperimentStateConflictError):
        await manager.conclude_experiment(db, experiment.id, "Poisson", {})


async def test_conclude_rejects_foreign_winner(db) -> None:
    manager = ExperimentManager()
    experiment = await manager.create_experiment(db, VALID)
    with pytest.raises(InvalidExperimentWinnerError):
        await manager.conclude_experiment(db, experiment.id, "Elo", {})


async def test_conclude_unknown_experiment(db) -> None:
    with pytest.raises(ExperimentNotFoundError):
        await ExperimentManager().conclude_experiment(db, uuid.uuid4(), "Poisson", {})


async def test_list_experiments_desc_with_pagination(db, session_factory) -> None:
    base = datetime.now(UTC)
    async with session_factory.begin() as session:
        for i in range(4):
            session.add(ExperimentModel(**{**VALID, "name": f"e{i}"}, status="RUNNING",
                                        created_at=base + timedelta(minutes=i)))
    manager = ExperimentManager()
    assert [e.name for e in await manager.list_experiments(db, skip=0, limit=3)] == ["e3", "e2", "e1"]
    assert [e.name for e in await manager.list_experiments(db, skip=3, limit=3)] == ["e0"]
    with pytest.raises(ValueError):
        await manager.list_experiments(db, skip=-1)


async def test_health_monitor_pings_concurrently() -> None:
    started = time.perf_counter()
    statuses = await ApiHealthMonitor().check_all()
    elapsed = time.perf_counter() - started
    assert [s.source_name for s in statuses] == list(MONITORED_SOURCES)
    assert all(s.status == "ONLINE" and 0 <= s.latency_ms < 1000 for s in statuses)
    assert all(s.last_checked.tzinfo is not None for s in statuses)
    assert elapsed < 0.45  # gathered, not sequential


def test_schema_guards() -> None:
    with pytest.raises(ValidationError):
        ExperimentCreate(**{**VALID, "model_b_name": "poisson"})
    with pytest.raises(ValidationError):
        ResearchCreate(category="X" * 51, topic="t")
    with pytest.raises(ValidationError):
        ResearchCreate(category="PRE_MATCH", topic="t", status="COMPLETED")  # extra="forbid"
