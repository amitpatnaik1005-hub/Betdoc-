import logging
import re
import uuid
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.capital.vault import SQL_ZERO, money_out, to_decimal
from app.models import BetLedger, ExchangeAccount
from app.schemas.arena import (
    ActiveBetOverview,
    ArenaFilterParams,
    CashOutQuote,
    SettlementRequest,
    StrategyAnalyticsNode,
)

arena_log = logging.getLogger("betdoc.arena")

ACTIVE_STATUSES: tuple[str, ...] = ("PENDING", "PENDING_NETWORK", "ACCEPTED", "UNKNOWN")
RESOLVED_STATUSES: tuple[str, ...] = (
    "WON", "LOST", "VOID", "HALF_WON", "HALF_LOST", "CASH_OUT", "REJECTED",
)
# REJECTED bets were never placed: excluded from analytics (payout - stake would fake a loss).
ANALYTICS_STATUSES: tuple[str, ...] = ("WON", "LOST", "VOID", "HALF_WON", "HALF_LOST", "CASH_OUT")

CURRENCY_PATTERN = re.compile(r"^[A-Z0-9]{3,10}$")
Q4 = Decimal("0.0001")
ZERO = Decimal(0)
ONE = Decimal(1)


class ArenaError(Exception):
    status_code: int = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ArenaNotFoundError(ArenaError):
    status_code = 404


class ArenaStateError(ArenaError):
    status_code = 400


def q4(value: Decimal) -> Decimal:
    return to_decimal(value).quantize(Q4, rounding=ROUND_HALF_EVEN)


def _strict_decimal(value: float, label: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ArenaStateError(f"{label} is not a valid number") from exc
    if not d.is_finite():
        raise ArenaStateError(f"{label} must be finite")
    return d


class ArenaEngine:
    # ---------- Secure query scaffolding ----------

    @staticmethod
    def _owned_bet(user_id: uuid.UUID, bet_id: uuid.UUID) -> Select:
        """RELATIONAL SECURITY: a bet is only reachable through its owner's ExchangeAccount."""
        return (
            select(BetLedger)
            .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
            .where(BetLedger.id == bet_id, ExchangeAccount.user_id == user_id)
        )

    # ---------- 1. Active bets ----------

    async def get_active_bets(self, db: AsyncSession, user_id: uuid.UUID) -> list[ActiveBetOverview]:
        stmt = (
            select(BetLedger, ExchangeAccount.exchange_name)
            .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
            .where(ExchangeAccount.user_id == user_id, BetLedger.status.in_(ACTIVE_STATUSES))
            .order_by(BetLedger.placed_at.desc())
        )
        rows = (await db.execute(stmt)).all()
        return [
            ActiveBetOverview(
                id=bet.id,
                exchange=str(exchange_name),
                match_id=str(bet.match_id),
                market_type=str(bet.market_type),
                selection=str(bet.selection),
                stake=money_out(to_decimal(bet.stake)),
                odds=money_out(to_decimal(bet.odds)),
                placed_at=bet.placed_at,
                status=str(bet.status),
                strategy_name=bet.strategy_name,
            )
            for bet, exchange_name in rows
        ]

    # ---------- 2. Cash-out quote ----------

    async def get_cash_out_quote(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        bet_id: uuid.UUID,
        current_true_prob: float,
        margin_pct: float,
    ) -> CashOutQuote:
        bet = await db.scalar(self._owned_bet(user_id, bet_id))
        if bet is None:
            raise ArenaNotFoundError("Bet not found")
        if bet.status not in ACTIVE_STATUSES:
            raise ArenaStateError(f"Cash out unavailable: bet is {bet.status}")

        p = _strict_decimal(current_true_prob, "current_true_prob")
        margin = _strict_decimal(margin_pct, "margin_pct")
        if p < ZERO or p > ONE:
            raise ArenaStateError("current_true_prob must be within [0, 1]")
        if margin < ZERO or margin >= ONE:
            raise ArenaStateError("margin_pct must be within [0, 1)")

        stake = to_decimal(bet.stake)
        odds = to_decimal(bet.odds)
        max_payout = stake * odds

        fair_value = max_payout * p
        offer = fair_value * (ONE - margin)
        offer = max(ZERO, min(offer, max_payout))

        fair_q = q4(fair_value)
        offer_q = q4(offer)
        live_odds = q4(ONE / p) if p > ZERO else ZERO

        return CashOutQuote(
            bet_id=bet.id,
            original_stake=money_out(stake),
            original_odds=money_out(odds),
            current_live_odds=money_out(live_odds),
            fair_value=money_out(fair_q),
            cash_out_offered=money_out(offer_q),
            margin_applied=money_out(fair_q - offer_q),
        )

    # ---------- 3. Settlement state machine ----------

    @staticmethod
    def _resolve_payout(stake: Decimal, odds: Decimal, request: SettlementRequest) -> Decimal:
        max_payout = stake * odds
        status = request.status

        if request.payout is not None:
            manual = _strict_decimal(request.payout, "payout")
            # EXPLOIT GUARD
            if manual < ZERO or manual > max_payout:
                raise ArenaStateError(f"payout must be between 0 and {q4(max_payout)} (stake x odds)")
            manual_q = q4(manual)
            # Status consistency: P&L = payout - stake is the single source of truth.
            if status in ("LOST", "REJECTED") and manual_q != ZERO:
                raise ArenaStateError(f"{status} requires payout = 0")
            if status == "VOID" and manual_q != q4(stake):
                raise ArenaStateError(f"VOID requires payout = stake ({q4(stake)})")
            if status == "HALF_LOST" and manual_q > q4(stake):
                raise ArenaStateError(f"HALF_LOST payout cannot exceed stake ({q4(stake)})")
            return manual_q

        match status:
            case "WON":
                return q4(max_payout)
            case "LOST" | "REJECTED":
                return q4(ZERO)
            case "VOID":
                return q4(stake)
            case "HALF_WON":
                return q4(stake + (max_payout - stake) / Decimal(2))
            case "HALF_LOST":
                return q4(stake / Decimal(2))
            case "CASH_OUT":
                raise ArenaStateError("CASH_OUT requires an explicit payout")
        raise ArenaStateError(f"Unsupported settlement status: {status}")

    async def settle_bet(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        bet_id: uuid.UUID,
        request: SettlementRequest,
    ) -> None:
        stmt = self._owned_bet(user_id, bet_id).with_for_update(of=BetLedger)
        try:
            bet = await db.scalar(stmt)
            if bet is None:
                raise ArenaNotFoundError("Bet not found")

            previous_status = str(bet.status)
            previous_payout = bet.payout
            was_active = previous_status in ACTIVE_STATUSES

            if not was_active and previous_status not in RESOLVED_STATUSES:
                raise ArenaStateError(f"Bet is in unexpected state {previous_status}")
            if not was_active and not request.force_regrade:
                raise ArenaStateError(
                    f"Bet already resolved as {previous_status}; set force_regrade=true to regrade"
                )

            payout = self._resolve_payout(to_decimal(bet.stake), to_decimal(bet.odds), request)
        except ArenaError:
            await db.rollback()  # release the row lock immediately
            raise

        bet.status = request.status
        bet.payout = payout
        if bet.resolved_at is None:
            bet.resolved_at = func.now()  # AUDIT: never overwrite an existing resolution timestamp
        await db.commit()

        arena_log.info(
            "%s bet=%s user=%s %s(%s) -> %s(%s)",
            "REGRADE" if not was_active else "SETTLE",
            bet_id, user_id, previous_status, previous_payout, request.status, payout,
        )

    # ---------- 4. Strategy analytics ----------

    async def get_strategy_analytics(
        self, db: AsyncSession, user_id: uuid.UUID, filters: ArenaFilterParams
    ) -> list[StrategyAnalyticsNode]:
        currency = (filters.currency or "").strip().upper()
        if not CURRENCY_PATTERN.match(currency):
            raise ArenaStateError("currency must be 3-10 alphanumeric characters")

        group_key = func.coalesce(BetLedger.strategy_name, BetLedger.market_type)
        status = BetLedger.status

        stmt = (
            select(
                group_key.label("strategy_name"),
                func.count(BetLedger.id).label("total_bets"),
                func.count(BetLedger.id).filter(status == "WON").label("won"),
                func.count(BetLedger.id).filter(status == "LOST").label("lost"),
                func.count(BetLedger.id).filter(status == "HALF_WON").label("half_won"),
                func.count(BetLedger.id).filter(status == "HALF_LOST").label("half_lost"),
                func.count(BetLedger.id).filter(status == "VOID").label("voided"),
                func.count(BetLedger.id).filter(status == "CASH_OUT").label("cashed_out"),
                # VOID DILUTION: voids carry no risk, so they add no volume.
                func.coalesce(func.sum(BetLedger.stake).filter(status != "VOID"), SQL_ZERO).label("volume"),
                # UNIVERSAL P&L: sum(payout - stake); coalesce inside guards legacy NULL payouts.
                func.coalesce(
                    func.sum(func.coalesce(BetLedger.payout, SQL_ZERO) - BetLedger.stake), SQL_ZERO
                ).label("net_profit"),
            )
            .select_from(BetLedger)
            .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
            .where(
                ExchangeAccount.user_id == user_id,
                BetLedger.currency == currency,
                BetLedger.status.in_(ANALYTICS_STATUSES),
                BetLedger.resolved_at.is_not(None),
            )
            .group_by(group_key)
        )
        rows = (await db.execute(stmt)).all()

        nodes: list[StrategyAnalyticsNode] = []
        for row in rows:
            volume = to_decimal(row.volume)
            net = to_decimal(row.net_profit)
            roi = (net / volume * Decimal(100)) if volume != ZERO else ZERO
            nodes.append(
                StrategyAnalyticsNode(
                    strategy_name=str(row.strategy_name),
                    total_bets=int(row.total_bets or 0),
                    bets_won=int(row.won or 0),
                    bets_lost=int(row.lost or 0),
                    bets_half_won=int(row.half_won or 0),
                    bets_half_lost=int(row.half_lost or 0),
                    bets_void=int(row.voided or 0),
                    bets_cashed_out=int(row.cashed_out or 0),
                    volume=money_out(volume),
                    net_profit=money_out(net),
                    roi_pct=money_out(roi),
                )
            )
        return sorted(nodes, key=lambda n: n.net_profit, reverse=True)
