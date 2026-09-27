import logging
import math

signals_log = logging.getLogger("betdoc.signals")

POWER_K_LOWER = 0.001
POWER_K_UPPER = 10.0


def _implied(odds_list: list[float]) -> list[float] | None:
    """Implied probabilities, or None if any price is invalid (<= 1.0 or non-finite)."""
    probs: list[float] = []
    for o in odds_list:
        if o is None or not math.isfinite(o) or o <= 1.0:
            return None
        probs.append(1.0 / o)
    return probs


def remove_vig_multiplicative(odds_list: list[float]) -> list[float]:
    """Proportional normalization. Returns fair decimal odds."""
    probs = _implied(odds_list)
    if not probs:
        return list(odds_list)
    total = sum(probs)
    if total <= 0.0:
        return list(odds_list)
    return [total / p for p in probs]  # 1 / (p / total)


def remove_vig_power_method(
    odds_list: list[float], tolerance: float = 1e-6, max_iter: int = 100
) -> list[float]:
    """
    Solve sum(p_i ** k) = 1 for k by bisection on [0.001, 10.0]. Returns fair decimal odds.
    f(k) = sum(p_i ** k) - 1 is strictly decreasing for p_i in (0, 1).
    """
    probs = _implied(odds_list)
    if not probs or len(probs) < 2:
        return list(odds_list)

    raw_sum = sum(probs)
    if raw_sum <= 1.0:
        # CIRCUIT BREAKER: incomplete or inverted (arb) market. No vig to remove.
        signals_log.debug("Power method aborted: implied sum %.6f <= 1.0", raw_sum)
        return list(odds_list)

    tol = tolerance if tolerance > 0 else 1e-6
    iterations = max(1, int(max_iter))

    def f(k: float) -> float:
        return sum(p ** k for p in probs) - 1.0

    lo, hi = POWER_K_LOWER, POWER_K_UPPER
    if f(lo) < 0.0 or f(hi) > 0.0:
        # Root not bracketed (e.g. extreme favourite): fall back safely.
        signals_log.debug("Power method root not bracketed; using multiplicative fallback")
        return remove_vig_multiplicative(odds_list)

    mid = (lo + hi) / 2.0
    for _ in range(iterations):  # bounded: can never loop forever
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if abs(fm) < tol or (hi - lo) / 2.0 < tol:
            break
        if fm > 0.0:
            lo = mid
        else:
            hi = mid

    fair = [p ** mid for p in probs]
    total = sum(fair)
    if total <= 0.0 or not math.isfinite(total):
        return remove_vig_multiplicative(odds_list)
    return [total / x for x in fair]  # renormalize residual so probs sum to exactly 1
