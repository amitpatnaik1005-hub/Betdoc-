"""Group 93: TODAR MAL's historical match, score & CLV warehouse.

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.deps import get_current_admin, get_current_user
from app.domain.archive.clv_warehouse_math import (
    SettledBetInput,
    TierThresholds,
    classify_clv_edge_tier,
    compute_leg_clv_pct,
    compute_sharp_clv_pct,
    evaluate_clv_warehouse_aggregates,
)
from app.models import Base, User
from app.models.feedback import ModelPredictionFeedback
from app.models.user_bets_ledger import FixtureScore, UserPlacedBet, UserPlacedLeg

from .conftest import app as archive_app

BASE = "/api/v1/archive"
DEVELOPER = "Amit Ashok Kumar Patnaik"
TIERS = TierThresholds(sharp_alpha_min_pct=1.5, sharp_beat_rate_min=0.55, positive_min_pct=0.15, adverse_below_pct=-0.25, min_sample=30)
TABLES = [User.__table__, UserPlacedBet.__table__, UserPlacedLeg.__table__, FixtureScore.__table__, ModelPredictionFeedback.__table__]


def _slip(status: str, stake: str, ret: str, clv: float | None, *, book: str = "PARIMATCH", sport: str = "soccer_epl", sharp: float | None = None) -> SettledBetInput:
    return SettledBetInput(
        bet_id=str(uuid.uuid4()), bookmaker=book, sport_key=sport, status=status, stake_inr=Decimal(stake), return_inr=Decimal(ret),
        pnl_inr=Decimal(ret) - Decimal(stake), clv_pct=clv, clv_sharp_pct=sharp,
    )


# --------------------------------------------------------------------------- math
def test_clv_formulas_only_measure_real_prices() -> None:
    assert compute_leg_clv_pct(Decimal("2.10"), Decimal("2.00")) == 5.0
    assert compute_leg_clv_pct(Decimal("1.90"), Decimal("2.00")) == -5.0
    assert compute_leg_clv_pct(Decimal("2.10"), None) is None
    assert compute_leg_clv_pct(Decimal("1.00"), Decimal("2.00")) is None
    assert compute_sharp_clv_pct(Decimal("2.10"), 0.50) == 5.0
    assert compute_sharp_clv_pct(Decimal("2.10"), 1.0) is None and compute_sharp_clv_pct(None, 0.5) is None


def test_edge_tiers_follow_the_thresholds() -> None:
    assert classify_clv_edge_tier(avg_clv_pct=2.1, beat_close_rate_fraction=0.62, thresholds=TIERS) == "SHARP_ALPHA"
    assert classify_clv_edge_tier(avg_clv_pct=2.1, beat_close_rate_fraction=0.40, thresholds=TIERS) == "POSITIVE_CLV"  # a few big beats
    assert classify_clv_edge_tier(avg_clv_pct=0.0, beat_close_rate_fraction=0.50, thresholds=TIERS) == "NEUTRAL_LINE"
    assert classify_clv_edge_tier(avg_clv_pct=-1.0, beat_close_rate_fraction=0.30, thresholds=TIERS) == "ADVERSE_DRIFT"
    assert classify_clv_edge_tier(avg_clv_pct=None, beat_close_rate_fraction=None, thresholds=TIERS) == "UNMEASURED"


def test_aggregates_in_inr_and_usd_with_void_out_of_turnover() -> None:
    slips = [
        _slip("WON", "1000.00", "2100.00", 6.061, sharp=4.5),
        _slip("LOST", "500.00", "0.00", 2.632, book="1XBET", sport="cricket_ipl", sharp=1.8),
        _slip("VOID", "300.00", "300.00", None),
    ]
    agg = evaluate_clv_warehouse_aggregates(slips, inr_per_usd=Decimal("84.00"), thresholds=TIERS)
    assert (agg.settled_bets_count, agg.won_bets_count, agg.lost_bets_count, agg.void_bets_count) == (3, 1, 1, 1)
    assert agg.win_rate_pct == 50.0
    assert agg.total_staked_inr == Decimal("1500.00") and agg.net_pnl_inr == Decimal("600.00")  # the void stake came back
    assert agg.total_staked_usd == Decimal("17.86") and agg.net_pnl_usd == Decimal("7.14")
    assert agg.roi_pct == 40.0
    assert agg.clv_sample_count == 2 and agg.avg_clv_pct == pytest.approx(4.3465, abs=1e-3) and agg.beat_close_rate_pct == 100.0 and agg.avg_clv_sharp_pct == 3.15
    assert agg.edge_tier == "SHARP_ALPHA" and agg.edge_tier_provisional is True  # 2 of the 30 slips the tier needs

    no_rate = evaluate_clv_warehouse_aggregates(slips, inr_per_usd=None, thresholds=TIERS)
    assert no_rate.total_staked_usd is None and no_rate.net_pnl_usd is None
    unmeasured = evaluate_clv_warehouse_aggregates([_slip("WON", "100", "200", None)], inr_per_usd=None, thresholds=TIERS)
    assert unmeasured.edge_tier == "UNMEASURED" and unmeasured.edge_tier_provisional is False and unmeasured.avg_clv_pct is None
    empty = evaluate_clv_warehouse_aggregates([], inr_per_usd=None, thresholds=TIERS)
    assert empty.roi_pct is None and empty.win_rate_pct is None and empty.edge_tier == "UNMEASURED"


# --------------------------------------------------------------------------- service & API
@pytest_asyncio.fixture
async def ledger(engine: AsyncEngine) -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=TABLES)
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all, tables=list(reversed(TABLES)))


def _bet(user: User, *, status: str, stake: str, odds: str | None, ret: str | None = None, book: str = "PARIMATCH", **extra: object) -> UserPlacedBet:
    now = datetime.now(UTC)
    settled = status != "PENDING"
    return UserPlacedBet(
        id=uuid.uuid4(), user_id=user.id, bookmaker=book, structure=extra.pop("structure", "SINGLE"), stake_inr=Decimal(stake),  # type: ignore[arg-type]
        placed_odds=Decimal(odds) if odds else None, placed_at=extra.pop("placed_at", now), status=status,  # type: ignore[arg-type]
        return_inr=Decimal(ret) if ret is not None else None, pnl_inr=(Decimal(ret) - Decimal(stake)) if ret is not None else None,
        settled_at=now if settled else None, **extra,
    )


def _leg(bet: UserPlacedBet, *, fixture: str, home: str, away: str, sport: str, odds: str, position: int = 0, **extra: object) -> UserPlacedLeg:
    return UserPlacedLeg(
        id=uuid.uuid4(), bet_id=bet.id, position=position, fixture_id=fixture, home=home, away=away, sport_key=sport,
        market="Match Winner", selection="HOME", odds=Decimal(odds), result=extra.pop("result", bet.status), **extra,  # type: ignore[arg-type]
    )


@pytest_asyncio.fixture
async def desk(ledger: None, session_factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:  # noqa: ARG001
    me = User(id=uuid.uuid4(), username="todar_mal_admin", hashed_password="x", role="ADMIN")
    other = User(id=uuid.uuid4(), username="someone_else", hashed_password="x", role="USER")
    async with session_factory() as db:
        db.add_all([me, other])
        await db.flush()
        epl = _bet(me, status="WON", stake="2000.00", odds="2.20", ret="4400.00", booking_code="PM-99281", clv_pct=7.317, clv_sharp_pct=5.8,
                   closing_odds=Decimal("2.05"), placed_at=datetime.now(UTC) - timedelta(hours=1))
        epl_leg = _leg(epl, fixture="epl-ars-che", home="Arsenal", away="Chelsea", sport="soccer_epl", odds="2.20", closing_odds=Decimal("2.05"))
        # Only the leg's closing price was captured: the slip's CLV is derived from it
        ipl = _bet(me, status="LOST", stake="1000.00", odds="1.90", ret="0.00", book="1XBET", placed_at=datetime.now(UTC) - timedelta(hours=2))
        ipl_leg = _leg(ipl, fixture="ipl-mi-csk", home="Mumbai", away="Chennai", sport="cricket_ipl", odds="1.90", closing_odds=Decimal("2.00"))
        # A double across two sports, never priced at the close: settled but outside the CLV sample
        mixed = _bet(me, status="LOST", stake="500.00", odds="4.00", ret="0.00", structure="DOUBLE", placed_at=datetime.now(UTC) - timedelta(hours=3))
        mixed_legs = [_leg(mixed, fixture="m1", home="A1", away="B1", sport="soccer_epl", odds="2.00"),
                      _leg(mixed, fixture="m2", home="A2", away="B2", sport="cricket_ipl", odds="2.00", position=1)]
        pending = _bet(me, status="PENDING", stake="300.00", odds="2.50")
        pending_leg = _leg(pending, fixture="epl-liv-mun", home="Liverpool", away="Manchester United", sport="soccer_epl", odds="2.50")
        theirs = _bet(other, status="WON", stake="9999.00", odds="3.00", ret="29997.00", clv_pct=50.0)
        theirs_leg = _leg(theirs, fixture="x", home="X", away="Y", sport="soccer_epl", odds="3.00")
        db.add_all([epl, ipl, mixed, pending, theirs])
        await db.flush()
        db.add_all([epl_leg, ipl_leg, *mixed_legs, pending_leg, theirs_leg])
        await db.flush()
        db.add_all([
            ModelPredictionFeedback(bet_id=epl.id, leg_id=epl_leg.id, fixture_id="epl-ars-che", sport_key="soccer_epl", market="Match Winner",
                                    selection="HOME", model_name="dixon_coles", predicted_prob=0.58, actual_outcome=1.0, brier_score=0.1764, log_loss=0.5447),
            ModelPredictionFeedback(bet_id=theirs.id, leg_id=theirs_leg.id, fixture_id="x", sport_key="soccer_epl", market="Match Winner",
                                    selection="HOME", model_name="dixon_coles", predicted_prob=0.10, actual_outcome=1.0, brier_score=0.81, log_loss=2.3026),
        ])
        ids = {"me": me.id, "pending": pending.id}  # captured before the commit expires the rows
        await db.commit()
    identity = SimpleNamespace(id=ids["me"], username="todar_mal_admin", role="ADMIN")
    archive_app.dependency_overrides[get_current_user] = lambda: identity
    archive_app.dependency_overrides[get_current_admin] = lambda: identity
    return ids


@pytest.mark.asyncio
async def test_dashboard_reports_only_the_users_measured_slips(client: AsyncClient, desk: dict[str, object]) -> None:  # noqa: ARG001
    resp = await client.get(f"{BASE}/clv-warehouse")
    assert resp.status_code == 200
    d = resp.json()
    assert (d["settled_bets_count"], d["pending_bets_count"], d["won_bets_count"], d["lost_bets_count"]) == (3, 1, 1, 2)
    assert d["total_staked_inr"] == 3500.0 and d["net_pnl_inr"] == 900.0 and d["roi_pct"] == 25.71  # someone else's slip is not here
    assert d["clv_sample_count"] == 2 and d["avg_clv_pct"] == round((7.317 + -5.0) / 2, 3) and d["beat_close_rate_pct"] == 50.0
    assert d["edge_tier"] == "POSITIVE_CLV" and d["edge_tier_provisional"] is True and d["edge_tier_min_sample"] == 30
    assert d["calibration_sample_count"] == 1 and d["mean_brier_score"] == pytest.approx(0.1764)  # only this user's slips
    assert d["inr_per_usd_rate"] is None and d["net_pnl_usd"] is None  # no measured rate: no USD figure
    assert d["architect"] == DEVELOPER

    slips = {s["booking_code"] or s["structure"]: s for s in d["settled_bets"]}
    assert slips["PM-99281"]["clv_pct"] == 7.317 and slips["PM-99281"]["closing_odds"] == 2.05
    lost_single = next(s for s in d["settled_bets"] if s["bookmaker"] == "1XBET")
    assert lost_single["clv_pct"] == -5.0 and lost_single["closing_odds"] == 2.0  # derived from the leg's close
    assert slips["DOUBLE"]["sport_key"] == "multi_sport" and slips["DOUBLE"]["clv_pct"] is None
    assert [s["placed_at"] for s in d["settled_bets"]] == sorted((s["placed_at"] for s in d["settled_bets"]), reverse=True)

    assert {b["bookmaker"]: b["settled_bets"] for b in d["by_bookmaker"]} == {"1XBET": 1, "PARIMATCH": 2}
    assert {s["sport_key"] for s in d["by_sport"]} == {"cricket_ipl", "multi_sport", "soccer_epl"}

    one_book = (await client.get(f"{BASE}/clv-warehouse", params={"bookmaker": "1xbet"})).json()
    assert one_book["settled_bets_count"] == 1 and one_book["pending_bets_count"] == 0
    epl_only = (await client.get(f"{BASE}/clv-warehouse", params={"sport_key": "soccer_epl", "limit": 1})).json()
    assert epl_only["settled_bets_count"] == 1 and epl_only["pending_bets_count"] == 1 and len(epl_only["settled_bets"]) == 1


@pytest.mark.asyncio
async def test_recording_a_score_settles_what_it_decides(client: AsyncClient, desk: dict[str, object], db_session: AsyncSession) -> None:
    body = {"fixture_id": "epl-liv-mun", "home": "Liverpool", "away": "Manchester United", "sport_key": "soccer_epl", "home_goals": 2, "away_goals": 1}
    resp = await client.post(f"{BASE}/clv-warehouse/scores", json=body)
    assert resp.status_code == 200
    out = resp.json()
    assert (out["fixture_id"], out["settled_legs"], out["settled_bets"]) == ("epl-liv-mun", 1, 1)
    d = out["dashboard"]
    assert d["pending_bets_count"] == 0 and d["settled_bets_count"] == 4 and d["archived_scores_count"] == 1
    assert d["recent_scores"][0]["home_goals"] == 2 and d["recent_scores"][0]["source"] == "admin"
    settled = next(s for s in d["settled_bets"] if s["bet_id"] == str(desk["pending"]))
    assert settled["status"] == "WON" and settled["return_inr"] == 750.0 and settled["settlement_source"] == "AUTOMATED"

    score = await db_session.get(FixtureScore, "epl-liv-mun")
    assert score is not None and score.recorded_by == desk["me"]


@pytest.mark.asyncio
async def test_score_entry_is_admin_only_and_validated(client: AsyncClient, desk: dict[str, object]) -> None:  # noqa: ARG001
    final_without_goals = {"home": "Liverpool", "away": "Manchester United", "status": "FINAL"}
    assert (await client.post(f"{BASE}/clv-warehouse/scores", json=final_without_goals)).status_code == 422
    archive_app.dependency_overrides.pop(get_current_admin)
    archive_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uuid.uuid4(), username="viewer", role="USER")
    body = {"fixture_id": "epl-liv-mun", "home": "Liverpool", "away": "Manchester United", "home_goals": 2, "away_goals": 1}
    assert (await client.post(f"{BASE}/clv-warehouse/scores", json=body)).status_code == 403
