"""Parameter sweeps and walk-forward validation.

* ``kelly_grid``: the Kelly multipliers a sweep tries, evenly spaced (10 from 0.10 to 0.50 by
  default), the same for every bot of the run.
* ``best_row``: the sweep's winner, by Sharpe ratio (a run without a Sharpe, no trades or a flat
  curve, never wins over one with); ties go to the higher ROI, then the smaller multiplier.
* ``LockedParameters``: what the in-sample optimisation chose, frozen and fingerprinted. The
  out-of-sample run is built from it and nothing else; there is no sweep on the test window.
* ``overfit_verdict``: in-sample against out-of-sample. A strategy that only works on the data it
  was tuned on is overfit; one whose Sharpe holds up (at least half of it) is robust.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

MIN_TRADES_FOR_VERDICT = 10
RETENTION_FLOOR = 0.5


def kelly_grid(low: Decimal = Decimal("0.1"), high: Decimal = Decimal("0.5"), steps: int = 10) -> list[Decimal]:
    if steps < 1:
        raise ValueError("at least one step")
    if not Decimal(0) < low <= high <= Decimal(1):
        raise ValueError("Kelly multipliers in (0, 1], low <= high")
    if steps == 1:
        return [low.normalize()]
    step = (high - low) / (steps - 1)
    grid = [(low + step * i).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN).normalize() for i in range(steps - 1)] + [high.normalize()]
    return sorted(set(grid))


def _score(row: Mapping[str, Any]) -> tuple[int, float, float, float]:
    sharpe, roi = row.get("sharpe"), row.get("roi_pct")
    return (int(sharpe is not None), float(sharpe) if sharpe is not None else float("-inf"), float(roi) if roi is not None else float("-inf"), -float(row["kelly"]))


def best_row(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not rows:
        raise ValueError("an empty sweep has no best row")
    return max(rows, key=_score)


@dataclass(frozen=True, slots=True)
class LockedParameters:
    kelly_multiplier: Decimal | None  # None: every bot keeps its own
    chosen_by: str  # sweep:sharpe | bot_settings
    window: str  # where it was chosen: in_sample | full

    def fingerprint(self, bots: Sequence[Mapping[str, Any]]) -> str:
        raw = json.dumps({"kelly": str(self.kelly_multiplier), "by": self.chosen_by, "window": self.window, "bots": list(bots)}, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def as_dict(self, bots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        return {"kelly_multiplier": None if self.kelly_multiplier is None else str(self.kelly_multiplier), "chosen_by": self.chosen_by, "window": self.window, "fingerprint": self.fingerprint(bots)}


def overfit_verdict(in_sample: Mapping[str, Any], out_of_sample: Mapping[str, Any]) -> dict[str, Any]:
    is_sharpe, oos_sharpe = in_sample.get("sharpe"), out_of_sample.get("sharpe")
    is_roi, oos_roi = in_sample.get("roi_pct"), out_of_sample.get("roi_pct")
    detail = {"in_sample_sharpe": is_sharpe, "out_of_sample_sharpe": oos_sharpe, "in_sample_roi_pct": is_roi, "out_of_sample_roi_pct": oos_roi,
              "in_sample_trades": in_sample.get("trades", 0), "out_of_sample_trades": out_of_sample.get("trades", 0)}
    if in_sample.get("trades", 0) < MIN_TRADES_FOR_VERDICT or out_of_sample.get("trades", 0) < MIN_TRADES_FOR_VERDICT:
        return {"verdict": "INSUFFICIENT_DATA", "sharpe_retention": None, "reason": f"fewer than {MIN_TRADES_FOR_VERDICT} graded trades on one side of the split", **detail}
    if is_sharpe is None or is_sharpe <= 0:
        verdict, reason = ("NO_EDGE", "no in-sample edge to carry forward")
        return {"verdict": verdict, "sharpe_retention": None, "reason": reason, **detail}
    retention = None if oos_sharpe is None else round(oos_sharpe / is_sharpe, 4)
    if oos_sharpe is None or oos_sharpe <= 0 or (oos_roi is not None and oos_roi < 0):
        return {"verdict": "OVERFIT", "sharpe_retention": retention, "reason": "the edge found in-sample did not survive out-of-sample", **detail}
    if retention is not None and retention < RETENTION_FLOOR:
        return {"verdict": "DEGRADED", "sharpe_retention": retention, "reason": f"out-of-sample Sharpe kept under {RETENTION_FLOOR:.0%} of in-sample", **detail}
    return {"verdict": "ROBUST", "sharpe_retention": retention, "reason": "the locked parameters held up on unseen data", **detail}
