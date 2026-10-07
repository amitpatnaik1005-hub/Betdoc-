"""Pure NumPy LSTM regressor trained with mini-batch SGD and full BPTT."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import Field
from scipy.special import expit

from app.domain.math.models_v2.base import (
    FLOAT_TINY,
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    NumericalStabilityError,
    as_float_array,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["LSTMConfig", "LSTMModel"]

Params = list[FloatArray]  # [W (4H,F), U (4H,H), b (4H,), Wy (O,H), by (O,)]


class LSTMConfig(MathModelConfig):
    hidden_size: int = Field(default=32, ge=1)
    learning_rate: float = Field(default=1e-2, gt=0.0)
    n_epochs: int = Field(default=100, ge=1)
    batch_size: int = Field(default=32, ge=1)
    clip_norm: float = Field(default=1.0, gt=0.0)
    forget_bias: float = 1.0
    weight_decay: float = Field(default=0.0, ge=0.0)
    xavier_gain: float = Field(default=1.0, gt=0.0)
    orthogonal_gain: float = Field(default=1.0, gt=0.0)
    standardize: bool = True
    shuffle: bool = True
    tol: float = Field(default=1e-8, ge=0.0)
    patience: int = Field(default=10, ge=1)
    min_std: float = Field(default=1e-12, gt=0.0)
    rng_stream: int = Field(default=41, ge=0)


@dataclass(frozen=True, slots=True)
class _LSTMState:
    W: FloatArray
    U: FloatArray
    b: FloatArray
    Wy: FloatArray
    by: FloatArray
    x_mean: FloatArray
    x_std: FloatArray
    y_mean: FloatArray
    y_std: FloatArray
    n_features: int
    n_targets: int
    loss_history: FloatArray


class LSTMModel(BaseMathModel):
    r"""Long Short-Term Memory network with a linear read-out on :math:`h_T`.

    .. math:: i_t = \sigma(W_i x_t + U_i h_{t-1} + b_i),\quad f_t = \sigma(W_f x_t + U_f h_{t-1} + b_f),\quad
              o_t = \sigma(W_o x_t + U_o h_{t-1} + b_o)

    .. math:: \tilde c_t = \tanh(W_c x_t + U_c h_{t-1} + b_c),\quad c_t = f_t\odot c_{t-1} + i_t\odot\tilde c_t,\quad
              h_t = o_t\odot\tanh(c_t),\quad \hat y = W_y h_T + b_y

    Loss :math:`\mathcal L = \frac{1}{BO}\sum (\hat y - y)^2`. Gradients from BPTT are rescaled by
    :math:`\min(1, \tau/\lVert g\rVert_2)` (global-norm clipping). :math:`W` uses Xavier-uniform
    :math:`U(\pm g\sqrt{6/(F+H)})`, :math:`U` uses per-gate orthogonal (QR) initialisation and
    :math:`b_f = 1` protects early gradient flow. ``X``: ``(batch, time, features)``; ``y``: ``(batch, targets)``.
    """

    config: LSTMConfig

    def __init__(self, config: LSTMConfig | None = None) -> None:
        super().__init__(resolve_config(config, LSTMConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        seq = as_float_array(X, name="X", ndim=3)
        if y is None:
            raise ValueError("y is required for LSTM training.")
        target = as_float_array(y, name="y", ndim=(1, 2))
        target = target.reshape(-1, 1) if target.ndim == 1 else target
        if target.shape[0] != seq.shape[0]:
            raise ValueError("X and y must share the batch dimension.")
        n_batch, _, n_feat = seq.shape
        n_out = target.shape[1]
        x_mean, x_std = self._stats(seq.reshape(-1, n_feat))
        y_mean, y_std = self._stats(target)
        xs = (seq - x_mean) / x_std
        ys = (target - y_mean) / y_std

        rng = self._rng(cfg.rng_stream)
        params = self._initialise(rng, n_feat, n_out)
        history: list[float] = []
        best, stale = math.inf, 0
        for _ in range(cfg.n_epochs):
            order = rng.permutation(n_batch) if cfg.shuffle else np.arange(n_batch)
            for start in range(0, n_batch, cfg.batch_size):
                idx = order[start : start + cfg.batch_size]
                _, grads = self._loss_and_gradients(params, xs[idx], ys[idx])
                norm = math.sqrt(sum(float((g * g).sum()) for g in grads))
                scale = min(1.0, cfg.clip_norm / (norm + FLOAT_TINY))
                for p, g in zip(params, grads, strict=True):
                    p -= cfg.learning_rate * (g * scale + cfg.weight_decay * p)
            loss, _ = self._loss_and_gradients(params, xs, ys, compute_grad=False)
            if not math.isfinite(loss) or not all(np.all(np.isfinite(p)) for p in params):
                raise NumericalStabilityError("LSTM training diverged (non-finite loss or weights).")
            history.append(loss)
            if best - loss > cfg.tol:
                best, stale = loss, 0
            else:
                stale += 1
                if stale >= cfg.patience:
                    break

        W, U, b, Wy, by = params
        self._publish_state(
            _LSTMState(
                freeze(W), freeze(U), freeze(b), freeze(Wy), freeze(by),
                freeze(x_mean), freeze(x_std), freeze(y_mean), freeze(y_std),
                n_feat, n_out, freeze(np.asarray(history, dtype=np.float64)),
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _LSTMState = self._current_state()
        seq = as_float_array(X, name="X", ndim=3)
        if seq.shape[2] != state.n_features:
            raise ValueError(f"X must have {state.n_features} features on axis 2.")
        xs = (seq - state.x_mean) / state.x_std
        y_hat, _, _ = self._forward([state.W, state.U, state.b, state.Wy, state.by], xs)
        return sanitize(y_hat * state.y_std + state.y_mean)

    @property
    def loss_history(self) -> FloatArray:
        return self._current_state().loss_history

    def _stats(self, data: FloatArray) -> tuple[FloatArray, FloatArray]:
        if not self.config.standardize:
            return np.zeros(data.shape[1]), np.ones(data.shape[1])
        std = data.std(axis=0)
        return data.mean(axis=0), np.where(std > self.config.min_std, std, 1.0)

    def _initialise(self, rng: np.random.Generator, n_feat: int, n_out: int) -> Params:
        cfg, h = self.config, self.config.hidden_size
        limit_w = cfg.xavier_gain * math.sqrt(6.0 / (n_feat + h))
        W = rng.uniform(-limit_w, limit_w, size=(4 * h, n_feat))
        blocks = []
        for _ in range(4):
            q, r = np.linalg.qr(rng.standard_normal((h, h)))
            signs = np.sign(np.diag(r))
            signs[signs == 0.0] = 1.0
            blocks.append(cfg.orthogonal_gain * q * signs)
        U = np.vstack(blocks)
        b = np.zeros(4 * h)
        b[h : 2 * h] = cfg.forget_bias
        limit_y = cfg.xavier_gain * math.sqrt(6.0 / (h + n_out))
        Wy = rng.uniform(-limit_y, limit_y, size=(n_out, h))
        return [W, U, b, Wy, np.zeros(n_out)]

    @staticmethod
    def _forward(params: Params, xs: FloatArray) -> tuple[FloatArray, FloatArray, list[tuple[FloatArray, ...]]]:
        W, U, b, Wy, by = params
        n_batch, n_time, _ = xs.shape
        h_dim = U.shape[1]
        h = np.zeros((n_batch, h_dim))
        c = np.zeros((n_batch, h_dim))
        cache: list[tuple[FloatArray, ...]] = []
        for t in range(n_time):
            x = xs[:, t, :]
            z = x @ W.T + h @ U.T + b
            i = expit(z[:, :h_dim])
            f = expit(z[:, h_dim : 2 * h_dim])
            o = expit(z[:, 2 * h_dim : 3 * h_dim])
            g = np.tanh(z[:, 3 * h_dim :])
            c_new = f * c + i * g
            tc = np.tanh(c_new)
            cache.append((x, h, c, i, f, o, g, tc))
            h, c = o * tc, c_new
        return h @ Wy.T + by, h, cache

    def _loss_and_gradients(
        self, params: Params, xs: FloatArray, ys: FloatArray, *, compute_grad: bool = True
    ) -> tuple[float, Params]:
        y_hat, h_last, cache = self._forward(params, xs)
        residual = y_hat - ys
        loss = float(np.mean(residual**2))
        if not compute_grad:
            return loss, []
        W, U, _, Wy, _ = params
        h_dim = U.shape[1]
        d_out = 2.0 * residual / residual.size
        dW, dU, db = np.zeros_like(W), np.zeros_like(U), np.zeros(4 * h_dim)
        dWy, dby = d_out.T @ h_last, d_out.sum(axis=0)
        dh = d_out @ Wy
        dc_next = np.zeros_like(dh)
        for x, h_prev, c_prev, i, f, o, g, tc in reversed(cache):
            do = dh * tc
            dc = dc_next + dh * o * (1.0 - tc**2)
            di, dg, df = dc * g, dc * i, dc * c_prev
            dc_next = dc * f
            dz = np.hstack([di * i * (1.0 - i), df * f * (1.0 - f), do * o * (1.0 - o), dg * (1.0 - g**2)])
            dW += dz.T @ x
            dU += dz.T @ h_prev
            db += dz.sum(axis=0)
            dh = dz @ U
        return loss, [dW, dU, db, dWy, dby]
