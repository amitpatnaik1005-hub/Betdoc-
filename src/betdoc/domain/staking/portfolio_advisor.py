from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext
from threading import RLock

from betdoc.domain.math.errors import (
    DomainMathError,
    NegativeExpectedValueError,
)
from betdoc.domain.math.money import MAX_PAISE, from_paise, to_paise


def _finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainMathError(
            "Expected a real number", field=label, value=value
        )
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise DomainMathError("Invalid numeric input", field=label) from exc
    if not math.isfinite(result):
        raise DomainMathError(
            "Expected a finite number", field=label, value=value
        )
    return result


def _money(value: Decimal, label: str) -> int:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise DomainMathError(
            "Expected finite Decimal INR", field=label, value=value
        )
    try:
        paise = to_paise(value, label=label)
    except (ValueError, ArithmeticError) as exc:
        raise DomainMathError(
            "Invalid monetary amount", field=label, value=value
        ) from exc

    with localcontext() as context:
        context.prec = 50
        if from_paise(paise) != value:
            raise DomainMathError(
                "Money must have whole-paise precision",
                field=label,
                value=value,
            )
    return paise


def _inr(paise: int) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        amount = from_paise(abs(paise))
        return -amount if paise < 0 else amount


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_DOWN))


@dataclass(frozen=True)
class CandidateBet:
    bet_id: str
    description: str
    decimal_odds: float
    model_probability: float
    edge_percentage: float


@dataclass(frozen=True)
class AllocatedBet:
    candidate: CandidateBet
    recommended_stake: Decimal
    expected_profit: Decimal
    fraction_of_bankroll: float


@dataclass(frozen=True)
class SessionPortfolio:
    total_session_bankroll: Decimal
    total_allocated_stake: Decimal
    stop_loss_threshold: Decimal
    expected_session_roi: float
    allocations: list[AllocatedBet]
    is_session_feasible: bool
    abort_reason: str | None = None


@dataclass(frozen=True)
class _ScoredCandidate:
    candidate: CandidateBet
    fraction: Decimal
    ev_per_unit: Decimal
    growth_score: Decimal


class PortfolioSessionAdvisor:
    """Fractional-Kelly allocation with conservative session reservations.

    One instance represents one session. Generating a portfolio atomically
    reserves its stakes. Successful execution must use those reservations;
    failed, unplaced orders must call cancel_allocation. Settlements supply
    gross returned money, including returned stake.

    Drawdown is measured from peak settled equity. Every outstanding stake
    counts as a potential total loss, regardless of correlation.

    This lock protects a single process only. Distributed or restart-safe
    enforcement requires these transitions in the authoritative ledger
    transaction; constructing another instance does not preserve a session.

    Basket selection ranks individual expected log growth. It is not a
    covariance-aware global portfolio optimizer. Fewer than three valid bets
    are allowed rather than manufacturing additional exposure.

    expected_session_roi is a percentage of the initial session bankroll.
    """

    def __init__(
        self,
        fractional_kelly_multiplier: float = 0.25,
        max_bankroll_risk_per_session: float = 0.20,
        max_single_bet_allocation: float = 0.08,
    ) -> None:
        multiplier = _finite(
            fractional_kelly_multiplier, "fractional_kelly_multiplier"
        )
        risk = _finite(
            max_bankroll_risk_per_session, "max_bankroll_risk_per_session"
        )
        single_cap = _finite(
            max_single_bet_allocation, "max_single_bet_allocation"
        )

        if not 0.0 < multiplier <= 1.0:
            raise DomainMathError("Kelly multiplier must be in (0, 1]")
        if not 0.0 < risk < 1.0:
            raise DomainMathError("Session risk fraction must be in (0, 1)")
        if not 0.0 < single_cap < 1.0:
            raise DomainMathError("Single-bet allocation must be in (0, 1)")

        self.fractional_kelly_multiplier: float = multiplier
        self.max_bankroll_risk_per_session: float = risk
        self.max_single_bet_allocation: float = single_cap

        self._multiplier: Decimal = Decimal(str(multiplier))
        self._risk_fraction: Decimal = Decimal(str(risk))
        self._single_cap: Decimal = Decimal(str(single_cap))
        self._bankroll_paise: int | None = None
        self._equity_paise: int = 0
        self._peak_equity_paise: int = 0
        self._loss_limit_paise: int = 0
        self._reservations: dict[str, int] = {}
        self._settlements: dict[str, int] = {}
        self._halted: bool = False
        self._lock = RLock()

    def _drawdown_paise(self) -> int:
        return max(0, self._peak_equity_paise - self._equity_paise)

    def _reserved_paise(self) -> int:
        return sum(self._reservations.values())

    def _score(self, candidate: CandidateBet) -> _ScoredCandidate:
        if not isinstance(candidate.bet_id, str) or not candidate.bet_id.strip():
            raise DomainMathError("Candidate ID must be non-empty")
        if not isinstance(candidate.description, str):
            raise DomainMathError(
                "Candidate description must be a string",
                bet_id=candidate.bet_id,
            )

        odds_value = _finite(candidate.decimal_odds, "decimal_odds")
        probability_value = _finite(
            candidate.model_probability, "model_probability"
        )
        _finite(candidate.edge_percentage, "edge_percentage")

        if not 1.0 < odds_value <= 1_000_000.0:
            raise DomainMathError(
                "Decimal odds must be in (1, 1000000]",
                bet_id=candidate.bet_id,
                odds=odds_value,
            )
        if not 0.0 <= probability_value <= 1.0:
            raise DomainMathError(
                "Model probability must be in [0, 1]",
                bet_id=candidate.bet_id,
                probability=probability_value,
            )

        with localcontext() as context:
            context.prec = 50
            odds = Decimal(str(odds_value))
            probability = Decimal(str(probability_value))
            loss_probability = Decimal("1") - probability
            net_odds = odds - Decimal("1")
            ev_per_unit = net_odds * probability - loss_probability

            # The supplied edge field is metadata, never the sizing authority.
            if ev_per_unit <= 0:
                raise NegativeExpectedValueError(
                    "Candidate has non-positive expected value",
                    fair_probability=probability_value,
                    offered_odds=odds_value,
                    ev_per_unit=ev_per_unit,
                )

            fraction = min(
                self._single_cap,
                self._multiplier * ev_per_unit / net_odds,
            )
            if fraction <= 0:
                raise NegativeExpectedValueError(
                    "Candidate has no positive Kelly allocation",
                    fair_probability=probability_value,
                    offered_odds=odds_value,
                    ev_per_unit=ev_per_unit,
                )

            growth_score = (
                probability * (Decimal("1") + net_odds * fraction).ln()
                + loss_probability * (Decimal("1") - fraction).ln()
            )
            return _ScoredCandidate(
                candidate=candidate,
                fraction=fraction,
                ev_per_unit=ev_per_unit,
                growth_score=growth_score,
            )

    def _infeasible(self, bankroll: int, reason: str) -> SessionPortfolio:
        return SessionPortfolio(
            total_session_bankroll=_inr(bankroll),
            total_allocated_stake=_inr(0),
            stop_loss_threshold=_inr(self._loss_limit_paise),
            expected_session_roi=0.0,
            allocations=[],
            is_session_feasible=False,
            abort_reason=reason,
        )

    def generate_session_portfolio(
        self,
        session_bankroll: Decimal,
        candidates: list[CandidateBet],
    ) -> SessionPortfolio:
        bankroll = _money(session_bankroll, "session_bankroll")

        with self._lock, localcontext() as context:
            context.prec = 50

            if self._bankroll_paise is not None:
                if bankroll != self._bankroll_paise:
                    raise DomainMathError(
                        "An existing session cannot change its initial bankroll",
                        expected_paise=self._bankroll_paise,
                        received_paise=bankroll,
                    )
            elif bankroll > 0:
                self._bankroll_paise = bankroll
                self._equity_paise = bankroll
                self._peak_equity_paise = bankroll
                self._loss_limit_paise = _floor(
                    Decimal(bankroll) * self._risk_fraction
                )

            if bankroll == 0:
                return self._infeasible(bankroll, "Session bankroll is zero.")
            if self._loss_limit_paise == 0:
                return self._infeasible(
                    bankroll, "Session risk budget is less than one paise."
                )

            drawdown = self._drawdown_paise()
            if self._halted or drawdown >= self._loss_limit_paise:
                self._halted = True
                return self._infeasible(
                    bankroll, "The session stop-loss has been reached."
                )

            reserved = self._reserved_paise()
            remaining_risk = self._loss_limit_paise - drawdown - reserved
            available_cash = self._equity_paise - reserved
            budget = min(remaining_risk, available_cash)

            if budget <= 0:
                return self._infeasible(
                    bankroll,
                    "No unreserved cash or session risk capacity remains.",
                )

            seen: set[str] = set()
            scored: list[_ScoredCandidate] = []

            for candidate in candidates:
                if (
                    not isinstance(candidate.bet_id, str)
                    or not candidate.bet_id.strip()
                ):
                    raise DomainMathError("Candidate ID must be non-empty")
                if candidate.bet_id in seen:
                    raise DomainMathError(
                        "Duplicate candidate ID", bet_id=candidate.bet_id
                    )
                seen.add(candidate.bet_id)

                if (
                    candidate.bet_id in self._reservations
                    or candidate.bet_id in self._settlements
                ):
                    continue

                try:
                    score = self._score(candidate)
                except NegativeExpectedValueError:
                    continue

                if _floor(Decimal(bankroll) * score.fraction) >= 1:
                    scored.append(score)

            scored.sort(
                key=lambda item: (-item.growth_score, item.candidate.bet_id)
            )
            selected = scored[:5]

            if not selected:
                return self._infeasible(
                    bankroll,
                    "No unused positive-EV candidates have a whole-paise stake.",
                )

            desired: list[tuple[_ScoredCandidate, Decimal]] = [
                (item, Decimal(bankroll) * item.fraction)
                for item in selected
            ]
            total_desired = sum(
                (amount for _, amount in desired), Decimal("0")
            )
            scale = min(Decimal("1"), Decimal(budget) / total_desired)

            allocations: list[AllocatedBet] = []
            new_reservations: dict[str, int] = {}
            total_stake = 0
            total_expected_profit = 0

            for item, desired_stake in desired:
                stake = min(
                    _floor(desired_stake * scale),
                    _floor(Decimal(bankroll) * self._single_cap),
                    budget - total_stake,
                )
                if stake <= 0:
                    continue

                expected_profit = _floor(
                    Decimal(stake) * item.ev_per_unit
                )
                allocations.append(
                    AllocatedBet(
                        candidate=item.candidate,
                        recommended_stake=_inr(stake),
                        expected_profit=_inr(expected_profit),
                        fraction_of_bankroll=float(
                            Decimal(stake) / Decimal(bankroll)
                        ),
                    )
                )
                new_reservations[item.candidate.bet_id] = stake
                total_stake += stake
                total_expected_profit += expected_profit

            if not allocations:
                return self._infeasible(
                    bankroll,
                    "Proportional sizing leaves no whole-paise allocations.",
                )

            if total_stake + reserved + drawdown > self._loss_limit_paise:
                raise DomainMathError(
                    "Allocation would violate the session loss limit",
                    proposed_paise=total_stake,
                    reserved_paise=reserved,
                    drawdown_paise=drawdown,
                    limit_paise=self._loss_limit_paise,
                )

            portfolio = SessionPortfolio(
                total_session_bankroll=_inr(bankroll),
                total_allocated_stake=_inr(total_stake),
                stop_loss_threshold=_inr(self._loss_limit_paise),
                expected_session_roi=float(
                    Decimal(total_expected_profit)
                    * Decimal("100")
                    / Decimal(bankroll)
                ),
                allocations=allocations,
                is_session_feasible=True,
            )
            self._reservations.update(new_reservations)
            return portfolio

    def cancel_allocation(self, bet_id: str) -> None:
        """Release a reservation only after confirming no bet was placed."""
        with self._lock:
            if bet_id in self._settlements:
                raise DomainMathError(
                    "Cannot cancel a settled allocation", bet_id=bet_id
                )
            if bet_id not in self._reservations:
                raise DomainMathError(
                    "No reservation exists for this bet", bet_id=bet_id
                )
            del self._reservations[bet_id]

    def record_settlement(
        self, bet_id: str, returned_amount: Decimal
    ) -> None:
        """Apply a final gross payout; repeated identical settlements are safe."""
        returned = _money(returned_amount, "returned_amount")

        with self._lock:
            previous = self._settlements.get(bet_id)
            if previous is not None:
                if previous != returned:
                    raise DomainMathError(
                        "Conflicting repeated settlement",
                        bet_id=bet_id,
                        previous_return_paise=previous,
                        received_return_paise=returned,
                    )
                return

            stake = self._reservations.get(bet_id)
            if stake is None:
                raise DomainMathError(
                    "Cannot settle an unreserved bet", bet_id=bet_id
                )

            new_equity = self._equity_paise + returned - stake
            if not 0 <= new_equity <= MAX_PAISE:
                raise DomainMathError(
                    "Settlement would exceed supported equity bounds",
                    bet_id=bet_id,
                    resulting_equity_paise=new_equity,
                )

            del self._reservations[bet_id]
            self._settlements[bet_id] = returned
            self._equity_paise = new_equity
            self._peak_equity_paise = max(
                self._peak_equity_paise, new_equity
            )

            if self._drawdown_paise() >= self._loss_limit_paise:
                self._halted = True

    @property
    def session_drawdown(self) -> Decimal:
        with self._lock:
            return _inr(self._drawdown_paise())

    @property
    def outstanding_exposure(self) -> Decimal:
        with self._lock:
            return _inr(self._reserved_paise())

    @property
    def is_session_stopped(self) -> bool:
        with self._lock:
            return self._halted
