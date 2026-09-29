import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.the_lab import ExperimentModel, ExperimentStatus

_CREATE_FIELDS: Final[frozenset[str]] = frozenset(
    {"name", "hypothesis", "model_a_name", "model_b_name"}
)


class ExperimentNotFoundError(LookupError):
    """Experiment does not exist (maps to 404)."""


class ExperimentStateConflictError(ValueError):
    """Experiment is not in a state that allows the transition (maps to 409)."""


class InvalidExperimentWinnerError(ValueError):
    """Winner is not one of the experiment's competing models (maps to 422)."""


class ExperimentManager:
    async def list_experiments(
        self, db: AsyncSession, skip: int = 0, limit: int = 50
    ) -> Sequence[ExperimentModel]:
        if skip < 0 or limit < 1:
            raise ValueError("skip must be >= 0 and limit must be >= 1")
        stmt = (
            select(ExperimentModel)
            .order_by(ExperimentModel.created_at.desc(), ExperimentModel.id.desc())
            .offset(skip)
            .limit(limit)
        )
        return (await db.scalars(stmt)).all()

    async def get_experiment(self, db: AsyncSession, experiment_id: uuid.UUID) -> ExperimentModel | None:
        return await db.get(ExperimentModel, experiment_id)

    async def create_experiment(self, db: AsyncSession, data: Mapping[str, Any]) -> ExperimentModel:
        keys = set(data)
        if unknown := keys - _CREATE_FIELDS:
            raise ValueError(f"Unexpected experiment fields: {sorted(unknown)}")
        if missing := _CREATE_FIELDS - keys:
            raise ValueError(f"Missing experiment fields: {sorted(missing)}")

        experiment = ExperimentModel(
            name=data["name"],
            hypothesis=data["hypothesis"],
            model_a_name=data["model_a_name"],
            model_b_name=data["model_b_name"],
            status=ExperimentStatus.RUNNING.value,
        )
        db.add(experiment)
        await db.commit()
        await db.refresh(experiment)
        return experiment

    async def conclude_experiment(
        self,
        db: AsyncSession,
        experiment_id: uuid.UUID,
        winner: str,
        metrics: Mapping[str, Any],
    ) -> ExperimentModel:
        experiment = await db.get(ExperimentModel, experiment_id, populate_existing=True)
        if experiment is None:
            raise ExperimentNotFoundError(f"Experiment {experiment_id} not found")
        if experiment.status != ExperimentStatus.RUNNING:
            raise ExperimentStateConflictError(
                f"Experiment {experiment_id} is {experiment.status}; only RUNNING experiments can be concluded"
            )
        if winner not in (experiment.model_a_name, experiment.model_b_name):
            raise InvalidExperimentWinnerError(
                f"winner must be '{experiment.model_a_name}' or '{experiment.model_b_name}'"
            )

        stmt = (
            update(ExperimentModel)
            .where(
                ExperimentModel.id == experiment_id,
                ExperimentModel.status == ExperimentStatus.RUNNING.value,
            )
            .values(
                status=ExperimentStatus.CONCLUDED.value,
                winner=winner,
                metrics=dict(metrics),
                concluded_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        result = cast(CursorResult[Any], await db.execute(stmt))
        if result.rowcount != 1:
            await db.rollback()
            raise ExperimentStateConflictError(
                f"Experiment {experiment_id} was concluded concurrently"
            )
        await db.commit()
        await db.refresh(experiment)
        return experiment
