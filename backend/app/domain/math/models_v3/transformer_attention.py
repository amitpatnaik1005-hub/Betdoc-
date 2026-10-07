"""Pure NumPy masked causal multi-head self-attention encoder with ridge read-out."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal, Self

import numpy as np
from pydantic import Field, model_validator

from app.domain.math.models_v2.base import (
    BaseMathModel,
    FloatArray,
    MathModelConfig,
    as_float_array,
    freeze,
    resolve_config,
    sanitize,
)

__all__ = ["TransformerAttentionConfig", "TransformerAttentionModel"]


class TransformerAttentionConfig(MathModelConfig):
    d_model: int = Field(default=32, ge=2)
    num_heads: int = Field(default=4, ge=1)
    positional_base: float = Field(default=10_000.0, gt=1.0)
    init_gain: float = Field(default=1.0, gt=0.0)
    use_residual: bool = True
    layer_norm_eps: float = Field(default=1e-6, gt=0.0)
    pooling: Literal["last", "mean"] = "last"
    ridge_alpha: float = Field(default=1e-3, gt=0.0)
    max_sequence_length: int = Field(default=4096, ge=1)
    rng_stream: int = Field(default=42, ge=0)

    @model_validator(mode="after")
    def _check_heads(self) -> Self:
        if self.d_model % self.num_heads != 0:
            raise ValueError("d_model must be divisible by num_heads.")
        return self


@dataclass(frozen=True, slots=True)
class _AttnState:
    W_in: FloatArray
    W_q: FloatArray
    W_k: FloatArray
    W_v: FloatArray
    W_o: FloatArray
    readout: FloatArray | None
    intercept: FloatArray | None
    n_features: int


class TransformerAttentionModel(BaseMathModel):
    r"""Causal multi-head self-attention block.

    .. math:: PE_{(p, 2i)} = \sin\!\left(p / b^{2i/d}\right),\qquad PE_{(p, 2i+1)} = \cos\!\left(p / b^{2i/d}\right),\qquad
              E = X W_{\mathrm{in}} + PE

    .. math:: \mathrm{head}_h = \operatorname{softmax}\!\left(\frac{Q_h K_h^\top}{\sqrt{d_k}} + M\right) V_h,\qquad
              M_{ij} = \begin{cases} 0 & j \le i \\ -\infty & j > i \end{cases}

    .. math:: Z = \mathrm{LayerNorm}\bigl(E + [\mathrm{head}_1,\dots,\mathrm{head}_H] W_O\bigr)

    Projections are Xavier-initialised from the seeded generator. If ``y`` is supplied,
    a closed-form ridge read-out :math:`\beta = (\Phi^\top\Phi + \lambda I)^{-1}\Phi^\top y` is fitted
    on pooled encodings. ``predict`` returns ``(batch, targets)``; ``encode`` returns ``(batch, time, d_model)``.
    """

    config: TransformerAttentionConfig

    def __init__(self, config: TransformerAttentionConfig | None = None) -> None:
        super().__init__(resolve_config(config, TransformerAttentionConfig))

    def fit(self, X: FloatArray, y: FloatArray | None = None, **kwargs: Any) -> None:
        cfg = self.config
        seq = self._as_sequences(X, n_features=None)
        n_feat, d = seq.shape[2], cfg.d_model
        rng = self._rng(cfg.rng_stream)

        def xavier(fan_in: int, fan_out: int) -> FloatArray:
            limit = cfg.init_gain * math.sqrt(6.0 / (fan_in + fan_out))
            return rng.uniform(-limit, limit, size=(fan_in, fan_out))

        weights = [xavier(n_feat, d)] + [xavier(d, d) for _ in range(4)]
        readout: FloatArray | None = None
        intercept: FloatArray | None = None
        if y is not None:
            target = as_float_array(y, name="y", ndim=(1, 2))
            target = target.reshape(-1, 1) if target.ndim == 1 else target
            if target.shape[0] != seq.shape[0]:
                raise ValueError("X and y must share the batch dimension.")
            phi = self._pool(self._encode(weights, seq)[0])
            phi_mean, y_mean = phi.mean(axis=0), target.mean(axis=0)
            pc, yc = phi - phi_mean, target - y_mean
            gram = pc.T @ pc + cfg.ridge_alpha * np.eye(d)
            readout = np.linalg.solve(gram, pc.T @ yc)
            intercept = y_mean - phi_mean @ readout
        self._publish_state(
            _AttnState(
                *(freeze(w) for w in weights),
                readout=None if readout is None else freeze(readout),
                intercept=None if intercept is None else freeze(intercept),
                n_features=n_feat,
            )
        )

    def predict(self, X: FloatArray, **kwargs: Any) -> FloatArray:
        state: _AttnState = self._current_state()
        if state.readout is None or state.intercept is None:
            raise ValueError("Fit with y to enable predict(); use encode() for representations.")
        phi = self._pool(self.encode(X))
        return sanitize(phi @ state.readout + state.intercept)

    def encode(self, X: FloatArray) -> FloatArray:
        state: _AttnState = self._current_state()
        seq = self._as_sequences(X, n_features=state.n_features)
        return self._encode([state.W_in, state.W_q, state.W_k, state.W_v, state.W_o], seq)[0]

    def attention_weights(self, X: FloatArray) -> FloatArray:
        """Return causal attention matrices with shape ``(batch, heads, time, time)``."""
        state: _AttnState = self._current_state()
        seq = self._as_sequences(X, n_features=state.n_features)
        return self._encode([state.W_in, state.W_q, state.W_k, state.W_v, state.W_o], seq)[1]

    def positional_encoding(self, length: int) -> FloatArray:
        d = self.config.d_model
        pos = np.arange(length, dtype=np.float64)[:, None]
        div = self.config.positional_base ** (np.arange(0, d, 2, dtype=np.float64) / d)
        pe = np.zeros((length, d), dtype=np.float64)
        pe[:, 0::2] = np.sin(pos / div)
        pe[:, 1::2] = np.cos(pos / div)[:, : d // 2]
        return pe

    def _encode(self, weights: list[FloatArray], seq: FloatArray) -> tuple[FloatArray, FloatArray]:
        cfg = self.config
        W_in, W_q, W_k, W_v, W_o = weights
        n_batch, n_time, _ = seq.shape
        heads, d = cfg.num_heads, cfg.d_model
        d_k = d // heads
        E = seq @ W_in + self.positional_encoding(n_time)[None, :, :]

        def split(m: FloatArray) -> FloatArray:
            return m.reshape(n_batch, n_time, heads, d_k).transpose(0, 2, 1, 3)

        Q, K, V = split(E @ W_q), split(E @ W_k), split(E @ W_v)
        scores = (Q @ K.transpose(0, 1, 3, 2)) / math.sqrt(d_k)
        causal = np.triu(np.ones((n_time, n_time), dtype=bool), k=1)
        scores = np.where(causal[None, None, :, :], -np.inf, scores)
        scores = scores - scores.max(axis=-1, keepdims=True)
        attn = np.exp(scores)
        attn = attn / attn.sum(axis=-1, keepdims=True)
        context = (attn @ V).transpose(0, 2, 1, 3).reshape(n_batch, n_time, d)
        out = context @ W_o
        if cfg.use_residual:
            out = E + out
        mean = out.mean(axis=-1, keepdims=True)
        var = out.var(axis=-1, keepdims=True)
        return sanitize((out - mean) / np.sqrt(var + cfg.layer_norm_eps)), attn

    def _pool(self, encoded: FloatArray) -> FloatArray:
        return encoded[:, -1, :] if self.config.pooling == "last" else encoded.mean(axis=1)

    def _as_sequences(self, X: FloatArray, *, n_features: int | None) -> FloatArray:
        seq = as_float_array(X, name="X", ndim=3)
        if seq.shape[1] > self.config.max_sequence_length:
            raise ValueError("Sequence length exceeds max_sequence_length.")
        if n_features is not None and seq.shape[2] != n_features:
            raise ValueError(f"X must have {n_features} features on axis 2.")
        return seq
