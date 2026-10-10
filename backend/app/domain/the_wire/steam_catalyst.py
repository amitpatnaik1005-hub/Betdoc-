"""Breaking news that moved the market (Group 78).

When an article naming a tracked fixture is ingested, the Wire records that fixture's de-vigged consensus
probabilities (Match Odds: HOME / DRAW / AWAY) at that moment. Each later news scan re-reads them. The article is
a **steam catalyst** when, within ``WIRE_CATALYST_WINDOW_SECONDS`` of publication:

1. its tactical impact is one of ``WIRE_CATALYST_IMPACTS`` (CRITICAL, HIGH);
2. a selection's consensus probability moved at least ``WIRE_CATALYST_MIN_PROB_SHIFT`` (3.5 points);
3. the move runs the news's way: bad news about the named side shortens the opponent or lengthens the side
   (its probability falls), good news the reverse.

The latency is measured from publication to the scan that saw the move; its resolution is the scan interval
(``WIRE_NEWS_SCAN_MINUTES``), so a recorded latency is an upper bound. The retail books' lag after the sharp move
(the brief's 45-180 seconds) is not measured here: the consensus is the books' blended price.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

OPPOSITE = {"HOME": "AWAY", "AWAY": "HOME"}


@dataclass(frozen=True, slots=True)
class Catalyst:
    selection: str
    probability_before: float
    probability_after: float
    shift: float  # after - before
    latency_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {"selection": self.selection, "probability_before": self.probability_before, "probability_after": self.probability_after,
                "shift": self.shift, "latency_seconds": self.latency_seconds}


def evaluate(*, published_at: datetime, observed_at: datetime, impact: str, sentiment: float, side: str | None,
             before: Mapping[str, float], after: Mapping[str, float], window_seconds: float, min_shift: float, impacts: list[str] | tuple[str, ...]) -> Catalyst | None:
    latency = (observed_at - published_at).total_seconds()
    if impact not in impacts or side not in OPPOSITE or sentiment == 0 or not 0 <= latency <= window_seconds:
        return None
    sign = 1.0 if sentiment > 0 else -1.0
    best: Catalyst | None = None
    for selection, direction in ((side, sign), (OPPOSITE[side], -sign)):
        if selection not in before or selection not in after:
            continue
        shift = after[selection] - before[selection]
        if shift * direction >= min_shift and (best is None or abs(shift) > abs(best.shift)):
            best = Catalyst(selection, round(before[selection], 4), round(after[selection], 4), round(shift, 4), round(latency, 1))
    return best
