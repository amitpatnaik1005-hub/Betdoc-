"""The multi-sport engine on the twin's live state (Group 79). Developed for Amit Ashok Kumar Patnaik.

* ``intel_context``: a fixture's intel sections as written by The Wire (Group 78) and the admin, read back as an
  adapter context: the weather section becomes ``weather`` (fortress pillar 2), the injuries section ``absences``
  with each player's 0-1 impact scaled to wins above replacement (``SPORTS_WAR_PER_IMPACT``, fortress pillar 4),
  the referee section ``officials`` (pillar 9). Pillar 1's calibrated weights ride along as ``model_weights``.
* ``in_play_tick`` (FA-1): a sport adapter's live win probability for the backed side, checked against the
  in-play stop-loss shield's rules (Group 77: the stop-loss floor on the cashout or fair value, the relative
  probability collapse), and published on ``<SPORTS_PREFIX>:inplay:<user>`` with the latest frame per fixture
  kept in ``<SPORTS_PREFIX>:inplay:last:<user>``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.domain.backtesting.inplay_stoploss_math import StopLossPolicy, evaluate
from app.domain.sports.base_sport_adapter import LiveWinProbability
from app.services.twin.intel import model_weights, read_intel

FRAME_TTL_SECONDS = 6 * 3600


async def intel_context(redis: Redis, settings: Settings, fixture_id: str, sport_key: str) -> tuple[dict[str, Any], list[str]]:
    """The fixture's intel as context keys, and the sections found. Redis down: nothing (the model runs without it)."""
    try:
        intel = (await read_intel(redis, settings, [fixture_id])).get(fixture_id)
        weights = await model_weights(redis, settings)
    except (RedisError, OSError):
        return {}, []
    out: dict[str, Any] = {}
    sections: list[str] = []
    if weights:
        out["model_weights"] = dict(weights)
    if intel is None:
        return out, sections
    if intel.weather is not None:
        w = intel.weather
        out["weather"] = {"indoor": w.indoor, "wind_kph": w.wind_kmh, "precipitation_mm": w.precipitation_mmh, "temperature_c": w.temperature_c,
                          "altitude_m": w.altitude_m, "dew_expected": bool(w.dew_expected)}
        sections.append("weather")
    if intel.injuries is not None and intel.injuries.absences:
        scale = float(settings.SPORTS_WAR_PER_IMPACT.get(sport_key, 0.0))
        out["absences"] = [{"side": a.side.lower(), "player": a.player, "status": a.status.lower(), "impact": a.impact, "war": round(a.impact * scale, 4)}
                           for a in intel.injuries.absences]
        sections.append("injuries")
    if intel.referee is not None:
        r = intel.referee
        out["officials"] = {"name": r.name, "cards_per_game": r.cards_per_game, "penalties_per_90": r.penalties_per_90,
                            **({"matches": r.matches} if r.matches is not None else {})}
        sections.append("referee")
    return out, sections


def merge_context(base: Mapping[str, Any] | None, intel: Mapping[str, Any]) -> dict[str, Any]:
    """The request's context wins over the intel; the intel fills what the request left out."""
    return {**dict(intel), **dict(base or {})}


def inplay_channel(settings: Settings, user_id: uuid.UUID) -> str:
    return f"{settings.SPORTS_PREFIX}:inplay:{user_id}"


def inplay_last_key(settings: Settings, user_id: uuid.UUID) -> str:
    return f"{settings.SPORTS_PREFIX}:inplay:last:{user_id}"


def shield_call(settings: Settings, live: LiveWinProbability, *, selection: str, entry_probability: float, stake_inr: float,
                entry_odds: float | None, stop_loss_pct: float | None, cashout_offer_inr: float | None) -> dict[str, Any]:
    """The shield's verdict for the backed side: the ticket's fair value is its return x the live probability."""
    policy = StopLossPolicy.from_settings(settings)
    pct = policy.clamp(stop_loss_pct)
    live_p = live.probability_of(selection)
    stake = Decimal(str(stake_inr))
    potential = stake * Decimal(str(entry_odds)) if entry_odds else stake / Decimal(str(entry_probability))
    fair_value = (potential * Decimal(str(round(live_p, 6)))).quantize(Decimal("0.01"))
    offer = None if cashout_offer_inr is None else Decimal(str(cashout_offer_inr)).quantize(Decimal("0.01"))
    call = evaluate(stake, pct, offer=offer, fair_value=fair_value, entry_probability=entry_probability, live_probability=live_p, policy=policy)
    return {"selection": selection, "entry_probability": round(entry_probability, 4), "live_probability": round(live_p, 4),
            "stop_loss_pct": pct, "fair_value_inr": str(fair_value), "cashout_offer_inr": None if offer is None else str(offer),
            "advice": "HOLD" if call is None else "CASH_OUT", "rule": None if call is None else call.rule, "reason": None if call is None else call.reason,
            "floor_inr": None if call is None else str(call.floor_inr)}


async def publish_tick(redis: Redis, settings: Settings, user_id: uuid.UUID, frame: dict[str, Any]) -> bool:
    body = json.dumps(frame, default=str)
    try:
        await redis.publish(inplay_channel(settings, user_id), body)
        key = inplay_last_key(settings, user_id)
        await redis.hset(key, str(frame.get("fixture_id") or frame["sport_key"]), body)
        await redis.expire(key, FRAME_TTL_SECONDS)
    except (RedisError, OSError):
        return False
    return True


def tick_frame(sport_key: str, live: LiveWinProbability, *, fixture_id: str | None, shield: dict[str, Any] | None, at: datetime) -> dict[str, Any]:
    return {"type": "sport_tick", "sport_key": sport_key, "fixture_id": fixture_id, "home_win_prob": round(live.home_win_prob, 4),
            "away_win_prob": round(live.away_win_prob, 4), "draw_prob": round(live.draw_prob, 4), "detail": live.detail, "shield": shield,
            "at": at.isoformat(), "developer_credit": "Amit Ashok Kumar Patnaik"}
