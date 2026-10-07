"""Universal base contract for the Smallcase Engine v2 model suite.

Concurrency contract
--------------------
* Configs are immutable (``frozen=True``) and strictly typed (``strict=True``).
* ``fit`` builds every learned parameter in local variables, freezes it into an
  immutable snapshot and publishes that snapshot atomically under a lock.
* ``predict`` grabs a reference to the current snapshot and operates on local data only.

Models are therefore safe to execute concurrently via ``asyncio.to_thread()`` and a
concurrent re-fit can never expose a half-trained model to readers.
"""

from __future__ import annotations

import math
import threading
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, Self, TypeVar

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, field_validator

__all__ = [
    "FLOAT_EPS",
    "FLOAT_MAX",
    "FLOAT_TINY",
    "BaseMathModel",
    "FloatArray",
    "MathModelConfig",
    "ModelNotFittedError",
    "NumericalStabilityError",
    "as_classification_target",
    "as_feature_matrix",
    "as_float_array",
    "as_vector",
    "check_finite_array",
    "check_finite_scalar",
    "freeze",
    "require_integral",
    "resolve_config",
    "safe_array_divide",
    "safe_scalar_divide",
    "sanitize",
]

FloatArray = npt.NDArray[np.float64]

FLOAT_EPS: float = float(np.finfo(np.float64).eps)
FLOAT_TINY: float = float(np.finfo(np.float64).tiny)
FLOAT_MAX: float = float(np.finfo(np.float64).max)
_SEED_UPPER_BOUND: int = 2**32  # numpy / scikit-learn seed domain


class MathModelConfig(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    random_state: int | None = 42

    @field_validator("random_state")
    @classmethod
    def _validate_seed(cls, value: int | None) -> int | None:
        if value is not None and not 0 <= value < _SEED_UPPER_BOUND:
            raise ValueError(f"random_state must lie in [0, {_SEED_UPPER_BOUND}).")
        return value


class ModelNotFittedError(RuntimeError):
    """Raised when a stateful model is queried before ``fit``."""


class NumericalStabilityError(ValueError):
    """Raised when inputs or intermediate results violate IEEE 754 finiteness."""


ConfigT = TypeVar("ConfigT", bound=MathModelConfig)


def resolve_config(config: MathModelConfig | None, config_cls: type[ConfigT]) -> ConfigT:
    """Return a validated config instance of ``config_cls``."""
    if config is None:
        return config_cls()
    if not isinstance(config, config_cls):
        raise TypeError(f"Expected {config_cls.__name__}, received {type(config).__name__}.")
    return config


def as_float_array(
    data: Any,
    *,
    name: str = "X",
    ndim: int | Sequence[int] | None = None,
    allow_empty: bool = False,
) -> FloatArray:
    """Copy ``data`` into a finite float64 array, validating dimensionality."""
    try:
        array = np.array(data, dtype=np.float64, copy=True)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be convertible to a float64 array.") from exc
    if ndim is not None:
        allowed = (ndim,) if isinstance(ndim, int) else tuple(ndim)
        if array.ndim not in allowed:
            raise ValueError(f"{name} must have ndim in {allowed}, got {array.ndim}.")
    if not allow_empty and array.size == 0:
        raise ValueError(f"{name} must not be empty.")
    if not np.all(np.isfinite(array)):
        raise NumericalStabilityError(f"{name} contains NaN or infinite values.")
    return array


def as_feature_matrix(
    data: Any,
    *,
    name: str = "X",
    n_features: int | None = None,
    min_samples: int = 1,
) -> FloatArray:
    """Validate a 2-D ``(n_samples, n_features)`` matrix."""
    matrix = as_float_array(data, name=name, ndim=2)
    if matrix.shape[0] < min_samples:
        raise ValueError(f"{name} requires at least {min_samples} rows, got {matrix.shape[0]}.")
    if n_features is not None and matrix.shape[1] != n_features:
        raise ValueError(f"{name} must have {n_features} columns, got {matrix.shape[1]}.")
    return matrix


def as_vector(data: Any, *, name: str = "y", length: int | None = None) -> FloatArray:
    """Validate a 1-D vector (a single-column 2-D array is accepted and flattened)."""
    array = as_float_array(data, name=name, ndim=(1, 2))
    if array.ndim == 2:
        if array.shape[1] != 1:
            raise ValueError(f"{name} must be 1-D or a single column.")
        array = array[:, 0]
    if length is not None and array.shape[0] != length:
        raise ValueError(f"{name} must have length {length}, got {array.shape[0]}.")
    return array


def as_classification_target(data: Any, *, length: int, name: str = "y") -> FloatArray:
    """Validate a classification target with at least two distinct classes."""
    if data is None:
        raise ValueError(f"{name} is required for supervised fitting.")
    target = as_vector(data, name=name, length=length)
    if np.unique(target).size < 2:
        raise ValueError(f"{name} must contain at least two distinct classes.")
    return target


def require_integral(array: FloatArray, *, name: str) -> npt.NDArray[np.int64]:
    """Ensure every element is integer valued and return an int64 copy."""
    if not np.all(array == np.round(array)):
        raise ValueError(f"{name} must contain integer-valued entries.")
    return array.astype(np.int64)


def check_finite_array(array: Any, *, name: str) -> FloatArray:
    """Return ``array`` as float64, raising if any element is NaN or infinite."""
    arr = np.asarray(array, dtype=np.float64)
    if not np.all(np.isfinite(arr)):
        raise NumericalStabilityError(f"{name} contains NaN or infinite values.")
    return arr


def check_finite_scalar(value: float, *, name: str) -> float:
    """Raise if ``value`` is NaN or infinite."""
    result = float(value)
    if not math.isfinite(result):
        raise NumericalStabilityError(f"{name} is not finite ({result}).")
    return result


def safe_scalar_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """IEEE-safe scalar division that handles ``ZeroDivisionError`` natively."""
    try:
        result = float(numerator) / float(denominator)
    except ZeroDivisionError:
        return default
    return result if math.isfinite(result) else default


def sanitize(array: Any, *, fill: float = 0.0) -> FloatArray:
    """Replace NaN with ``fill`` and clamp infinities to the float64 range."""
    return np.nan_to_num(np.asarray(array, dtype=np.float64), nan=fill, posinf=FLOAT_MAX, neginf=-FLOAT_MAX)


def safe_array_divide(numerator: Any, denominator: Any, default: float = 0.0) -> FloatArray:
    """Element-wise division returning ``default`` wherever the denominator underflows."""
    num = np.asarray(numerator, dtype=np.float64)
    den = np.asarray(denominator, dtype=np.float64)
    num, den = np.broadcast_arrays(num, den)
    out = np.full(num.shape, default, dtype=np.float64)
    np.divide(num, den, out=out, where=np.abs(den) > FLOAT_TINY)
    return sanitize(out, fill=default)


def freeze(array: npt.NDArray[Any]) -> npt.NDArray[Any]:
    """Return a read-only copy of ``array`` for immutable state snapshots."""
    frozen = np.array(array, copy=True)
    frozen.setflags(write=False)
    return frozen


class BaseMathModel(ABC):
    def __init__(self, config: MathModelConfig) -> None:
        self.config = config
        self._state_lock = threading.RLock()
        self._state: Any | None = None

    @abstractmethod
    def fit(self, X: npt.NDArray[np.float64], y: npt.NDArray[np.float64] | None = None, **kwargs: Any) -> None:
        """Fit the model. Must be thread-safe."""
        pass

    @abstractmethod
    def predict(self, X: npt.NDArray[np.float64], **kwargs: Any) -> npt.NDArray[np.float64]:
        """Return deterministic predictions. Must be thread-safe."""
        pass

    @property
    def is_fitted(self) -> bool:
        with self._state_lock:
            return self._state is not None

    def _publish_state(self, state: Any) -> None:
        """Atomically publish a fully built, immutable state snapshot."""
        with self._state_lock:
            self._state = state

    def _current_state(self) -> Any:
        """Return the current snapshot or raise ``ModelNotFittedError``."""
        with self._state_lock:
            state = self._state
        if state is None:
            raise ModelNotFittedError(f"{type(self).__name__} must be fitted before use.")
        return state

    def _optional_state(self) -> Any | None:
        with self._state_lock:
            return self._state

    def _rng(self, *streams: int) -> np.random.Generator:
        """Create a call-local generator (thread-safe, deterministic when seeded)."""
        seed = self.config.random_state  # type: ignore[attr-defined]
        if seed is None:
            return np.random.default_rng()
        return np.random.default_rng([seed, *(abs(int(s)) for s in streams)])

    def with_overrides(self, **overrides: Any) -> Self:
        """Return a new, unfitted model whose config is re-validated with ``overrides``."""
        payload = {**self.config.model_dump(), **overrides}
        new_config = type(self.config).model_validate(payload)
        return type(self)(new_config)  # type: ignore[call-arg]
