from __future__ import annotations

from typing import Final, assert_never

from betdoc.domain.intelligence.advisor_models import RejectionReason
from betdoc.domain.risk.models import RiskDecision, RiskVerdict

_RUPEE_SIGN: Final[str] = "\u20b9"
_PAISE_PER_RUPEE: Final[int] = 100


class RiskExplanationFormatter:
    __slots__ = ()

    def format_decision(self, decision: RiskDecision) -> str:
        approved = self._format_inr(decision.approved_stake_paise)
        requested = self._format_inr(decision.requested_stake_paise)

        if decision.verdict is RiskVerdict.APPROVED:
            return f"Stake of {approved} approved in full. Favourable EV detected."

        reason = decision.binding_constraint
        if reason is None:
            return f"Stake of {requested} is not actionable. {decision.detail}"

        rejected = decision.verdict is RiskVerdict.REJECTED
        constraints = decision.constraints

        match reason:
            case RejectionReason.DAILY_LOSS_LIMIT_REACHED:
                budget = self._format_inr(constraints.daily_loss_headroom_paise)
                if rejected:
                    return (
                        f"Requested stake of {requested} was rejected. Your remaining "
                        f"daily loss budget is {budget}, and our policy restricts a "
                        "single bet to a maximum utilisation of that budget. Suggested "
                        "action: stand down until the budget resets at 00:00 UTC."
                    )
                return (
                    f"Requested stake of {requested} was reduced to {approved}. Your "
                    f"remaining daily loss budget is {budget}, and our policy restricts "
                    "a single bet to a maximum utilisation of that budget. Suggested "
                    "action: accept the reduced stake."
                )

            case RejectionReason.OVEREXPOSURE_ON_SPORT:
                headroom = self._format_inr(
                    min(
                        constraints.sport_exposure_headroom_paise,
                        constraints.settled_liquidity_paise,
                    )
                )
                return (
                    f"Stake of {requested} rejected or reduced. Your current unsettled "
                    f"exposure leaves only {headroom} in permitted liquidity. This "
                    f"stake exceeds the cap. Suggested alternative: {approved}."
                )

            case RejectionReason.EXCEEDS_SINGLE_STAKE_CAP:
                cap = self._format_inr(constraints.single_stake_cap_paise)
                if rejected:
                    return (
                        f"Requested stake of {requested} exceeds the hard per-bet cap "
                        f"of {cap}, which leaves no viable stake. Stake rejected."
                    )
                return (
                    f"Requested stake exceeds the hard per-bet cap of {cap}. "
                    f"Stake reduced to {approved}."
                )

            case RejectionReason.NEGATIVE_EV:
                ev_percentage = decision.expected_value_bps / 100
                return (
                    f"Rejected: Expected Value is {ev_percentage:.2f}%. Taking this bet "
                    "mathematically guarantees a loss over time. Minimum required EV is "
                    "strictly enforced."
                )

            case RejectionReason.EXTREME_NEWS_VOLATILITY:
                return f"Rejected: {decision.detail}."

        assert_never(reason)

    @staticmethod
    def _format_inr(paise: int) -> str:
        sign = "-" if paise < 0 else ""
        rupees, remainder = divmod(abs(paise), _PAISE_PER_RUPEE)
        digits = str(rupees)

        if len(digits) <= 3:
            grouped = digits
        else:
            head, tail = digits[:-3], digits[-3:]
            pairs: list[str] = []
            while len(head) > 2:
                pairs.append(head[-2:])
                head = head[:-2]
            if head:
                pairs.append(head)
            grouped = f"{','.join(reversed(pairs))},{tail}"

        return f"{sign}{_RUPEE_SIGN}{grouped}.{remainder:02d}"
