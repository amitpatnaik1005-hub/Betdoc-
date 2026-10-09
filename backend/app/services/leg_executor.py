"""Multi-leg execution (Group 64): arbitrages and hedges through the Omni-Sniper, one leg at a time.

Every leg is an ordinary two-phase CFO execution (``TradeExecutor.execute_leg``): reserved under the
bankroll lock, fired through the sniper, committed or rolled back. What this module adds is the
order of the legs and what each fill means for the next one: legging risk.

1. Pre-flight, before anything fires: the live price of every leg (refused if a book now offers
   less than the user saw), that the legs cover every outcome of the market, the bankroll, and every
   risk guard for every leg (exposure checked against the whole arbitrage, velocity per leg). A leg
   that would be blocked is found here, not after Leg A is already live.
2. Leg A fires first (the exchange leg: the one a thin market partly fills).
3. Its receipt says what actually matched. A fill below the request (₹10,000 asked, ₹4,000 matched)
   re-sizes every remaining leg before it fires: each is staked to pay what the legs already placed
   pay, ``stake = payout / rupee_odds`` (the proportional ``planned * filled / requested`` when the
   price held), so the outcomes stay level.
4. Leg A fails (rejected, blocked, unconfirmed): nothing else fires. An unconfirmed leg stops the
   sequence too: hedging a bet that may not exist would open a naked one.
5. Each leg's ``min_acceptable_odds`` is the price at which its outcome stops covering every stake
   placed and to be placed (after the remaining legs re-size), so a slipped fill can never turn the
   arbitrage into a loss. Hedge legs use the same idea against their target, and never go past the
   user's slippage limit.

A plan that stops part-way is ``LEGGED``: the receipt carries the book's real profit per outcome
and the Active Portfolio offers the cover at the next best prices.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.domain.math.arbitrage_calc import (
    ONE,
    PAISA,
    ZERO,
    ArbitrageMathError,
    HeldBet,
    Offer,
    best_offers,
    book_profits,
    from_inr,
    hedge_after_fills,
    hedge_book,
    leg_cost_inr,
    stake_arbitrage,
    to_inr,
)
from app.models.cfo_vault import AuditEvent
from app.schemas.cfo_vault import ExecutionReceipt
from app.schemas.portfolio import ArbitrageExecuteRequest, HedgeExecuteRequest, LegReceipt, MultiLegReceipt
from app.services.aryabhata_pipeline import load_risk_limits
from app.services.cfo_execution import TradeExecutor
from app.services.cfo_ledger import CfoError, DuplicateExecutionError, OrderTicket, audit_row, opening_balance, read_account, write_audit
from app.services.fx_rates import FxUnavailableError
from app.services.portfolio_manager import PortfolioManager, PricingContext, market_offers, market_outcomes
from app.services.portfolio_positions import load_open_bets
from app.services.risk_guard import GuardReport, RiskGuardViolation, load_limits
from app.services.sniper import SniperFeed

logger = logging.getLogger("betdoc.legging")

_HUNDRED = Decimal(100)
_MIN_ODDS = Decimal("1.0001")
_LEG_NAMESPACE = uuid.UUID("6b1c9f3e-64a0-4d2e-9a51-0c4f64e0b064")
_STAKE_DRIFT = Decimal("0.02")  # a hedge whose stakes moved more than 2% since the modal showed them is re-confirmed


def _utcnow() -> datetime:
    return datetime.now(UTC)


def leg_key(group_id: uuid.UUID, selection: str) -> uuid.UUID:
    """Each leg's idempotency key: stable per (order, outcome), so a replay can never fire a leg twice."""
    return uuid.uuid5(_LEG_NAMESPACE, f"{group_id}|{selection}")


@dataclass(slots=True)
class _Leg:
    offer: Offer
    planned_inr: Decimal
    ccy: Decimal  # the stake in the leg's own currency (rupees for an INR leg)
    stake_inr: Decimal  # what that costs, as the ledger reserves it (rounded up to the paisa)
    floor: Decimal | None = None
    receipt: ExecutionReceipt | None = None
    status: str = "ABORTED"
    reason: str | None = None
    message: str | None = None
    held: HeldBet | None = None

    @classmethod
    def of(cls, offer: Offer, ccy: Decimal) -> _Leg:
        cost = leg_cost_inr(ccy, offer.fx)
        return cls(offer, cost, ccy, cost)

    @property
    def selection(self) -> str:
        return self.offer.selection

    @property
    def foreign(self) -> bool:
        return self.offer.currency != "INR"

    def resize(self, ccy: Decimal) -> None:
        self.ccy = max(ccy, ZERO)
        self.stake_inr = leg_cost_inr(self.ccy, self.offer.fx)

    def value_inr(self) -> Decimal:
        """The stake's exact rupee value: payouts are computed from this, never from the rounded cost."""
        return to_inr(self.ccy, self.offer.fx)

    def stake_ccy(self) -> Decimal | None:
        return self.ccy if self.foreign else None

    def as_receipt(self) -> LegReceipt:
        r = self.receipt
        return LegReceipt(
            selection=self.selection,
            bookmaker_id=self.offer.provider,
            odds=self.offer.raw_odds,
            min_acceptable_odds=self.floor,
            planned_stake_inr=self.planned_inr,
            requested_stake_inr=self.stake_inr if self.status != "ABORTED" else None,
            filled_stake_inr=r.stake_inr if r is not None else None,
            matched_odds=r.odds if r is not None else None,
            status=self.status,  # type: ignore[arg-type]
            reason=self.reason,
            message=self.message,
            ledger_id=r.ledger_id if r is not None else None,
            remote_bet_id=r.remote_bet_id if r is not None else None,
        )


def _floor(offer: Offer, rupee_floor: Decimal, slippage_floor: Decimal | None = None) -> Decimal:
    """The raw price floor for a leg: never above the asked price, never at or below 1."""
    floor = offer.floor_for(max(rupee_floor, ONE + PAISA / _HUNDRED))
    if slippage_floor is not None:
        floor = max(floor, slippage_floor)
    return max(_MIN_ODDS, min(offer.raw_odds, floor))


# Sizing a leg once others are live must never raise out of the sequence: it ends it, reported
_SEQUENCE_ERRORS = (ArbitrageMathError, CfoError, ArithmeticError)


class LegExecutor:
    def __init__(
        self,
        executor: TradeExecutor,
        portfolio: PortfolioManager,
        redis: Redis | None,
        settings: Settings,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.executor = executor
        self.portfolio = portfolio
        self.redis = redis
        self.settings = settings
        self.clock = clock
        self.feed = SniperFeed(redis, settings, clock)

    # -------------------------------------------------------------- arbitrage
    async def execute_arbitrage(self, user_id: uuid.UUID, request: ArbitrageExecuteRequest) -> MultiLegReceipt:
        group = request.idempotency_key
        await self._claim_group(group)
        ctx, books, meta = await self.portfolio.market(request.fixture_id, request.market)
        if meta is not None and meta.commence_time is not None and meta.commence_time <= ctx.now:
            raise CfoError("MARKET_IN_PLAY", "Arbitrage runs pre-match only: in-play prices from polled feeds are already stale", status_code=409)
        selections = [leg.selection for leg in request.legs]
        outcomes = market_outcomes(books, [*selections, *(meta.labels if meta else ())])
        if sorted(outcomes) != sorted(selections):
            raise CfoError(
                "ARB_INCOMPLETE",
                f"An arbitrage has to cover every outcome ({', '.join(outcomes)}); an uncovered one loses every leg",
                status_code=422,
                detail={"outcomes": list(outcomes)},
            )
        chosen = self._live_legs(ctx, books, [(leg.selection, leg.bookmaker_id, leg.odds) for leg in request.legs])
        try:
            arb = stake_arbitrage(chosen, request.total_stake_inr)
        except ArbitrageMathError as exc:
            raise CfoError("NO_ARBITRAGE", str(exc), status_code=409) from exc
        min_margin = Decimal(str(self.settings.ARB_MIN_MARGIN_PCT)) / _HUNDRED
        if arb is None or arb.margin < min_margin:
            raise CfoError("NO_ARBITRAGE", "At these prices, after commission and FX, this is no longer an arbitrage", status_code=409)

        # Exchange legs first (a thin exchange market is what partly fills), then the longest price
        ordered = sorted(arb.legs, key=lambda leg: (-leg.offer.commission, -leg.rupee_odds, leg.offer.selection))
        legs = [_Leg.of(leg.offer, leg.stake_ccy) for leg in ordered]
        tickets = {leg.selection: self._ticket(user_id, request.fixture_id, request.market, group, "arbitrage", leg, meta) for leg in legs}
        await self._preflight(user_id, list(tickets.values()), total=arb.total_stake_inr, risk_reducing=False)
        await self._emit(user_id, group, f"Arbitrage {arb.margin * _HUNDRED:.3f}% · {len(legs)} legs · ₹{arb.total_stake_inr} · pre-flight passed")

        fired: list[HeldBet] = []
        target: Decimal | None = None  # what every outcome should pay: the lowest payout placed so far
        status = "COMPLETE"
        for index, leg in enumerate(legs):
            remaining = legs[index + 1 :]
            try:
                if target is not None:
                    before = leg.stake_inr
                    leg.resize(from_inr(target / leg.offer.rupee_odds(), leg.offer.fx))
                    if leg.stake_inr != before:
                        await self._emit(user_id, group, f"Leg {chr(65 + index)} {leg.selection}: re-sized ₹{before} → ₹{leg.stake_inr} to match the fills before it", "warning")
                    if leg.stake_inr <= ZERO:
                        leg.status, leg.reason = "SKIPPED", "RESIZED_TO_ZERO"
                        status = "LEGGED"
                        break
                placed = sum((h.stake_inr for h in fired), ZERO)
                # The rest re-size to this leg's payout: its outcome must still cover all of it
                rest_book = sum((ONE / later.offer.rupee_odds() for later in remaining), ZERO)
                if rest_book >= ONE:
                    raise CfoError("NO_ARBITRAGE", "The remaining legs no longer leave room for this one", status_code=409)
                leg.floor = _floor(leg.offer, (placed + leg.stake_inr) / (leg.value_inr() * (ONE - rest_book)))
            except _SEQUENCE_ERRORS as exc:
                if not fired:
                    raise exc if isinstance(exc, CfoError) else CfoError("NO_ARBITRAGE", str(exc), status_code=409) from exc
                leg.status, leg.reason, leg.message = "FAILED", "LEG_SIZING_FAILED", str(exc)  # legs are live: report, never raise
                status = "LEGGED"
                break
            ok = await self._fire(user_id, group, index, leg, replace(tickets[leg.selection], stake_inr=leg.stake_inr, stake_ccy=leg.stake_ccy()))
            if not ok:
                status = "ABORTED" if not fired and leg.status == "FAILED" else "LEGGED"
                break
            assert leg.held is not None
            fired.append(leg.held)
            payout = leg.held.stake_inr * leg.held.rupee_odds
            target = payout if target is None else min(target, payout)
            if leg.status == "PARTIAL":
                await self._emit(
                    user_id, group,
                    f"Leg {chr(65 + index)} partial fill: ₹{leg.held.stake_inr} of ₹{leg.stake_inr} matched; "
                    f"the remaining legs scale to {(leg.held.stake_inr / leg.stake_inr * _HUNDRED).quantize(Decimal('0.1'))}%",
                    "warning",
                )
        await self._abort_rest(user_id, group, "arbitrage", legs, request.fixture_id)
        return self._receipt(group, "arbitrage", legs, fired, outcomes, status)

    # -------------------------------------------------------------- hedge
    async def execute_hedge(self, user_id: uuid.UUID, request: HedgeExecuteRequest) -> MultiLegReceipt:
        group = request.idempotency_key
        await self._claim_group(group)
        async with self.executor.session_factory() as session:
            bets = await load_open_bets(session, user_id, fixture_id=request.fixture_id, market=request.market)
        if not bets:
            raise CfoError("NOTHING_TO_HEDGE", "No open position on this market", status_code=404)
        if any(bet.unconfirmed for bet in bets):
            raise CfoError("UNCONFIRMED_BET", "A bet in this book is unconfirmed: resolve it before hedging", status_code=409)
        ctx, books, meta = await self.portfolio.market(request.fixture_id, request.market)
        outcomes = market_outcomes(books, [*(b.selection for b in bets), *(meta.labels if meta else ())])
        try:
            profits = book_profits([ctx.held(bet) for bet in bets], outcomes)
            offers = best_offers([o for o in market_offers(books, ctx)[0] if ctx.routable(o.provider)])
            plan = hedge_book(profits, offers, anchor=request.anchor, fraction=request.fraction)
        except (ArbitrageMathError, FxUnavailableError) as exc:
            raise CfoError("NO_HEDGE", str(exc), status_code=409) from exc
        if not plan.legs:
            raise CfoError("NOTHING_TO_HEDGE", "This book already meets the target: no hedge is needed", status_code=409)
        self._check_expectation(request, plan)
        risk_reducing = plan.worst_after >= plan.worst_before

        legs = [_Leg.of(leg.offer, leg.stake_ccy) for leg in sorted(plan.legs, key=lambda leg: (-leg.stake_inr, leg.offer.selection))]
        tickets = {leg.selection: self._ticket(user_id, request.fixture_id, request.market, group, "hedge", leg, meta) for leg in legs}
        limits = await self._preflight(user_id, list(tickets.values()), total=plan.hedge_stake_inr, risk_reducing=risk_reducing)
        slip = limits.max_slippage_pct / _HUNDRED
        await self._emit(
            user_id, group,
            f"{plan.kind.replace('_', ' ').title()} hedge · {len(legs)} legs · ₹{plan.hedge_stake_inr} · worst case ₹{plan.worst_before} → ₹{plan.worst_after}",
        )

        current = dict(profits)
        traded: set[str] = set()
        status = "COMPLETE"
        floor_target = min(plan.target, ZERO)
        for index, leg in enumerate(legs):
            remaining = legs[index + 1 :]
            try:
                if traded:
                    stakes = hedge_after_fills(current, offers, anchor=plan.anchor, target=plan.target, exclude=frozenset(traded))
                    before = leg.stake_inr
                    leg.resize(stakes.get(leg.selection, ZERO))
                    if leg.stake_inr != before:
                        await self._emit(user_id, group, f"Leg {chr(65 + index)} {leg.selection}: re-sized ₹{before} → ₹{leg.stake_inr} after the fills before it", "warning")
                    for later in remaining:
                        later.resize(stakes.get(later.selection, ZERO))
                    if leg.stake_inr <= ZERO:
                        leg.status, leg.reason = "SKIPPED", "NOT_NEEDED"
                        continue
                # This outcome must not end below the target's floor (₹0 for a free bet) after every stake to come
                to_spend = leg.stake_inr + sum((later.stake_inr for later in remaining), ZERO)
                rupee_floor = (floor_target + to_spend - current[leg.selection]) / leg.value_inr()
                leg.floor = _floor(leg.offer, rupee_floor, leg.offer.raw_odds * (ONE - slip))
            except _SEQUENCE_ERRORS as exc:
                if not traded:
                    raise exc if isinstance(exc, CfoError) else CfoError("NO_HEDGE", str(exc), status_code=409) from exc
                leg.status, leg.reason, leg.message = "FAILED", "LEG_SIZING_FAILED", str(exc)
                status = "LEGGED"
                break
            ok = await self._fire(
                user_id, group, index, leg,
                replace(tickets[leg.selection], stake_inr=leg.stake_inr, stake_ccy=leg.stake_ccy()),
                risk_reducing=risk_reducing,
            )
            if not ok:
                status = "ABORTED" if not traded and leg.status == "FAILED" else "LEGGED"
                break
            assert leg.held is not None
            traded.add(leg.selection)
            current = {o: p - leg.held.stake_inr + (leg.held.stake_inr * leg.held.rupee_odds if o == leg.selection else ZERO) for o, p in current.items()}
        await self._abort_rest(user_id, group, "hedge", legs, request.fixture_id)
        held = [h for h in (leg.held for leg in legs) if h is not None]
        receipt = self._receipt(group, "hedge", legs, held, outcomes, status, base=profits)
        if status == "COMPLETE" and receipt.worst_case < plan.worst_after - PAISA * len(legs):
            receipt = receipt.model_copy(update={"status": "LEGGED", "message": "Hedged, but partial fills left it short of the plan: see the outcomes"})
        return receipt

    # -------------------------------------------------------------- shared steps
    async def _claim_group(self, group: uuid.UUID) -> None:
        if self.redis is None:
            raise RiskGuardViolation("RISK_SERVICES_UNAVAILABLE", "Execution needs Redis for idempotency; nothing was executed", status_code=503)
        key = f"{self.settings.CFO_IDEMPOTENCY_KEY_PREFIX}:group:{group}"
        try:
            claimed = await self.redis.set(key, "1", nx=True, ex=self.settings.CFO_IDEMPOTENCY_TTL_SECONDS)
        except (RedisError, OSError) as exc:
            raise RiskGuardViolation("RISK_SERVICES_UNAVAILABLE", "Execution needs Redis for idempotency; nothing was executed", status_code=503) from exc
        if not claimed:
            raise DuplicateExecutionError("DUPLICATE_REQUEST", "This order was already submitted (duplicate click)")

    def _live_legs(self, ctx: PricingContext, books: Sequence[Any], wanted: Sequence[tuple[str, str, Decimal]]) -> list[Offer]:
        """The requested legs at the requested prices, each confirmed against its book's live quote."""
        live = market_offers(books, ctx)[0]
        chosen: list[Offer] = []
        for selection, bookmaker_id, odds in wanted:
            current = max((o for o in live if o.provider == bookmaker_id and o.selection == selection), key=lambda o: o.raw_odds, default=None)
            if current is None:
                raise CfoError("PRICE_UNAVAILABLE", f"{bookmaker_id} has no live price for {selection}", status_code=409)
            if current.raw_odds < odds:
                raise CfoError(
                    "PRICE_MOVED",
                    f"{bookmaker_id} now offers {current.raw_odds} for {selection} (was {odds})",
                    status_code=409,
                    detail={"selection": selection, "bookmaker_id": bookmaker_id, "current_odds": str(current.raw_odds), "requested_odds": str(odds)},
                )
            if not ctx.routable(bookmaker_id):
                raise CfoError("NO_EXECUTION_VENUE", f"No execution venue routes {bookmaker_id}", status_code=422)
            chosen.append(replace(current, raw_odds=odds))  # trade at the price shown: the floor guards the rest
        return chosen

    @staticmethod
    def _check_expectation(request: HedgeExecuteRequest, plan: Any) -> None:
        if request.expected_legs is None:
            return
        planned = {leg.offer.selection: leg for leg in plan.legs}
        expected = {leg.selection: leg for leg in request.expected_legs}
        changed = set(planned) != set(expected)
        for selection, leg in planned.items():
            seen = expected.get(selection)
            if seen is None:
                continue
            if leg.offer.provider != seen.bookmaker_id or leg.offer.raw_odds < seen.odds:
                changed = True
            elif abs(leg.stake_inr - seen.stake_inr) > seen.stake_inr * _STAKE_DRIFT:
                changed = True
        if changed:
            raise CfoError(
                "HEDGE_CHANGED",
                "Prices moved since the hedge was shown; review the new one",
                status_code=409,
                detail={"legs": [{"selection": s, "bookmaker_id": l.offer.provider, "odds": str(l.offer.raw_odds), "stake_inr": str(l.stake_inr)} for s, l in planned.items()]},
            )

    def _ticket(self, user_id: uuid.UUID, fixture_id: str, market: str, group: uuid.UUID, strategy: str, leg: _Leg, meta: Any) -> OrderTicket:
        return OrderTicket(
            user_id=user_id,
            idempotency_key=leg_key(group, leg.selection),
            fixture_id=fixture_id,
            market=market,
            selection=leg.selection,
            bookmaker_id=leg.offer.provider,
            stake_inr=leg.stake_inr,
            odds=leg.offer.raw_odds,
            commence_time=meta.commence_time if meta is not None else None,
            strategy=strategy,
            group_id=group,
            currency=leg.offer.currency,
            stake_ccy=leg.stake_ccy(),
        )

    async def _preflight(self, user_id: uuid.UUID, tickets: Sequence[OrderTicket], *, total: Decimal, risk_reducing: bool) -> Any:
        """Every check every leg will face, before the first one fires. Returns the user's guard limits."""
        guard = self.executor.guard
        async with self.executor.session_factory() as session:
            account = await read_account(session, user_id)
            available = account.available_balance if account is not None else await opening_balance(session, user_id, self.settings)
            equity = account.equity if account is not None else available
            if total > available:
                raise CfoError("INSUFFICIENT_BALANCE", f"The legs need ₹{total}; ₹{available} is available", status_code=409)
            # Exposure is checked once for the whole order: each leg alone would pass, together they must too
            await guard.check(session, replace(tickets[0], stake_inr=total), account, risk_reducing=risk_reducing)
            limits = await load_limits(session, user_id)
        risk_limits = await load_risk_limits(self.redis, self.executor.session_factory, self.settings)
        if not risk_reducing:
            for ticket in tickets[1:]:
                await guard.velocity(ticket, limits, GuardReport())
            for ticket in tickets:
                self.executor._check_limits(ticket, risk_limits, equity=equity)
        elif risk_limits.halted:
            raise RiskGuardViolation("BLOCKED_BY_KILL_SWITCH", "Trading is halted by the emergency stop")
        return limits

    async def _fire(self, user_id: uuid.UUID, group: uuid.UUID, index: int, leg: _Leg, ticket: OrderTicket, *, risk_reducing: bool = False) -> bool:
        name = f"Leg {chr(65 + index)}"
        await self._emit(user_id, group, f"{name}: {leg.selection} @ {leg.offer.raw_odds} (min {leg.floor}) at {leg.offer.provider} for ₹{leg.stake_inr}")
        try:
            receipt = await self.executor.execute_leg(ticket, min_odds=leg.floor or leg.offer.raw_odds, risk_reducing=risk_reducing)
        except CfoError as exc:
            leg.status, leg.reason, leg.message = "FAILED", exc.reason, exc.message
            await self._emit(user_id, group, f"{name} failed ({exc.reason})" + ("; aborting the remaining legs" if index == 0 else "; position is legged"), "error")
            return False
        leg.receipt = receipt
        if receipt.status == "UNKNOWN":
            leg.status, leg.reason, leg.message = "UNCONFIRMED", "EXECUTION_UNKNOWN", receipt.message
            await self._emit(user_id, group, f"{name} unconfirmed: stopping here rather than hedge a bet that may not exist", "warning")
            return False
        matched = replace(leg.offer, raw_odds=receipt.odds)
        # A foreign leg pays from the exact stake that matched in its own currency, not the rounded cost
        paid_from = to_inr(receipt.stake_ccy, leg.offer.fx) if leg.foreign and receipt.stake_ccy is not None else receipt.stake_inr
        leg.held = HeldBet(leg.selection, receipt.stake_inr, paid_from * matched.rupee_odds() / receipt.stake_inr)
        leg.status = "PARTIAL" if receipt.partial_fill else "FILLED"
        leg.reason = "PARTIAL_FILL" if receipt.partial_fill else "FILLED"
        return True

    async def _abort_rest(self, user_id: uuid.UUID, group: uuid.UUID, strategy: str, legs: Sequence[_Leg], fixture_id: str) -> None:
        for leg in legs:
            if leg.status != "ABORTED":
                continue
            leg.reason = leg.reason or f"{strategy.upper()}_LEG_ABORTED"
            leg.message = leg.message or "Not fired: an earlier leg did not complete"
            await write_audit(
                self.executor.session_factory,
                audit_row(
                    AuditEvent.BLOCKED,
                    leg.reason,
                    user_id=user_id,
                    idempotency_key=leg_key(group, leg.selection),
                    fixture_id=fixture_id,
                    selection=leg.selection,
                    stake_inr=leg.stake_inr,
                    odds=leg.offer.raw_odds,
                    detail={"group_id": str(group), "strategy": strategy, "bookmaker_id": leg.offer.provider},
                ),
            )

    def _receipt(
        self,
        group: uuid.UUID,
        strategy: str,
        legs: Sequence[_Leg],
        held: Sequence[HeldBet],
        outcomes: Sequence[str],
        status: str,
        base: Mapping[str, Decimal] | None = None,
    ) -> MultiLegReceipt:
        profits = book_profits(list(held), outcomes)
        if base is not None:
            profits = {o: base[o] + profits[o] for o in outcomes}
        profits = {o: p.quantize(PAISA, rounding=ROUND_DOWN) for o, p in profits.items()}
        worst, best = min(profits.values()), max(profits.values())
        if status == "COMPLETE" and strategy == "arbitrage" and worst < ZERO:
            status = "LEGGED"  # every leg placed, but a later leg's partial fill left one outcome short
        messages = {
            "COMPLETE": "Every leg placed" + (" (partial fills re-sized the legs after them)" if any(l.status == "PARTIAL" for l in legs) else ""),
            "LEGGED": "Part of the plan is open: the book is not fully covered. The Active Portfolio shows the cover",
            "ABORTED": "Leg A did not go through, so nothing else was fired. No funds moved",
        }
        return MultiLegReceipt(
            group_id=group,
            strategy=strategy,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            message=messages[status],
            legs=[leg.as_receipt() for leg in legs],
            outcome_profits=profits,
            worst_case=worst,
            best_case=best,
            total_staked_inr=sum((h.stake_inr for h in held), ZERO),
            execution_mode=self.settings.CFO_EXECUTION_MODE,
        )

    async def _emit(self, user_id: uuid.UUID, group: uuid.UUID, message: str, level: str = "info") -> None:
        await self.feed.emit(user_id, "legging", message, level=level, ref=str(group))
