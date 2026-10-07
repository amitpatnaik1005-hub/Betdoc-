"""Odds comparison analytics with deterministic tie-breaking."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from numbers import Real
from statistics import fmean
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from app.models.bookmakers import BookmakerConfigModel


class BestOddsResult(TypedDict):
    best_bookmaker: str
    best_odds: float
    mean_market_odds: float
    edge_percentage: float
    considered_bookmakers: list[str]
    ignored_bookmakers: list[str]


def _validate_odds(bookmaker: str, odds: object) -> float:
    if isinstance(odds, bool) or not isinstance(odds, Real):
        raise ValueError(f"Odds for {bookmaker!r} must be a real number, got {type(odds).__name__}.")
    value = float(odds)
    if not math.isfinite(value):
        raise ValueError(f"Odds for {bookmaker!r} must be finite, got {value!r}.")
    if value <= 0:
        raise ValueError(f"Odds for {bookmaker!r} must be positive, got {value!r}.")
    return value


def _validate_config(config: BookmakerConfigModel) -> tuple[str, int]:
    name = config.name
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Active bookmaker names must be non-empty strings.")
    rank = config.priority_rank
    if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
        raise ValueError(f"priority_rank for {name!r} must be an integer >= 1, got {rank!r}.")
    return name, rank


def _ranking_key(config: BookmakerConfigModel, odds: float) -> tuple[float, int, str, str]:
    # Highest odds first, then lowest priority_rank, then case-insensitive name,
    # then the raw name so the ordering is total and fully deterministic.
    return (-odds, config.priority_rank, config.name.casefold(), config.name)


def find_best_odds(
    market_data: Mapping[str, float],
    active_bookmakers: Sequence[BookmakerConfigModel],
) -> BestOddsResult:
    """Pick the best odds among active bookmakers.

    Odds from bookmakers not in ``active_bookmakers`` are ignored (bookmaker
    names are matched case-insensitively). Ties go to the lowest
    ``priority_rank``, then to the case-insensitive alphabetical name.
    """
    if not isinstance(market_data, Mapping):
        raise ValueError("market_data must be a mapping of bookmaker name to decimal odds.")

    active_by_key: dict[str, BookmakerConfigModel] = {}
    for config in active_bookmakers:
        name, _ = _validate_config(config)
        key = name.strip().casefold()
        if key in active_by_key:
            raise ValueError(f"Duplicate active bookmaker (case-insensitive): {name!r}.")
        active_by_key[key] = config

    original_names: dict[str, str] = {}
    validated: dict[str, float] = {}
    for raw_name, raw_odds in market_data.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("Bookmaker names in market_data must be non-empty strings.")
        key = raw_name.strip().casefold()
        if key in validated:
            raise ValueError(f"Duplicate bookmaker in market_data (case-insensitive): {raw_name!r}.")
        validated[key] = _validate_odds(raw_name, raw_odds)
        original_names[key] = raw_name.strip()

    eligible = [(active_by_key[key], odds) for key, odds in validated.items() if key in active_by_key]
    ignored = sorted(
        (original_names[key] for key in validated if key not in active_by_key), key=str.casefold
    )

    if not eligible:
        raise ValueError("None of the supplied odds belong to an active bookmaker.")

    ranked = sorted(eligible, key=lambda item: _ranking_key(item[0], item[1]))
    best_config, best_odds = ranked[0]
    mean_odds = fmean(odds for _, odds in eligible)
    edge_percentage = (best_odds - mean_odds) / mean_odds * 100.0

    return BestOddsResult(
        best_bookmaker=best_config.name,
        best_odds=best_odds,
        mean_market_odds=round(mean_odds, 6),
        edge_percentage=round(edge_percentage, 4),
        considered_bookmakers=[config.name for config, _ in ranked],
        ignored_bookmakers=ignored,
    )
