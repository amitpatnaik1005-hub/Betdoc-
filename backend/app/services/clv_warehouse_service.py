"""Group 93: TODAR MAL's historical match, score & closing-line-value warehouse.

Reads the user's own real recorded slips (``UserPlacedBet`` and their legs), the final scores
(``FixtureScore``) and the model calibration feedback written when those slips settled
(``ModelPredictionFeedback``). Paper trades, arbitrage signals and every currency but INR/USD stay out.

* CLV per slip: the stored ``clv_pct`` / ``clv_sharp_pct`` (Group 73's closing-line capture); when only the
  closing price was stored, ``placed / closing - 1`` from the slip, or from its single leg. No closing price,
  no CLV: the slip simply is not in the sample.
* USD: ``CurrencyService`` with no refresh. No fresh measured rate, no USD figure.
* Recording a score goes through the same path as the Oracle's ``POST /oracle/scores``
  (``user_pnl_tracker.record_score`` then ``settle_pending``), so a score entered here settles the slips
  it decides exactly as it would there. ``FixtureScore`` keeps who recorded it, when, and from where.

Developer Credit: Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.domain.archive.backup_vault_math import convert_inr_to_usd
from app.domain.archive.clv_warehouse_math import (
    ClvAggregateMetrics,
    SettledBetInput,
    TierThresholds,
    compute_leg_clv_pct,
    evaluate_clv_warehouse_aggregates,
)
from app.models.control_panel import SystemSettingsModel
from app.models.feedback import ModelPredictionFeedback
from app.models.user_bets_ledger import FixtureScore, PlacedStatus, UserPlacedBet, UserPlacedLeg
from app.schemas.archive import (
    BookmakerClvBreakdownRead,
    ClvWarehouseDashboardResponse,
    ClvWarehouseScoreRecordedResponse,
    HistoricalFixtureScoreArchiveRead,
    HistoricalSettledBetArchiveRead,
    SportClvBreakdownRead,
)
from app.schemas.ashoka import ScoreIn
from app.services import user_pnl_tracker as tracker

MULTI_SPORT = "multi_sport"
UNKNOWN_SPORT = "unknown"


def _usd(amount: Decimal | None, rate: Decimal | None) -> float | None:
    converted = convert_inr_to_usd(amount, rate)
    return float(converted) if converted is not None else None


def _thresholds(settings: Settings) -> TierThresholds:
    return TierThresholds(
        sharp_alpha_min_pct=settings.CLV_WAREHOUSE_SHARP_ALPHA_MIN_PCT,
        sharp_beat_rate_min=settings.CLV_WAREHOUSE_SHARP_BEAT_RATE_MIN,
        positive_min_pct=settings.CLV_WAREHOUSE_POSITIVE_MIN_PCT,
        adverse_below_pct=settings.CLV_WAREHOUSE_ADVERSE_BELOW_PCT,
        min_sample=settings.CLV_WAREHOUSE_MIN_SAMPLE,
    )


def slip_sport(legs: Sequence[UserPlacedLeg]) -> str:
    sports = {leg.sport_key for leg in legs if leg.sport_key}
    return sports.pop() if len(sports) == 1 else (MULTI_SPORT if sports else UNKNOWN_SPORT)


def slip_closing_odds(bet: UserPlacedBet, legs: Sequence[UserPlacedLeg]) -> Decimal | None:
    """The slip's recorded closing price, or its single leg's (a single's slip price is that leg's)."""
    if bet.closing_odds is not None:
        return bet.closing_odds
    return legs[0].closing_odds if len(legs) == 1 else None


def slip_clv(bet: UserPlacedBet, closing: Decimal | None) -> float | None:
    return float(bet.clv_pct) if bet.clv_pct is not None else compute_leg_clv_pct(bet.placed_odds, closing)


def _matches(value: str | None, wanted: str | None) -> bool:
    return wanted is None or wanted.upper() == "ALL" or (value or "").upper() == wanted.upper()


class ClvWarehouseService:
    """The CLV & historical warehouse: the user's settled real slips, final scores and calibration."""

    async def _present(self, db: AsyncSession) -> set[str]:
        def _names(sync_session: Session) -> set[str]:
            return set(inspect(sync_session.connection()).get_table_names())

        return await db.run_sync(_names)

    async def _usd_rate(self, db: AsyncSession, redis: Redis | None, settings: Settings, now: datetime, present: set[str]) -> Decimal | None:
        from app.services.currency_service import CurrencyService, CurrencyUnavailableError  # noqa: PLC0415

        if "currency_exchange_rates" not in present and redis is None:
            return None  # nowhere a rate could be read from
        try:
            return (await CurrencyService(settings, redis).inr_per_unit(db, "USD", now, allow_refresh=False)).inr_per_unit
        except CurrencyUnavailableError:
            return None

    async def _architect(self, db: AsyncSession, present: set[str]) -> str:
        if "system_settings" in present:
            from app.services.twin.vetting import developer_credit  # noqa: PLC0415 - pulls in the oracle engine

            return await developer_credit(db)
        return str(SystemSettingsModel.__table__.c.developer_name.default.arg)

    # ------------------------------------------------------------ scores
    async def record_score(
        self, db: AsyncSession, redis: Redis | None, settings: Settings, payload: ScoreIn, *, user_id: uuid.UUID, recorded_by: uuid.UUID
    ) -> ClvWarehouseScoreRecordedResponse:
        now = datetime.now(UTC)
        row = await tracker.record_score(db, payload, source="admin", by=recorded_by, now=now)
        fixture_id = row.fixture_id
        await db.commit()
        if db.bind is None:  # pragma: no cover - every request session is bound
            raise RuntimeError("the request session has no engine to settle with")
        report = await tracker.settle_pending(async_sessionmaker(db.bind, expire_on_commit=False), now)
        await tracker.bump(redis, report.users)
        db.expire_all()  # the settler committed in its own session: read its results afresh
        return ClvWarehouseScoreRecordedResponse(
            fixture_id=fixture_id,
            settled_legs=report.legs,
            settled_bets=report.bets,
            dashboard=await self.dashboard(db, redis, settings, user_id=user_id),
        )

    # ------------------------------------------------------------ dashboard
    async def dashboard(
        self,
        db: AsyncSession,
        redis: Redis | None,
        settings: Settings,
        *,
        user_id: uuid.UUID,
        bookmaker: str | None = None,
        sport_key: str | None = None,
        limit: int | None = None,
    ) -> ClvWarehouseDashboardResponse:
        now = datetime.now(UTC)
        present = await self._present(db)
        rate = await self._usd_rate(db, redis, settings, now, present)
        thresholds = _thresholds(settings)
        row_limit = limit or settings.CLV_WAREHOUSE_DEFAULT_LIMIT

        bets: list[UserPlacedBet] = []
        legs_by_bet: dict[uuid.UUID, list[UserPlacedLeg]] = {}
        if {"user_placed_bets", "user_placed_legs"} <= present:
            bets, legs_by_bet = await tracker.user_bets(db, user_id)

        pending = 0
        inputs: list[SettledBetInput] = []
        reads: list[HistoricalSettledBetArchiveRead] = []
        by_book: dict[str, list[SettledBetInput]] = defaultdict(list)
        by_sport: dict[str, list[SettledBetInput]] = defaultdict(list)
        settled_ids: list[uuid.UUID] = []
        for bet in bets:  # newest first
            legs = legs_by_bet.get(bet.id, [])
            sport = slip_sport(legs)
            if not _matches(bet.bookmaker, bookmaker) or not _matches(sport, sport_key):
                continue
            if bet.status == PlacedStatus.PENDING.value:
                pending += 1
                continue
            stake = Decimal(bet.stake_inr)
            returned = Decimal(bet.return_inr) if bet.return_inr is not None else Decimal("0")
            pnl = Decimal(bet.pnl_inr) if bet.pnl_inr is not None else returned - stake
            closing = slip_closing_odds(bet, legs)
            clv = slip_clv(bet, closing)
            sharp = float(bet.clv_sharp_pct) if bet.clv_sharp_pct is not None else None
            item = SettledBetInput(
                bet_id=str(bet.id), bookmaker=bet.bookmaker, sport_key=sport, status=bet.status,
                stake_inr=stake, return_inr=returned, pnl_inr=pnl, clv_pct=clv, clv_sharp_pct=sharp,
            )
            inputs.append(item)
            by_book[bet.bookmaker].append(item)
            by_sport[sport].append(item)
            settled_ids.append(bet.id)
            if len(reads) < row_limit:
                reads.append(self._slip_read(bet, legs, sport, closing, clv, sharp, stake, returned, pnl, rate))

        overall = evaluate_clv_warehouse_aggregates(inputs, inr_per_usd=rate, thresholds=thresholds)
        calibration_count, brier, log_loss = await self._calibration(db, present, settled_ids)
        scores_count, scores = await self._scores(db, present, sport_key, settings.CLV_WAREHOUSE_RECENT_SCORES)

        return ClvWarehouseDashboardResponse(
            settled_bets_count=overall.settled_bets_count,
            pending_bets_count=pending,
            won_bets_count=overall.won_bets_count,
            lost_bets_count=overall.lost_bets_count,
            void_bets_count=overall.void_bets_count,
            win_rate_pct=overall.win_rate_pct,
            total_staked_inr=float(overall.total_staked_inr),
            total_staked_usd=_float(overall.total_staked_usd),
            total_return_inr=float(overall.total_return_inr),
            total_return_usd=_float(overall.total_return_usd),
            net_pnl_inr=float(overall.net_pnl_inr),
            net_pnl_usd=_float(overall.net_pnl_usd),
            roi_pct=overall.roi_pct,
            clv_sample_count=overall.clv_sample_count,
            avg_clv_pct=overall.avg_clv_pct,
            avg_clv_sharp_pct=overall.avg_clv_sharp_pct,
            beat_close_rate_pct=overall.beat_close_rate_pct,
            edge_tier=overall.edge_tier,
            edge_tier_provisional=overall.edge_tier_provisional,
            edge_tier_min_sample=thresholds.min_sample,
            calibration_sample_count=calibration_count,
            mean_brier_score=brier,
            mean_log_loss=log_loss,
            archived_scores_count=scores_count,
            inr_per_usd_rate=float(rate) if rate is not None else None,
            architect=await self._architect(db, present),
            by_bookmaker=[self._book_read(name, evaluate_clv_warehouse_aggregates(rows, inr_per_usd=rate, thresholds=thresholds))
                          for name, rows in sorted(by_book.items())],
            by_sport=[self._sport_read(name, evaluate_clv_warehouse_aggregates(rows, inr_per_usd=rate, thresholds=thresholds))
                      for name, rows in sorted(by_sport.items())],
            settled_bets=reads,
            recent_scores=scores,
        )

    async def _calibration(self, db: AsyncSession, present: set[str], bet_ids: list[uuid.UUID]) -> tuple[int, float | None, float | None]:
        """Brier and log loss over the feedback rows of exactly the slips shown."""
        if "model_prediction_feedback" not in present or not bet_ids:
            return 0, None, None
        count, brier, log_loss = (await db.execute(
            select(func.count(), func.avg(ModelPredictionFeedback.brier_score), func.avg(ModelPredictionFeedback.log_loss))
            .where(ModelPredictionFeedback.bet_id.in_(bet_ids))
        )).one()
        return int(count or 0), (round(float(brier), 6) if brier is not None else None), (round(float(log_loss), 6) if log_loss is not None else None)

    async def _scores(self, db: AsyncSession, present: set[str], sport_key: str | None, limit: int) -> tuple[int, list[HistoricalFixtureScoreArchiveRead]]:
        if "fixture_scores" not in present:
            return 0, []
        count_stmt = select(func.count()).select_from(FixtureScore)
        stmt = select(FixtureScore).order_by(FixtureScore.recorded_at.desc()).limit(limit)
        if sport_key is not None and sport_key.upper() != "ALL":
            count_stmt = count_stmt.where(FixtureScore.sport_key == sport_key)
            stmt = stmt.where(FixtureScore.sport_key == sport_key)
        count = int(await db.scalar(count_stmt) or 0)
        return count, [HistoricalFixtureScoreArchiveRead.model_validate(row) for row in (await db.execute(stmt)).scalars()]

    @staticmethod
    def _slip_read(
        bet: UserPlacedBet, legs: Sequence[UserPlacedLeg], sport: str, closing: Decimal | None, clv: float | None, sharp: float | None,
        stake: Decimal, returned: Decimal, pnl: Decimal, rate: Decimal | None,
    ) -> HistoricalSettledBetArchiveRead:
        fixtures = " · ".join(f"{leg.home} vs {leg.away}" for leg in legs[:2]) or (bet.notes or f"Slip {str(bet.id)[:8]}")
        markets = " · ".join(f"{leg.market}: {leg.selection}" for leg in legs[:2]) or bet.structure
        if len(legs) > 2:
            fixtures += f" (+{len(legs) - 2} more)"
            markets += f" (+{len(legs) - 2} more)"
        return HistoricalSettledBetArchiveRead(
            bet_id=bet.id, booking_code=bet.booking_code, bookmaker=bet.bookmaker_name or bet.bookmaker, structure=bet.structure,
            status=bet.status, sport_key=sport, fixture_summary=fixtures, market_summary=markets,
            placed_odds=float(bet.placed_odds) if bet.placed_odds is not None else None,
            closing_odds=float(closing) if closing is not None else None, clv_pct=clv, clv_sharp_pct=sharp,
            stake_inr=float(stake), stake_usd=_usd(stake, rate), return_inr=float(returned), return_usd=_usd(returned, rate),
            pnl_inr=float(pnl), pnl_usd=_usd(pnl, rate), settlement_source=bet.settlement_source, root_cause_tag=bet.root_cause_tag,
            placed_at=bet.placed_at, settled_at=bet.settled_at,
        )

    @staticmethod
    def _book_read(name: str, agg: ClvAggregateMetrics) -> BookmakerClvBreakdownRead:
        return BookmakerClvBreakdownRead(
            bookmaker=name, settled_bets=agg.settled_bets_count, win_rate_pct=agg.win_rate_pct,
            total_staked_inr=float(agg.total_staked_inr), total_staked_usd=_float(agg.total_staked_usd),
            net_pnl_inr=float(agg.net_pnl_inr), net_pnl_usd=_float(agg.net_pnl_usd), roi_pct=agg.roi_pct,
            clv_sample_count=agg.clv_sample_count, avg_clv_pct=agg.avg_clv_pct, beat_close_rate_pct=agg.beat_close_rate_pct,
        )

    @staticmethod
    def _sport_read(name: str, agg: ClvAggregateMetrics) -> SportClvBreakdownRead:
        return SportClvBreakdownRead(
            sport_key=name, settled_bets=agg.settled_bets_count, win_rate_pct=agg.win_rate_pct,
            net_pnl_inr=float(agg.net_pnl_inr), net_pnl_usd=_float(agg.net_pnl_usd), roi_pct=agg.roi_pct,
            clv_sample_count=agg.clv_sample_count, avg_clv_pct=agg.avg_clv_pct,
        )


def _float(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None
