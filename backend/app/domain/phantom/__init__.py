"""The Lab's quant toolkit (PANINI): dutching, matched betting, market making and cointegration (Group 96)."""

from app.domain.phantom.errors import PhantomDomainError
from app.domain.phantom.manager import CointegrationSignal, MatchedBettingMode, PhantomManager

__all__ = ["CointegrationSignal", "MatchedBettingMode", "PhantomDomainError", "PhantomManager"]
