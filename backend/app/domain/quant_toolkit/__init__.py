"""The Lab's quant toolkit (PANINI): dutching, matched betting, market making and cointegration (Group 96)."""

from app.domain.quant_toolkit.errors import QuantToolkitError
from app.domain.quant_toolkit.manager import CointegrationSignal, MatchedBettingMode, QuantToolkitManager

__all__ = ["CointegrationSignal", "MatchedBettingMode", "QuantToolkitError", "QuantToolkitManager"]
