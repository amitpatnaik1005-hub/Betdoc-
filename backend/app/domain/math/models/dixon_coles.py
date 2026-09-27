import numpy as np

from app.domain.math.models.poisson import PoissonModel

DEFAULT_RHO = -0.15


class DixonColesModel(PoissonModel):
    name = "dixon_coles"

    def __init__(self, rho: float = DEFAULT_RHO) -> None:
        self.rho = rho

    def _build_matrix(self, home_xg: float, away_xg: float) -> np.ndarray:
        base = super()._build_matrix(home_xg, away_xg)
        lam, mu, rho = home_xg, away_xg, self.rho

        tau = np.ones_like(base)
        tau[0, 0] = 1.0 - lam * mu * rho
        tau[0, 1] = 1.0 + lam * rho
        tau[1, 0] = 1.0 + mu * rho
        tau[1, 1] = 1.0 - rho

        adjusted = base * np.clip(tau, 0.0, None)
        total = float(adjusted.sum())
        if total <= 0.0:
            return base
        return adjusted / total  # re-normalize to exactly 1.0
