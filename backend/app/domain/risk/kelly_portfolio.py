from app.domain.risk.common import safe_float


def portfolio_kelly(edges: list[float], odds_list: list[float], max_total: float = 1.0) -> list[float]:
    """
    Independent Kelly per bet: f = edge / (odds - 1), where edge = p * odds - 1.
    Negative or zero edges get 0. If the combined allocation exceeds max_total,
    all fractions are scaled down proportionally.
    """
    if not edges:
        return []
    fractions = [0.0] * len(edges)
    for i, (edge, odds) in enumerate(zip(edges, odds_list)):
        e = safe_float(edge)
        b = safe_float(odds) - 1.0
        if e <= 0.0 or b <= 0.0:
            continue
        fractions[i] = safe_float(min(1.0, e / b))

    total = sum(fractions)
    cap = safe_float(max_total)
    if cap > 0.0 and total > cap:
        scale = cap / total
        fractions = [safe_float(f * scale) for f in fractions]
    return fractions
