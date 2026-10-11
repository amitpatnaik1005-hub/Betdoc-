"""
Abstract Base Sport Adapter Interface (Group 79)
Developed for Amit Ashok Kumar Patnaik | BetDoc Quantitative Sports Trading Fleet

Every one of the nine sport adapters answers four questions the same way:

* ``calculate_fair_odds``: unmargined probabilities and decimal odds for the match and its line markets,
  from at least three independent analytical sub-models blended into one consensus (fortress pillar 1);
* ``settle_market``: a leg's ``LegStatus`` (WON, LOST, VOID push, HALF_WON / HALF_LOST on quarter lines)
  from the final result; ``resolve_market_winner`` is its boolean view (a half win counts as a win);
* ``in_play_win_probability``: the live win probability from the current game state, the tick the
  in-play stop-loss shield (FA-1) evaluates;
* ``kelly_multiplier``: the sport-calibrated fractional Kelly the CFO sizes with (FA-4), smaller where
  the model's parameters are less certain.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.domain.sports.context import MatchContext
from app.domain.sports.quant import Consensus, SubModelEstimate, blend, clamp_prob, fair_odds
from app.domain.sports.settlement import (
    GENERIC_MARKETS,
    NormalizedResult,
    SettlementInputError,
    is_win,
    norm,
    settle_generic,
)
from app.schemas.bet_types import LegStatus

DEVELOPER_CREDIT = "Amit Ashok Kumar Patnaik"


@dataclass(frozen=True)
class FairOddsResult:
    home_win_prob: float
    away_win_prob: float
    draw_prob: float
    fair_home_odds: float
    fair_away_odds: float
    fair_draw_odds: float | None
    expected_total_score: float
    sport_specific_metrics: dict[str, Any] = field(default_factory=dict)
    market_probabilities: dict[str, float] = field(default_factory=dict)
    sub_models: tuple[SubModelEstimate, ...] = ()
    consensus: Consensus | None = None
    evidence: dict[str, bool] = field(default_factory=dict)
    kelly_multiplier: float = 0.25
    model_family: str = ""

    def probability_of(self, side: str) -> float:
        return {"home": self.home_win_prob, "away": self.away_win_prob, "draw": self.draw_prob}[side]


@dataclass(frozen=True, slots=True)
class LiveWinProbability:
    home_win_prob: float
    away_win_prob: float
    draw_prob: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def probability_of(self, side: str) -> float:
        return {"home": self.home_win_prob, "away": self.away_win_prob, "draw": self.draw_prob}[side]


class BaseSportAdapter(ABC):
    """Base interface for all quantitative sport domain adapters."""

    #: FA-4: sport-calibrated fractional Kelly multiplier (model parameter uncertainty).
    kelly_multiplier: ClassVar[float] = 0.25
    #: The model family shown to the desk.
    model_family: ClassVar[str] = ""
    #: Whether a level result is a selectable outcome of the main market (1X2).
    has_draw: ClassVar[bool] = False
    #: The Odds API sport-key family (``americanfootball_nfl`` -> ``americanfootball``).
    odds_api_family: ClassVar[str] = ""
    #: Sport-specific market names -> (canonical market, default params).
    MARKET_ALIASES: ClassVar[dict[str, tuple[str, dict[str, Any]]]] = {}
    #: Sport-specific (non-generic) canonical markets this adapter settles in ``settle_special``.
    SPECIAL_MARKETS: ClassVar[frozenset[str]] = frozenset()
    #: Generic canonical markets this sport offers.
    GENERIC_SUPPORTED: ClassVar[frozenset[str]] = frozenset()
    #: Which of the 27 universal bet types (``bet_catalog.BetType``) this sport offers.
    BET_TYPES: ClassVar[frozenset[str]] = frozenset()

    @property
    @abstractmethod
    def sport_key(self) -> str:
        """Unique canonical identifier for the sport (e.g. 'cricket', 'basketball')."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name of the sport."""

    @property
    @abstractmethod
    def standard_markets(self) -> list[str]:
        """List of standard market keys supported by this sport adapter."""

    @abstractmethod
    def calculate_fair_odds(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> FairOddsResult:
        """Calculates true unmargined fair probabilities and decimal odds for match winner and totals."""

    @abstractmethod
    def in_play_win_probability(
        self,
        home_stats: dict[str, Any],
        away_stats: dict[str, Any],
        live_state: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> LiveWinProbability:
        """The live win probability from the current game state (FA-1 in-play tick)."""

    @abstractmethod
    def normalize_result(self, match_result: Mapping[str, Any]) -> NormalizedResult:
        """The sport's raw result as the shared ``NormalizedResult``."""

    def settle_special(self, market: str, result: NormalizedResult, raw: Mapping[str, Any], selection: str,
                       line: float | None, params: Mapping[str, Any]) -> LegStatus:
        """Settles one of ``SPECIAL_MARKETS``; adapters with sport-specific markets override it."""
        raise SettlementInputError(f"{self.sport_key} has no market {market!r}")

    # ------------------------------------------------------------------ settlement
    def canonical_market(self, market_type: str) -> tuple[str, dict[str, Any]]:
        """``market_type`` as (canonical market, default params). Accepts sport aliases, canonical keys and the
        ``"<market>:<period>"`` form (``"period_spread:h1"``)."""
        key = norm(market_type)
        if ":" in str(market_type):
            base, _, period = str(market_type).partition(":")
            canonical, defaults = self.canonical_market(base)
            return canonical, {**defaults, "period": norm(period)}
        if key in self.MARKET_ALIASES:
            canonical, defaults = self.MARKET_ALIASES[key]
            return canonical, dict(defaults)
        if key in GENERIC_MARKETS or key in self.SPECIAL_MARKETS:
            return key, {}
        raise SettlementInputError(f"{self.display_name} has no market {market_type!r}")

    def settle_market(
        self,
        market_type: str,
        match_result: Mapping[str, Any],
        selection: str,
        line: float | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> LegStatus:
        canonical, defaults = self.canonical_market(market_type)
        merged = {**defaults, **(params or {})}
        if "line" in merged and line is None:
            line = float(merged["line"])
        result = self.normalize_result(match_result)
        if result.void:
            return LegStatus.VOID
        if canonical in self.SPECIAL_MARKETS:
            return self.settle_special(canonical, result, match_result, selection, line, merged)
        return settle_generic(canonical, result, selection, line, merged, has_draw=self.has_draw)

    def resolve_market_winner(
        self,
        market_type: str,
        match_result: dict[str, Any],
        selection: str,
        line: float | None = None,
    ) -> bool:
        """Resolves whether a betting selection won given final match statistics and line (a half win counts)."""
        try:
            return is_win(self.settle_market(market_type, match_result, selection, line))
        except SettlementInputError:
            return False

    def validate_sport_data(self, data: dict[str, Any]) -> bool:
        """Validates that incoming team/player statistics contain required numerical fields."""
        return isinstance(data, dict) and len(data) > 0

    @property
    def supported_markets(self) -> list[str]:
        """Every market name this adapter settles: its aliases, generic markets and special markets."""
        return sorted(set(self.MARKET_ALIASES) | set(self.GENERIC_SUPPORTED) | set(self.SPECIAL_MARKETS))

    # ------------------------------------------------------------------ assembly helpers
    def _consensus(self, estimates: list[SubModelEstimate], ctx: MatchContext) -> Consensus:
        return blend(estimates, dict(ctx.model_weights))

    def _assemble(
        self,
        consensus: Consensus,
        ctx: MatchContext,
        *,
        expected_total: float,
        metrics: dict[str, Any],
        markets: dict[str, float],
    ) -> FairOddsResult:
        home = clamp_prob(consensus.home_win_prob)
        away = clamp_prob(consensus.away_win_prob)
        draw = consensus.draw_prob if self.has_draw or consensus.draw_prob > 0 else 0.0
        total = home + away + draw
        home, away, draw = home / total, away / total, draw / total
        return FairOddsResult(
            home_win_prob=round(home, 4),
            away_win_prob=round(away, 4),
            draw_prob=round(draw, 4),
            fair_home_odds=round(fair_odds(home), 3),
            fair_away_odds=round(fair_odds(away), 3),
            fair_draw_odds=round(fair_odds(draw), 3) if draw > 0 else None,
            expected_total_score=round(expected_total, 2),
            sport_specific_metrics={**metrics, "consensus": consensus.as_dict(), "kelly_multiplier": self.kelly_multiplier,
                                    "model_family": self.model_family},
            market_probabilities={k: round(float(v), 4) for k, v in markets.items()},
            sub_models=consensus.sub_models,
            consensus=consensus,
            evidence=ctx.evidence(),
            kelly_multiplier=self.kelly_multiplier,
            model_family=self.model_family,
        )
