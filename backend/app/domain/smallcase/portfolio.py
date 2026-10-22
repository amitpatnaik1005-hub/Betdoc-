"""A smallcase's weights and its risk (Group 107, point b.8). Developed for Amit Ashok Kumar Patnaik.

* **Sizing** is KUMBHA's single policy (``app.domain.cfo.growth_math.size``), leg by leg: the independent full-Kelly
  fraction of each single (``app.domain.risk.kelly_portfolio.portfolio_kelly``: edge / (odds - 1)), times the Kelly
  multiplier, the skill and long-shot factors, under the per-bet ceiling and the drawdown damper. The basket as a
  whole stays under ``SMALLCASE_MAX_BASKET_FRACTION`` of bankroll: past it every leg is scaled down alike. Without a
  bankroll the weights still stand and no rupee figure is shown.
* **Risk** is measured on the basket as sized: the expected return (exact), and from a seeded Monte Carlo over the
  legs (independent: one per fixture) the chance of a profit, the 95% historical VaR and CVaR
  (``app.domain.risk.var``, ``app.domain.risk.cvar``), the stakes' concentration (HHI, ``app.domain.risk.concentration``),
  the equal-risk-contribution weights for comparison (``app.domain.risk.parity``) and each leg's share of the 95% VaR
  (``app.domain.risk.component_var``'s Euler components, as shares summing to one: the components themselves are of the
  normal approximation, which overstates a basket whose worst case is losing its stake). A single's return per rupee
  is ``odds - 1`` with chance ``p``, else ``-1``: its variance is ``p (1 - p) odds^2``.
* **The parlay alternative**: the same legs as one multiple, its odds, chance and EV, for comparison.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np

from app.domain.cfo import growth_math as gm
from app.domain.risk.component_var import component_var
from app.domain.risk.concentration import calculate_hhi
from app.domain.risk.cvar import calculate_cvar
from app.domain.risk.kelly_portfolio import portfolio_kelly
from app.domain.risk.parity import risk_parity_weights
from app.domain.risk.var import calculate_historical_var
from app.domain.smallcase.baskets import Pick

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Holding:
    pick: Pick
    full_kelly: float
    fraction: float  # of bankroll, after KUMBHA's policy and the basket ceiling
    stake: Decimal  # zero without a bankroll

    def as_dict(self) -> dict[str, Any]:
        return {**self.pick.as_dict(), "full_kelly": round(self.full_kelly, 6), "fraction": round(self.fraction, 6), "stake_inr": str(self.stake)}


@dataclass(frozen=True, slots=True)
class Sizing:
    holdings: tuple[Holding, ...]
    regime: str
    damper: float
    scaled: float  # 1.0 unless the basket ceiling scaled every leg down
    halted: bool

    @property
    def fraction(self) -> float:
        return sum(h.fraction for h in self.holdings)

    @property
    def stake(self) -> Decimal:
        return sum((h.stake for h in self.holdings), ZERO)


def size(picks: Sequence[Pick], *, bankroll: Decimal | None, drawdown: float, policy: gm.SizingPolicy, step: Decimal, basket_cap: float,
         skill: float | None, latched: bool) -> Sizing:
    """Each leg by KUMBHA's policy, then the basket under its ceiling."""
    full = portfolio_kelly([p.ev for p in picks], [p.price for p in picks], max_total=1.0)
    sized = [gm.size(f, p.price, bankroll or Decimal(1), drawdown, policy, step, bss=skill, latched=latched) for f, p in zip(full, picks, strict=True)]
    fractions = [s.fraction for s in sized]
    total = sum(fractions)
    scale = basket_cap / total if total > basket_cap else 1.0
    regime = gm.damper(drawdown, policy, latched=latched)
    holdings = tuple(
        Holding(p, f, fr * scale, gm.round_stake(bankroll, fr * scale, step) if bankroll and fr > 0 else ZERO)
        for p, f, fr in zip(picks, full, fractions, strict=True)
    )
    return Sizing(holdings, regime.name, regime.multiplier, scale, regime.halted)


def _seed(picks: Sequence[Pick]) -> int:
    """The same basket always draws the same paths: the figures do not wobble between refreshes."""
    return int.from_bytes(hashlib.sha256("|".join(p.leg_id for p in picks).encode()).digest()[:8], "big")


def risk(holdings: Sequence[Holding], paths: int) -> dict[str, Any]:
    """The basket's return distribution per rupee staked across it, and its risk measures."""
    if not holdings:
        return {}
    weights = np.array([h.fraction for h in holdings], dtype=float)
    if weights.sum() <= 0:
        weights = np.ones(len(holdings))  # nothing sized (no edge left after the damper): describe it equally weighted
    w = weights / weights.sum()
    odds = np.array([h.pick.price for h in holdings])
    p = np.array([h.pick.fair_probability for h in holdings])
    expected = float(np.sum(w * (p * odds - 1.0)))
    rng = np.random.default_rng(_seed([h.pick for h in holdings]))
    wins = rng.random((paths, len(holdings))) < p
    returns = np.where(wins, w * (odds - 1.0), -w).sum(axis=1)
    variance = p * (1.0 - p) * odds**2
    cov = np.diag(variance)
    parity = risk_parity_weights(cov)
    contribution = component_var(w, cov, 0.95)  # Euler components of the normal VaR: shares of it are what compare across legs
    share = contribution / contribution.sum() if contribution.sum() > 0 else np.zeros(len(holdings))
    sd = math.sqrt(float(w @ cov @ w))
    return {
        "weights": [round(float(x), 6) for x in w],
        "expected_return": round(expected, 6),
        "p_profit": round(float(np.mean(returns > 0)), 4),
        "p_lose_all": round(float(np.prod(1.0 - p)), 6),
        "var_95": round(calculate_historical_var(returns.tolist(), 0.95), 6),
        "cvar_95": round(calculate_cvar(returns.tolist(), 0.95), 6),
        "std_return": round(sd, 6),
        "hhi": round(calculate_hhi(w), 6),
        "risk_parity_weights": [round(float(x), 6) for x in parity],
        "var_share": [round(float(x), 6) for x in share],
        "paths": paths,
        "basis": "per rupee staked across the basket; Monte Carlo over independent legs (one per fixture), seeded by the legs",
    }


def as_parlay(picks: Sequence[Pick]) -> dict[str, Any] | None:
    """The same legs as one multiple, for comparison (independent legs: the chance is the product)."""
    if len(picks) < 2:
        return None
    odds = math.prod(p.price for p in picks)
    chance = math.prod(p.fair_probability for p in picks)
    return {"odds": round(odds, 4), "win_probability": round(chance, 6), "ev": round(chance * odds - 1.0, 6)}
