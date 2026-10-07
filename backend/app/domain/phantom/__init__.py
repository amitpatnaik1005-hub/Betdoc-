"""PHANTOM domain: GARUDA's arbitrage, dutching, matched betting and market-making engines."""

from app.domain.phantom.errors import PhantomDomainError
from app.domain.phantom.manager import CointegrationSignal, MatchedBettingMode, PhantomManager

__all__ = ["CointegrationSignal", "MatchedBettingMode", "PhantomDomainError", "PhantomManager"]
