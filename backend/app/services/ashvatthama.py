"""ASHVATTHAMA, the consultant and bet-slip architect (Group 80; reshaped in Group 97). Developed for Amit Ashok Kumar Patnaik.

BetDoc automates no bookmaker (Parimatch publishes no betting API): every real ticket is placed by hand. What remains
here is where each venue's slip is placed, which the real-money desk's tickets carry. Group 97 retired the paper-mode
fill simulator and the stand-alone live ticket with the Paper Trading Sandbox.
"""

from __future__ import annotations

from app.core.config import Settings

COMMANDER = "ASHVATTHAMA"


def venue_url(settings: Settings, venue: str) -> str | None:
    """Where the trader places a slip on ``venue`` by hand; None for a venue BetDoc has no address for."""
    return {"parimatch": settings.PARIMATCH_TARGET_URL, "1xbet": settings.ONE_XBET_TARGET_URL, "stake": settings.STAKE_TARGET_URL,
            "betfair": "https://www.betfair.com/exchange/plus/", "pinnacle": "https://www.pinnacle.com/"}.get(venue)
