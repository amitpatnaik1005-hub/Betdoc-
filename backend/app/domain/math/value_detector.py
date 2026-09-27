import logging
import math
import uuid

from app.domain.math.utilities import (
    calculate_overround,
    expected_value,
    kelly_criterion,
    remove_vig_proportional,
)
from app.schemas.math import PredictionResult, ValueBetFlag

logger = logging.getLogger("betdoc.panini")

SELECTIONS: tuple[str, str, str] = ("HOME", "DRAW", "AWAY")
REQUIRED_KEYS = frozenset(SELECTIONS)


class ValueDetector:
    def __init__(self, ev_threshold: float = 0.02, kelly_fraction: float = 0.25) -> None:
        self.ev_threshold = ev_threshold
        self.kelly_fraction = kelly_fraction

    def detect(
        self,
        prediction: PredictionResult,
        bookmaker_odds: dict[str, float] | None,
        match_id: str = "",
    ) -> list[ValueBetFlag]:
        if not bookmaker_odds or set(bookmaker_odds.keys()) != REQUIRED_KEYS:
            logger.info("Value detection skipped: market is not a strict 3-way HOME/DRAW/AWAY book")
            return []

        odds = [float(bookmaker_odds[s]) for s in SELECTIONS]
        if any(not math.isfinite(o) or o <= 1.0 for o in odds):
            logger.info("Value detection skipped: invalid odds %s", odds)
            return []

        overround = calculate_overround(odds)
        fair_market = remove_vig_proportional(odds)
        if overround < 0:
            logger.warning("Negative overround (%.4f): possible bad odds feed", overround)

        model_probs = {
            "HOME": prediction.home_win_prob,
            "DRAW": prediction.draw_prob,
            "AWAY": prediction.away_win_prob,
        }

        flags: list[ValueBetFlag] = []
        for selection, price, fair_p in zip(SELECTIONS, odds, fair_market):
            true_p = float(model_probs[selection])
            ev = expected_value(true_p, price)
            if ev > self.ev_threshold:
                logger.info(
                    "Value %s (match=%s): model=%.4f fair_market=%.4f odds=%.2f ev=%.4f",
                    selection, match_id or "-", true_p, fair_p, price, ev,
                )
                flags.append(
                    ValueBetFlag(
                        selection=selection,
                        true_prob=float(true_p),
                        bookmaker_odds=float(price),
                        expected_value=float(ev),
                        kelly_stake_fraction=float(kelly_criterion(true_p, price, self.kelly_fraction)),
                        leg_id=str(uuid.uuid4()),
                        match_id=str(match_id),
                        market_type="MATCH_WINNER_1X2",
                    )
                )

        return sorted(flags, key=lambda f: f.expected_value, reverse=True)
