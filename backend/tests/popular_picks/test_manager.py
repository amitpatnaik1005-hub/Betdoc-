"""PopularPicksManager tests: seeding, active filtering, FA-1 review gate."""

import math
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError

from app.domain.popular_picks.errors import PopularPickNotFoundError, PopularPicksDomainError
from app.domain.popular_picks.manager import MOCK_PARLAY_BLUEPRINTS
from app.models.popular_picks import ParlayReviewGateModel, PopularParlayModel

pytestmark = pytest.mark.asyncio


async def _count(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def test_generate_mock_picks_seeds_one_of_each_type(manager, db_session):
    seeded = await manager.generate_mock_picks(db_session)
    snapshot = [(p.pick_type, p.total_odds, [leg["odds"] for leg in p.legs]) for p in seeded]

    assert len(snapshot) == len(MOCK_PARLAY_BLUEPRINTS) == 3
    assert sorted(pick_type for pick_type, _, _ in snapshot) == ["AI_PREDICTED", "SHARP_MONEY", "TRENDING"]
    for _, total_odds, leg_odds in snapshot:
        assert total_odds == pytest.approx(math.prod(leg_odds), abs=1e-4)
        assert total_odds >= 1.0


async def test_generate_mock_picks_is_idempotent(manager, db_session):
    first = await manager.generate_mock_picks(db_session)
    assert len(first) == 3
    second = await manager.generate_mock_picks(db_session)
    assert second == []
    assert await _count(db_session, PopularParlayModel) == 3


async def test_generate_mock_picks_reseeds_when_only_expired_picks_exist(manager, db_session, make_parlay):
    await make_parlay(title="Stale", expires_in=timedelta(hours=-1))
    seeded = await manager.generate_mock_picks(db_session)
    assert len(seeded) == 3
    assert await _count(db_session, PopularParlayModel) == 4


async def test_get_active_picks_excludes_inactive_and_expired(manager, db_session, make_parlay):
    live_id = await make_parlay(title="Live")
    await make_parlay(title="Expired", expires_in=timedelta(minutes=-5))
    await make_parlay(title="Inactive", is_active=False)

    picks = await manager.get_active_picks(db_session)
    assert [p.id for p in picks] == [live_id]


async def test_get_active_picks_orders_by_success_rate(manager, db_session, make_parlay):
    low_id = await make_parlay(title="Low", historical_success_rate=0.2)
    high_id = await make_parlay(title="High", historical_success_rate=0.6)

    picks = await manager.get_active_picks(db_session)
    assert [p.id for p in picks] == [high_id, low_id]


async def test_get_active_picks_caps_at_fifty(manager, db_session, make_parlay):
    for index in range(55):
        await make_parlay(title=f"Parlay {index}")
    picks = await manager.get_active_picks(db_session)
    assert len(picks) == 50


@pytest.mark.parametrize("decision", ["ACCEPTED", "REJECTED", "MODIFIED"])
async def test_record_review_decision_persists_gate(manager, db_session, make_parlay, decision):
    parlay_id = await make_parlay()
    user_id = uuid4()

    gate = await manager.record_review_decision(db_session, parlay_id=parlay_id, user_id=user_id, decision=decision)
    gate_id, stored_decision, stored_parlay, stored_user, created_at = (
        gate.id,
        gate.decision,
        gate.parlay_id,
        gate.user_id,
        gate.created_at,
    )

    assert stored_decision == decision
    assert stored_parlay == parlay_id
    assert stored_user == user_id
    assert created_at is not None
    persisted = (
        await db_session.execute(select(ParlayReviewGateModel.id).where(ParlayReviewGateModel.id == gate_id))
    ).scalar_one()
    assert persisted == gate_id


async def test_record_review_decision_allows_anonymous_users(manager, db_session, make_parlay):
    parlay_id = await make_parlay()
    gate = await manager.record_review_decision(db_session, parlay_id=parlay_id, user_id=None, decision="REJECTED")
    assert gate.user_id is None
    assert gate.decision == "REJECTED"


async def test_record_review_decision_unknown_parlay_raises(manager, db_session):
    missing_id = uuid4()
    with pytest.raises(PopularPickNotFoundError) as exc_info:
        await manager.record_review_decision(db_session, parlay_id=missing_id, user_id=None, decision="ACCEPTED")
    assert exc_info.value.parlay_id == missing_id
    assert await _count(db_session, ParlayReviewGateModel) == 0


async def test_record_review_decision_rejects_invalid_decision(manager, db_session, make_parlay):
    parlay_id = await make_parlay()
    with pytest.raises(PopularPicksDomainError) as exc_info:
        await manager.record_review_decision(db_session, parlay_id=parlay_id, user_id=None, decision="MAYBE")
    assert not isinstance(exc_info.value, PopularPickNotFoundError)
    assert await _count(db_session, ParlayReviewGateModel) == 0


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        ({"total_odds": 0.5}, "total_odds below 1.0"),
        ({"historical_success_rate": 1.5}, "success rate above 1.0"),
        ({"historical_success_rate": -0.1}, "success rate below 0.0"),
        ({"pick_type": "HUNCH"}, "unknown pick_type"),
    ],
)
async def test_parlay_check_constraints(session_factory, overrides, label):
    from datetime import UTC, datetime

    values = {
        "id": uuid4(),
        "title": f"Invalid: {label}",
        "pick_type": "TRENDING",
        "legs": [{"match_id": "X-1", "selection": "HOME", "odds": 2.0}],
        "total_odds": 2.0,
        "historical_success_rate": 0.5,
        "is_active": True,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        **overrides,
    }
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(insert(PopularParlayModel).values(**values))
        await session.rollback()
        assert await _count(session, PopularParlayModel) == 0


async def test_review_gate_decision_check_constraint(session_factory, make_parlay):
    parlay_id = await make_parlay()  # UUID captured before any rollback
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                insert(ParlayReviewGateModel).values(id=uuid4(), parlay_id=parlay_id, decision="MAYBE")
            )
        await session.rollback()
        remaining = (
            await session.execute(
                select(func.count())
                .select_from(ParlayReviewGateModel)
                .where(ParlayReviewGateModel.parlay_id == parlay_id)
            )
        ).scalar_one()
        assert remaining == 0


async def test_deleting_parlay_cascades_review_gates(manager, session_factory, make_parlay):
    parlay_id = await make_parlay()
    async with session_factory() as session:
        await manager.record_review_decision(session, parlay_id=parlay_id, user_id=None, decision="ACCEPTED")
    async with session_factory() as session:
        await session.execute(delete(PopularParlayModel).where(PopularParlayModel.id == parlay_id))
        await session.commit()
        assert await _count(session, ParlayReviewGateModel) == 0
