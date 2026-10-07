"""Loss Distribution Approach (compound Poisson-Lognormal) operational-risk quantile."""

import math
import numpy as np
from typing import Any
from app.domain.risk.common import safe_float, to_finite_array

_FAILSAFE = (ArithmeticError, ValueError, TypeError, MemoryError)


def lda_percentile(
    lambda_freq: float,
    lognorm_mu: float,
    lognorm_sigma: float,
    percentile: float = 0.99,
    n_sims: int = 10000,
    seed: int | None = None,
    max_total_events: int = 10_000_000,
    min_sims: int = 1,
) -> float:
    r"""Aggregate loss :math:`L = \sum_{k=1}^{N} S_k`, :math:`N \sim \mathrm{Poisson}(\lambda)`,
    :math:`S_k \sim \mathrm{Lognormal}(\mu, \sigma)`; returns :math:`F_L^{-1}(p)`.

    If :math:`\lambda\cdot n_{\text{sims}}` exceeds ``max_total_events``, the simulation count is scaled
    down to protect memory. Period totals use ``np.add.reduceat`` over one flat severity array;
    zero-event periods are handled explicitly (``reduceat`` would otherwise copy a neighbour).
    """
    try:
        lam, mu, sig, p = safe_float(lambda_freq), safe_float(lognorm_mu), safe_float(lognorm_sigma), safe_float(percentile)
        sims, budget = int(n_sims), int(max_total_events)
        if lam <= 0.0 or sig < 0.0 or not 0.0 < p < 1.0 or sims < 1 or budget < 1:
            return 0.0
        if lam * sims > budget:
            sims = max(int(budget // lam), int(min_sims), 1)
        rng = np.random.default_rng(seed)
        counts = rng.poisson(lam, size=sims)
        total_events = int(counts.sum())
        if total_events == 0:
            return 0.0
        severities = rng.lognormal(mean=mu, sigma=sig, size=total_events)
        totals = np.zeros(sims, dtype=np.float64)
        has_events = counts > 0
        starts = (np.cumsum(counts) - counts)[has_events]
        totals[has_events] = np.add.reduceat(severities, starts)
        q = float(np.quantile(totals, p))
        return q if math.isfinite(q) else 0.0
    except _FAILSAFE:
        return 0.0
