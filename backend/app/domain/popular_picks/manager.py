"""PopularPicksManager: ASHOKA's trending parlays and FA-1 review gate."""

import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.popular_picks.errors import PopularPickNotFoundError, PopularPicksDomainError
from app.models.popular_picks import ParlayReviewGateModel, PickType, PopularParlayModel, ReviewDecision
from app.schemas.popular_picks import ParlayLegSchema

logger = logging.getLogger("betdoc.ashoka")

MAX_ACTIVE_PICKS = 50

# Demo data for seeding a development database by hand (``generate_mock_picks``). The API never serves it:
# since Group 69 the list comes from Ashoka's live trend scan.
MOCK_PARLAY_BLUEPRINTS: tuple[dict[str, Any], ...] = (
    {
        "title": "ASHOKA Trending: Weekend Favourites Treble",
        "pick_type": PickType.TRENDING,
        "historical_success_rate": 0.34,
        "ttl_hours": 12,
        "legs": [
            {"match_id": "EPL-ARS-CHE", "selection": "HOME", "odds": 1.85},
            {"match_id": "LALIGA-RMA-SEV", "selection": "HOME", "odds": 1.45},
            {"match_id": "SERIEA-INT-NAP", "selection": "OVER_2_5", "odds": 1.90},
        ],
    },
    {
        "title": "ASHOKA People's Choice: Goals Galore Double",
        "pick_type": PickType.AI_PREDICTED,
        "historical_success_rate": 0.41,
        "ttl_hours": 24,
        "legs": [
            {"match_id": "BUNDES-BAY-DOR", "selection": "BTTS_YES", "odds": 1.60},
            {"match_id": "LIGUE1-PSG-MAR", "selection": "OVER_2_5", "odds": 1.70},
        ],
    },
    {
        "title": "ASHOKA Sharp Money: Underdog Value Double",
        "pick_type": PickType.SHARP_MONEY,
        "historical_success_rate": 0.27,
        "ttl_hours": 6,
        "legs": [
            {"match_id": "EPL-EVE-TOT", "selection": "AWAY", "odds": 2.40},
            {"match_id": "ERED-AJA-PSV", "selection": "DRAW", "odds": 3.60},
        ],
    },
)


def compute_total_odds(legs: Sequence[ParlayLegSchema]) -> float:
    """Parlay odds are the product of every leg's decimal odds."""
    if not legs:
        raise PopularPicksDomainError("A parlay needs at least one leg.")
    return round(math.prod(leg.odds for leg in legs), 4)


class PopularPicksManager:
    def __init__(self, *, max_active_picks: int = MAX_ACTIVE_PICKS) -> None:
        self._max_active_picks = max_active_picks

    async def scan_trending(self, db: AsyncSession, redis: Any, settings: Any, now: datetime | None = None) -> list[PopularParlayModel]:
        """Group 69: sharp steam parlays, public traps and AI hybrids from the live market (``trends.scan``)."""
        from app.domain.popular_picks import trends  # noqa: PLC0415 - pulls in the oracle engine

        return await trends.scan(db, redis, settings, now or datetime.now(UTC))

    async def submit_external(self, db: AsyncSession, redis: Any, settings: Any, body: Any, now: datetime | None = None) -> PopularParlayModel:
        from app.domain.popular_picks import trends  # noqa: PLC0415

        try:
            return await trends.evaluate_external(db, redis, settings, now or datetime.now(UTC), body)
        except ValueError as exc:
            raise PopularPicksDomainError(str(exc)) from exc

    @staticmethod
    def _active_filter() -> tuple[Any, ...]:
        return (PopularParlayModel.is_active.is_(True), PopularParlayModel.expires_at > func.now())

    async def get_active_picks(self, db: AsyncSession) -> list[PopularParlayModel]:
        statement = (
            select(PopularParlayModel)
            .where(*self._active_filter())
            .order_by(
                PopularParlayModel.historical_success_rate.desc(),
                PopularParlayModel.created_at.desc(),
                PopularParlayModel.id,
            )
            .limit(self._max_active_picks)
            .execution_options(populate_existing=True)
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def generate_mock_picks(self, db: AsyncSession) -> list[PopularParlayModel]:
        """Seed one mock parlay per pick_type when no active picks exist. Returns newly seeded rows."""
        active_count = (
            await db.execute(select(func.count()).select_from(PopularParlayModel).where(*self._active_filter()))
        ).scalar_one()
        if active_count:
            logger.debug("ASHOKA skipped mock seeding: %d active parlays already exist.", active_count)
            return []

        now = datetime.now(UTC)
        rows: list[dict[str, Any]] = []
        for blueprint in MOCK_PARLAY_BLUEPRINTS:
            legs = [ParlayLegSchema.model_validate(leg) for leg in blueprint["legs"]]
            rows.append(
                {
                    "id": uuid4(),
                    "title": blueprint["title"],
                    "pick_type": blueprint["pick_type"].value,
                    "legs": [leg.model_dump() for leg in legs],
                    "total_odds": compute_total_odds(legs),
                    "historical_success_rate": blueprint["historical_success_rate"],
                    "is_active": True,
                    "expires_at": now + timedelta(hours=blueprint["ttl_hours"]),
                }
            )

        try:
            await db.execute(insert(PopularParlayModel), rows)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.warning("ASHOKA could not seed mock parlays; the insert was rejected.", exc_info=True)
            return []

        seeded_ids = [row["id"] for row in rows]
        result = await db.execute(
            select(PopularParlayModel)
            .where(PopularParlayModel.id.in_(seeded_ids))
            .order_by(PopularParlayModel.historical_success_rate.desc())
            .execution_options(populate_existing=True)
        )
        seeded = list(result.scalars().all())
        logger.info("ASHOKA seeded %d mock popular parlays.", len(seeded))
        return seeded

    async def _parlay_exists(self, db: AsyncSession, parlay_id: UUID) -> bool:
        found = (
            await db.execute(select(PopularParlayModel.id).where(PopularParlayModel.id == parlay_id))
        ).scalar_one_or_none()
        return found is not None

    async def record_review_decision(
        self,
        db: AsyncSession,
        parlay_id: UUID,
        user_id: UUID | None,
        decision: str,
    ) -> ParlayReviewGateModel:
        try:
            normalized = ReviewDecision(decision)
        except ValueError as exc:
            raise PopularPicksDomainError(f"Invalid review decision {decision!r}.") from exc

        if not await self._parlay_exists(db, parlay_id):
            raise PopularPickNotFoundError(parlay_id)

        gate_id = uuid4()
        try:
            await db.execute(
                insert(ParlayReviewGateModel).values(
                    id=gate_id,
                    user_id=user_id,
                    parlay_id=parlay_id,
                    decision=normalized.value,
                )
            )
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            if not await self._parlay_exists(db, parlay_id):
                raise PopularPickNotFoundError(parlay_id) from exc
            raise PopularPicksDomainError("ASHOKA rejected the review record.") from exc

        gate = (
            await db.execute(
                select(ParlayReviewGateModel)
                .where(ParlayReviewGateModel.id == gate_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        logger.info(
            "ASHOKA recorded review gate %s: parlay=%s decision=%s user=%s",
            gate_id,
            parlay_id,
            normalized.value,
            user_id or "anonymous",
        )
        return gate
