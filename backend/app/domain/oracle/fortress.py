"""The 14-pillar fortress: the twin's last word on a slip before it reaches the user (Group 72).

The fortress runs over a slip the parlay engine has already priced and simulated (Group 69) at a retail
book (Parimatch, 1xBet), plus the evidence gathered for its fixtures. Every pillar returns PASS, FAIL or
UNVERIFIED: missing or stale evidence is never a pass. A slip is vetted only when every enforced pillar
passes (``TWIN_ADVISORY_PILLARS`` may name pillars that only report).

 1. Model consensus. At least ``min_models`` models price every leg; no model gives a leg EV <= 0
    (the veto); the weighted model EV of every leg, and the slip's simulated joint EV, clear ``min_ev``.
    Weights come from the calibration store (``<prefix>:model_weights``), equal when it is empty.
 2. Weather. Outdoors: wind and rain under their limits (the models price neither).
 3. Travel. The backed side: no charter delay past the limit, no turnaround under ``min_rest_hours``;
    a side that crossed ``circadian_timezones`` loses ``circadian_penalty`` points of win probability,
    and the leg must still be worth backing after the cut. Markets that back no side (draw, totals,
    BTTS) need both sides clear.
 4. Injuries and the dressing room. No tier-1 absence (impact >= ``key_player_impact``) on either side,
    and no managerial change within ``manager_change_days``: the pre-match prices no longer describe
    the teams.
 5. Lineups. Both official team sheets are published.
 6. Market microstructure. No sharp steam on another selection of the leg's market (the line moving
    against it), and no reverse line movement: the public on this selection at ``rlm_public_share`` or
    more while its price lengthens from the open.
 7. Sharp price. Each leg's retail odds beat the sharp books' price, de-vigged by Shin's method over
    the whole market, by at least ``min_sharp_edge``.
 8. The crowd's parlays. No leg sits in a live PUBLIC TRAP of the trend scan.
 9. The referee (sports in ``referee_sports``). A known referee whose penalty rate is under the limit.
10. Motivation. The opponent of the backed side does not want it ``motivation_gap`` more; a derby needs
    ``derby_ev_multiplier`` times the EV bar.
11. Liquidity. The stake fits under the book's maximum for the fixture (the evidence, else the
    configured venue limit). An unknown maximum is unverified.
12. Independence. The anti-correlation gate: no two legs of one fixture, no team twice in 36 hours.
13. Sizing. Quarter Kelly of the slip's growth-optimal fraction, capped; a rolling drawdown past
    ``drawdown_scale_at`` halves it, past ``drawdown_halt_at`` stakes nothing (and pages the Sentinel).
    The stake rounds down to ``stake_step``; one that rounds to nothing fails.
14. Execution gate. The kill switch is off (and Redis can say so) and every retail price is fresh.
    ``confirm`` re-runs this pillar against a re-fetched price before the slip is placed or routed.

No slip is a certainty: the audit's numbers are what it is worth, and passing 14 pillars says how much
was checked, not that it cannot lose.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Any

from app.domain.oracle.markets import MarketKind, MarketRef
from app.domain.oracle.parlay_engine import LegCandidate, Quote, SlipCandidate
from app.schemas.twin import FixtureIntel

PAISA = Decimal("0.01")
PILLAR_KEYS: dict[int, str] = {
    1: "model_consensus", 2: "weather", 3: "travel_fatigue", 4: "injuries", 5: "lineups", 6: "market_microstructure", 7: "sharp_price",
    8: "crowd_parlays", 9: "referee", 10: "motivation", 11: "liquidity", 12: "independence", 13: "sizing", 14: "execution_gate",
}
PILLAR_TITLES: dict[int, str] = {
    1: "Model consensus & zero-negative-EV veto", 2: "Weather & pitch", 3: "Travel & circadian fatigue", 4: "Injury wire & dressing room",
    5: "Official lineups", 6: "Reverse line movement & steam", 7: "De-vigged sharp price", 8: "Crowd parlay forensics",
    9: "Referee profile", 10: "Motivation & derby", 11: "Liquidity & stake limits", 12: "Leg independence",
    13: "Fractional Kelly & drawdown breaker", 14: "Kill switch & fresh price",
}


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"
    ADVISORY = "ADVISORY"


@dataclass(frozen=True, slots=True)
class PillarResult:
    number: int
    status: Status
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"number": self.number, "key": PILLAR_KEYS[self.number], "title": PILLAR_TITLES[self.number], "status": self.status.value,
                "reason": self.reason, "metrics": self.metrics}


# ================================================================ policy
@dataclass(frozen=True, slots=True)
class FortressPolicy:
    min_models: int
    min_ev: float
    max_wind_kmh: float
    max_rain_mmh: float
    max_flight_delay_hours: float
    min_rest_hours: float
    circadian_timezones: int
    circadian_penalty: float
    key_player_impact: float
    manager_change_days: float
    rlm_public_share: float
    min_sharp_edge: float
    sharp_max_age_seconds: float
    referee_sports: tuple[str, ...]
    referee_max_penalties_per_90: float
    referee_strict_cards: float
    motivation_gap: float
    derby_ev_multiplier: float
    kelly_fraction: float
    max_stake_pct: float
    drawdown_scale_at: float
    drawdown_scale: float
    drawdown_halt_at: float
    stake_step: Decimal
    max_quote_age_seconds: float
    max_odds_drift_pct: float
    intel_max_age: Mapping[str, timedelta]
    advisory: frozenset[int] = frozenset()

    @classmethod
    def from_settings(cls, settings: Any) -> FortressPolicy:
        advisory = frozenset(int(p) for p in str(settings.TWIN_ADVISORY_PILLARS).replace(" ", "").split(",") if p.isdigit() and 1 <= int(p) <= 14)
        return cls(
            min_models=settings.TWIN_MIN_MODELS, min_ev=settings.TWIN_MIN_CONSENSUS_EV,
            max_wind_kmh=settings.TWIN_MAX_WIND_KMH, max_rain_mmh=settings.TWIN_MAX_RAIN_MMH,
            max_flight_delay_hours=settings.TWIN_MAX_FLIGHT_DELAY_HOURS, min_rest_hours=settings.TWIN_MIN_REST_HOURS,
            circadian_timezones=settings.TWIN_CIRCADIAN_TIMEZONES, circadian_penalty=settings.TWIN_CIRCADIAN_PENALTY,
            key_player_impact=settings.TWIN_KEY_PLAYER_IMPACT, manager_change_days=settings.TWIN_MANAGER_CHANGE_DAYS,
            rlm_public_share=settings.TWIN_RLM_PUBLIC_SHARE, min_sharp_edge=settings.TWIN_MIN_SHARP_EDGE, sharp_max_age_seconds=settings.TWIN_SHARP_MAX_AGE_SECONDS,
            referee_sports=tuple(s.strip().casefold() for s in settings.TWIN_REFEREE_SPORTS.split(",") if s.strip()),
            referee_max_penalties_per_90=settings.TWIN_REFEREE_MAX_PENALTIES_PER_90, referee_strict_cards=settings.TWIN_REFEREE_STRICT_CARDS,
            motivation_gap=settings.TWIN_MOTIVATION_MAX_GAP, derby_ev_multiplier=settings.TWIN_DERBY_EV_MULTIPLIER,
            kelly_fraction=settings.TWIN_KELLY_FRACTION, max_stake_pct=settings.TWIN_MAX_STAKE_PCT,
            drawdown_scale_at=settings.TWIN_DRAWDOWN_SCALE_AT, drawdown_scale=settings.TWIN_DRAWDOWN_SCALE, drawdown_halt_at=settings.TWIN_DRAWDOWN_HALT_AT,
            stake_step=Decimal(settings.TWIN_STAKE_STEP_INR), max_quote_age_seconds=settings.TWIN_MAX_QUOTE_AGE_SECONDS,
            max_odds_drift_pct=settings.TWIN_MAX_ODDS_DRIFT_PCT,
            intel_max_age={k: timedelta(minutes=float(v)) for k, v in settings.TWIN_INTEL_MAX_AGE_MINUTES.items()},
            advisory=advisory,
        )


# ================================================================ the maths
def shin_devig(odds: Sequence[float], *, tol: float = 1e-12) -> tuple[list[float], float]:
    """Fair probabilities from one book's complete market by Shin's model, and z (the insider share).

    With implied ``pi_i = 1/o_i`` and booksum ``B``, ``p_i(z) = (sqrt(z^2 + 4(1-z) pi_i^2 / B) - z) / (2(1-z))``
    and z solves ``sum p_i(z) = 1`` (equivalently ``sum sqrt(z^2 + 4(1-z) pi_i^2 / B) = 2 + (n-2) z``).
    ``sum p_i`` falls from ``sqrt(B) > 1`` at z = 0 to under 1 as z grows,
    so bisection finds the one root. A book without overround (B <= 1) is normalised proportionally.
    """
    if len(odds) < 2 or any(not math.isfinite(o) or o <= 1.0 for o in odds):
        raise ValueError("a complete market of decimal odds above 1")
    implied = [1.0 / o for o in odds]
    booksum = sum(implied)
    if booksum <= 1.0 + tol:
        return [p / booksum for p in implied], 0.0

    def probs(z: float) -> list[float]:
        return [(math.sqrt(z * z + 4.0 * (1.0 - z) * p * p / booksum) - z) / (2.0 * (1.0 - z)) for p in implied]

    lo, hi = 0.0, 0.999
    if sum(probs(hi)) - 1.0 > 0:  # no root in range: margin beyond any insider share, fall back to proportional
        return [p / booksum for p in implied], hi
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if sum(probs(mid)) - 1.0 > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    z = (lo + hi) / 2.0
    fair = probs(z)
    total = sum(fair)
    return [p / total for p in fair], z


def rolling_drawdown(bankroll_now: Decimal, pnls_oldest_first: Sequence[Decimal]) -> float:
    """The deepest fall from a peak over the window, as a share of that peak. The window starts at
    ``bankroll_now - sum(pnls)``; each settled bet moves equity by its P&L."""
    equity = bankroll_now - sum(pnls_oldest_first, Decimal(0))
    peak = equity
    worst = 0.0
    for pnl in pnls_oldest_first:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, float((peak - equity) / peak))
    return worst


@dataclass(frozen=True, slots=True)
class Sizing:
    fraction: float  # of bankroll, after the Kelly fraction, the cap and the drawdown scaling
    stake: Decimal
    halted: bool
    scaled: bool
    full_kelly: float


def size_stake(full_kelly: float, bankroll: Decimal, drawdown: float, policy: FortressPolicy) -> Sizing:
    if drawdown > policy.drawdown_halt_at:
        return Sizing(0.0, Decimal(0).quantize(PAISA), True, False, full_kelly)
    fraction = min(max(full_kelly, 0.0) * policy.kelly_fraction, policy.max_stake_pct)
    scaled = drawdown > policy.drawdown_scale_at
    if scaled:
        fraction *= policy.drawdown_scale
    raw = bankroll * Decimal(str(fraction))
    stake = ((raw / policy.stake_step).to_integral_value(rounding=ROUND_DOWN) * policy.stake_step).quantize(PAISA)
    return Sizing(fraction, stake, False, scaled, full_kelly)


def backed_side(market: MarketRef, selection: str) -> str | None:
    """The team a selection needs to do well (None: the draw, totals, BTTS, '12')."""
    if market.kind in (MarketKind.MATCH_ODDS, MarketKind.ASIAN_HANDICAP, MarketKind.DRAW_NO_BET):
        return selection if selection in ("HOME", "AWAY") else None
    if market.kind is MarketKind.DOUBLE_CHANCE:
        return {"1X": "HOME", "X2": "AWAY"}.get(selection)
    return None


# ================================================================ inputs
@dataclass(slots=True)
class LegEvidence:
    leg: LegCandidate  # retail quotes only
    quote: Quote  # the retail price the slip was simulated at
    sharp_market: dict[str, dict[str, Quote]]  # selection -> sharp book -> quote, over the leg's whole market
    intel: FixtureIntel | None
    steam_selections: frozenset[str] | None  # selections of this market with sharp steam; None: unreadable
    public_share: float | None = None  # BetDoc's own measured share of bets on this selection
    public_traps: tuple[str, ...] = ()  # titles of live PUBLIC_TRAP parlays holding this leg


@dataclass(slots=True)
class FortressInputs:
    slip: SlipCandidate
    legs: list[LegEvidence]
    bankroll: Decimal | None
    drawdown: float  # rolling, from the user's settled bets
    kill_switch: bool | None  # None: Redis cannot say
    model_weights: Mapping[str, float]
    book_max_stake: Decimal | None  # the configured venue maximum, if any
    now: datetime


@dataclass(frozen=True, slots=True)
class FortressVerdict:
    pillars: tuple[PillarResult, ...]
    is_vetted: bool
    passed: int
    conviction: float
    sizing: Sizing | None
    consensus_ev: float | None
    sharp_edge: float | None
    reasons: tuple[str, ...]


def _section(intel: FixtureIntel | None, name: str, now: datetime, policy: FortressPolicy) -> Any | None:
    """The section if present and inside its age limit."""
    if intel is None:
        return None
    section = getattr(intel, name)
    if section is None:
        return None
    seen = section.observed_at if section.observed_at.tzinfo else section.observed_at.replace(tzinfo=UTC)
    limit = policy.intel_max_age.get(name)
    if limit is not None and now - seen > limit:
        return None
    return section


def _label(ev: LegEvidence) -> str:
    leg = ev.leg
    return f"{leg.home} v {leg.away} {leg.market.key} {leg.selection}"


def _missing(number: int, what: Sequence[str]) -> PillarResult:
    return PillarResult(number, Status.UNVERIFIED, f"no fresh evidence for {', '.join(what[:4])}" + (" ..." if len(what) > 4 else ""), {"missing": list(what)})


# ================================================================ the pillars
def pillar_1(inputs: FortressInputs, policy: FortressPolicy, min_ev_per_leg: Mapping[str, float]) -> tuple[PillarResult, float | None]:
    fails: list[str] = []
    per_leg: list[dict[str, Any]] = []
    weakest: float | None = None
    for ev in inputs.legs:
        evs = ev.leg.model_evs(ev.quote.net_odds)
        weights = {name: max(float(inputs.model_weights.get(name, 1.0)), 0.0) for name in evs}
        total = sum(weights.values())
        consensus = sum(weights[n] * v for n, v in evs.items()) / total if total > 0 else None
        dissent = sorted(name for name, v in evs.items() if v <= 0)
        bar = min_ev_per_leg.get(ev.leg.leg_id, policy.min_ev)
        per_leg.append({"leg": ev.leg.leg_id, "models": {k: round(v, 4) for k, v in evs.items()}, "consensus_ev": None if consensus is None else round(consensus, 4), "bar": bar})
        if len(evs) < policy.min_models:
            fails.append(f"{_label(ev)}: {len(evs)} model(s) price it, {policy.min_models} needed")
        if dissent:
            fails.append(f"{_label(ev)}: EV <= 0 under {', '.join(dissent)} (veto)")
        if consensus is None or consensus < bar:
            fails.append(f"{_label(ev)}: weighted model EV {0 if consensus is None else consensus:+.2%} under {bar:+.2%}")
        if consensus is not None:
            weakest = consensus if weakest is None else min(weakest, consensus)
    joint = inputs.slip.sim.joint_ev
    if len(inputs.legs) > 1 and joint < policy.min_ev:
        fails.append(f"joint EV {joint:+.2%} under {policy.min_ev:+.2%}")
    metrics = {"legs": per_leg, "joint_ev": round(joint, 4), "weights": dict(inputs.model_weights) or "equal"}
    if fails:
        return PillarResult(1, Status.FAIL, "; ".join(fails), metrics), weakest
    return PillarResult(1, Status.PASS, f"every model prices every leg above its odds; weakest weighted EV {weakest:+.2%}", metrics), weakest


def pillar_2(inputs: FortressInputs, policy: FortressPolicy) -> PillarResult:
    missing, fails, seen = [], [], {}
    for fixture, ev in _by_fixture(inputs.legs).items():
        w = _section(ev.intel, "weather", inputs.now, policy)
        if w is None:
            missing.append(f"{ev.leg.home} v {ev.leg.away}")
            continue
        seen[fixture] = {"indoor": w.indoor, "wind_kmh": w.wind_kmh, "precipitation_mmh": w.precipitation_mmh, "altitude_m": w.altitude_m, "dew_expected": w.dew_expected, "source": w.source}
        if w.indoor:
            continue
        if w.wind_kmh > policy.max_wind_kmh:
            fails.append(f"{ev.leg.home} v {ev.leg.away}: wind {w.wind_kmh:g} km/h over {policy.max_wind_kmh:g}")
        if w.precipitation_mmh > policy.max_rain_mmh:
            fails.append(f"{ev.leg.home} v {ev.leg.away}: rain {w.precipitation_mmh:g} mm/h over {policy.max_rain_mmh:g}")
    if fails:
        return PillarResult(2, Status.FAIL, "; ".join(fails), {"fixtures": seen})
    if missing:
        return _missing(2, [f"weather at {m}" for m in missing])
    return PillarResult(2, Status.PASS, "conditions inside the limits everywhere", {"fixtures": seen})


def pillar_3(inputs: FortressInputs, policy: FortressPolicy) -> PillarResult:
    missing, fails, notes = [], [], []
    for ev in inputs.legs:
        t = _section(ev.intel, "travel", inputs.now, policy)
        if t is None:
            missing.append(f"travel for {ev.leg.home} v {ev.leg.away}")
            continue
        side = backed_side(ev.leg.market, ev.leg.selection)
        sides = [side] if side else ["HOME", "AWAY"]
        for s in sides:
            team = t.home if s == "HOME" else t.away
            name = ev.leg.home if s == "HOME" else ev.leg.away
            if team.flight_delay_hours > policy.max_flight_delay_hours:
                fails.append(f"{name}: charter delayed {team.flight_delay_hours:g}h (limit {policy.max_flight_delay_hours:g}h)")
            if team.rest_hours < policy.min_rest_hours:
                fails.append(f"{name}: {team.rest_hours:g}h since its last fixture (minimum {policy.min_rest_hours:g}h)")
            if team.timezones_crossed >= policy.circadian_timezones:
                if side is None:
                    fails.append(f"{name}: {team.timezones_crossed} time zones crossed; the leg backs no side to discount")
                    continue
                cut_ev = (ev.leg.probability - policy.circadian_penalty) * ev.quote.net_odds - 1.0
                notes.append(f"{name}: circadian cut {policy.circadian_penalty:.1%} -> EV {cut_ev:+.2%}")
                if cut_ev <= 0:
                    fails.append(f"{name}: {team.timezones_crossed} time zones crossed; after the {policy.circadian_penalty:.1%} cut the leg is worth {cut_ev:+.2%}")
    if fails:
        return PillarResult(3, Status.FAIL, "; ".join(fails), {"notes": notes})
    if missing:
        return _missing(3, missing)
    return PillarResult(3, Status.PASS, "; ".join(notes) if notes else "no delayed charter, short turnaround or circadian disruption on the backed side", {"notes": notes})


def pillar_4(inputs: FortressInputs, policy: FortressPolicy) -> PillarResult:
    missing, fails = [], []
    for ev in _by_fixture(inputs.legs).values():
        inj = _section(ev.intel, "injuries", inputs.now, policy)
        if inj is None:
            missing.append(f"injury wire for {ev.leg.home} v {ev.leg.away}")
            continue
        for a in inj.absences:
            if a.impact >= policy.key_player_impact:
                team = ev.leg.home if a.side == "HOME" else ev.leg.away
                fails.append(f"{team}: {a.player} {a.status} (impact {a.impact:.2f})")
        for side, at in inj.manager_changed_at.items():
            when = at if at.tzinfo else at.replace(tzinfo=UTC)
            if inputs.now - when <= timedelta(days=policy.manager_change_days):
                fails.append(f"{ev.leg.home if side == 'HOME' else ev.leg.away}: managerial change {(inputs.now - when).days}d ago (volatility)")
    if fails:
        return PillarResult(4, Status.FAIL, "; ".join(fails))
    if missing:
        return _missing(4, missing)
    return PillarResult(4, Status.PASS, "no tier-1 absence and no managerial change in the window")


def pillar_5(inputs: FortressInputs, policy: FortressPolicy) -> PillarResult:
    missing, fails = [], []
    for ev in _by_fixture(inputs.legs).values():
        lu = _section(ev.intel, "lineups", inputs.now, policy)
        if lu is None:
            missing.append(f"lineups for {ev.leg.home} v {ev.leg.away}")
        elif not (lu.home_confirmed and lu.away_confirmed):
            pending = [n for n, ok in ((ev.leg.home, lu.home_confirmed), (ev.leg.away, lu.away_confirmed)) if not ok]
            fails.append(f"team sheet not yet published: {', '.join(pending)}")
    if fails:
        return PillarResult(5, Status.FAIL, "; ".join(fails))
    if missing:
        return _missing(5, missing)
    return PillarResult(5, Status.PASS, "both official team sheets published for every fixture")


def pillar_6(inputs: FortressInputs, policy: FortressPolicy) -> PillarResult:
    fails, unread, notes = [], [], []
    for ev in inputs.legs:
        leg = ev.leg
        if ev.steam_selections is None:
            unread.append(f"steam on {leg.home} v {leg.away}")
        else:
            against = sorted(s for s in ev.steam_selections if s != leg.selection)
            if against:
                fails.append(f"{_label(ev)}: sharp steam on {', '.join(against)} (the line is moving against it)")
        splits = _section(ev.intel, "public_splits", inputs.now, policy)
        share = ev.public_share
        share_source = "BetDoc's recorded bets" if share is not None else None
        opening = None
        if splits is not None:
            measured = splits.splits.get(leg.market.key, {}).get(leg.selection)
            if measured is not None:
                share, share_source = measured.tickets_pct, splits.source
            opening = splits.opening_odds.get(leg.market.key, {}).get(leg.selection)
        if share is not None and opening is not None:
            drift = ev.quote.odds - opening
            notes.append(f"{_label(ev)}: public {share:.0%} ({share_source}), odds {opening:g} -> {ev.quote.odds:g}")
            if share >= policy.rlm_public_share and drift > 0:
                fails.append(f"{_label(ev)}: reverse line movement, {share:.0%} of tickets on it yet its price lengthened {opening:g} -> {ev.quote.odds:g}")
    if fails:
        return PillarResult(6, Status.FAIL, "; ".join(fails), {"notes": notes})
    if unread:
        return _missing(6, unread)
    return PillarResult(6, Status.PASS, "no adverse steam or reverse line movement" + ("" if notes else " (no measured public split: steam only)"), {"notes": notes})


def pillar_7(inputs: FortressInputs, policy: FortressPolicy, sharp_books: Sequence[str]) -> tuple[PillarResult, float | None]:
    fails, missing, rows = [], [], []
    weakest: float | None = None
    for ev in inputs.legs:
        selections = ev.leg.market.selections
        chosen = None
        for book in sharp_books:
            quotes = [ev.sharp_market.get(s, {}).get(book) for s in selections]
            if all(q is not None and q.age(inputs.now) <= policy.sharp_max_age_seconds for q in quotes):
                chosen = (book, quotes)
                break
        if chosen is None:
            missing.append(f"a fresh complete sharp market for {_label(ev)}")
            continue
        book, quotes = chosen
        fair, z = shin_devig([q.odds for q in quotes])  # type: ignore[union-attr]
        p = fair[selections.index(ev.leg.selection)]
        fair_odds = 1.0 / p
        edge = ev.quote.net_odds / fair_odds - 1.0
        weakest = edge if weakest is None else min(weakest, edge)
        rows.append({"leg": ev.leg.leg_id, "sharp_book": book, "shin_z": round(z, 5), "fair_odds": round(fair_odds, 4), "retail_odds": ev.quote.odds, "edge": round(edge, 4)})
        if edge < policy.min_sharp_edge:
            fails.append(f"{_label(ev)}: {ev.quote.odds:g} at {ev.quote.bookmaker} is {edge:+.2%} over {book}'s fair {fair_odds:.3f} (needs {policy.min_sharp_edge:+.1%})")
    if fails:
        return PillarResult(7, Status.FAIL, "; ".join(fails), {"legs": rows}), weakest
    if missing:
        return _missing(7, missing), weakest
    return PillarResult(7, Status.PASS, f"every leg beats the de-vigged sharp price; weakest edge {weakest:+.2%}", {"legs": rows}), weakest


def pillar_8(inputs: FortressInputs) -> PillarResult:
    trapped = [f"{_label(ev)} (in '{ev.public_traps[0]}')" for ev in inputs.legs if ev.public_traps]
    if trapped:
        return PillarResult(8, Status.FAIL, "leg in a live public-trap parlay: " + "; ".join(trapped))
    return PillarResult(8, Status.PASS, "no leg sits in a live public-trap parlay")


def pillar_9(inputs: FortressInputs, policy: FortressPolicy) -> PillarResult:
    missing, fails, seen = [], [], {}
    for fixture, ev in _by_fixture(inputs.legs).items():
        sport = (ev.leg.sport_key or "").casefold()
        if not any(sport.startswith(prefix) for prefix in policy.referee_sports):
            continue
        ref = _section(ev.intel, "referee", inputs.now, policy)
        if ref is None:
            missing.append(f"referee for {ev.leg.home} v {ev.leg.away}")
            continue
        seen[fixture] = {"name": ref.name, "cards_per_game": ref.cards_per_game, "penalties_per_90": ref.penalties_per_90, "strict": ref.cards_per_game > policy.referee_strict_cards}
        if ref.penalties_per_90 > policy.referee_max_penalties_per_90:
            fails.append(f"{ref.name}: {ref.penalties_per_90:g} penalties per 90 (limit {policy.referee_max_penalties_per_90:g})")
    if fails:
        return PillarResult(9, Status.FAIL, "; ".join(fails), {"referees": seen})
    if missing:
        return _missing(9, missing)
    return PillarResult(9, Status.PASS, "referees known and inside the penalty limit" if seen else "no fixture in a refereed sport", {"referees": seen})


def pillar_10(inputs: FortressInputs, policy: FortressPolicy) -> tuple[PillarResult, dict[str, float]]:
    """Also returns the EV bar per leg (a derby raises it), which pillar 1 applies."""
    missing, fails, bars = [], [], {}
    for ev in inputs.legs:
        mot = _section(ev.intel, "motivation", inputs.now, policy)
        if mot is None:
            missing.append(f"motivation for {ev.leg.home} v {ev.leg.away}")
            continue
        if mot.derby:
            bars[ev.leg.leg_id] = policy.min_ev * policy.derby_ev_multiplier
        side = backed_side(ev.leg.market, ev.leg.selection)
        if side is not None:
            ours, theirs = (mot.home, mot.away) if side == "HOME" else (mot.away, mot.home)
            if theirs - ours >= policy.motivation_gap:
                fails.append(f"{_label(ev)}: the opponent's stake in the result is {theirs:.2f} against {ours:.2f}")
    if fails:
        return PillarResult(10, Status.FAIL, "; ".join(fails), {"derby_bars": bars}), bars
    if missing:
        return _missing(10, missing), bars
    return PillarResult(10, Status.PASS, "no motivation gap against the backed side" + (f"; derby: EV bar raised to {max(bars.values()):+.2%}" if bars else ""), {"derby_bars": bars}), bars


def pillar_11(inputs: FortressInputs, policy: FortressPolicy, stake: Decimal | None) -> PillarResult:
    book = inputs.slip.book
    limits = [inputs.book_max_stake] if inputs.book_max_stake is not None else []
    for ev in _by_fixture(inputs.legs).values():
        liq = _section(ev.intel, "liquidity", inputs.now, policy)
        if liq is not None and book in liq.max_stake_inr:
            limits.append(liq.max_stake_inr[book])
    if not limits:
        return PillarResult(11, Status.UNVERIFIED, f"no known maximum stake at {book} (configure ROUTER_VENUE_STAKE_LIMITS or send liquidity evidence)")
    ceiling = min(limits)
    if stake is None or stake <= 0:
        return PillarResult(11, Status.UNVERIFIED, "no stake to check (sizing did not produce one)", {"max_stake_inr": str(ceiling)})
    if stake > ceiling:
        return PillarResult(11, Status.FAIL, f"stake ₹{stake:,} is over {book}'s ₹{ceiling:,} maximum", {"max_stake_inr": str(ceiling), "stake_inr": str(stake)})
    return PillarResult(11, Status.PASS, f"₹{stake:,} fits under {book}'s ₹{ceiling:,} maximum", {"max_stake_inr": str(ceiling), "stake_inr": str(stake)})


def pillar_12(inputs: FortressInputs) -> PillarResult:
    verdict = inputs.slip.verdict
    if verdict.checks.get("independent", False):
        return PillarResult(12, Status.PASS, "every leg on its own fixture, no team twice inside 36 hours")
    clashes = [r for r in verdict.reasons if r.startswith(("SAME_FIXTURE", "SAME_TEAM", "NEGATIVE_CORRELATION"))]
    return PillarResult(12, Status.FAIL, "; ".join(clashes) or "correlated legs")


def pillar_13(inputs: FortressInputs, policy: FortressPolicy) -> tuple[PillarResult, Sizing | None]:
    if inputs.bankroll is None or inputs.bankroll <= 0:
        return PillarResult(13, Status.UNVERIFIED, "no bankroll to size against (give one, or fund the CFO main account)"), None
    sizing = size_stake(inputs.slip.sim.kelly_fraction, inputs.bankroll, inputs.drawdown, policy)
    metrics = {"full_kelly": round(sizing.full_kelly, 5), "fraction": round(sizing.fraction, 5), "stake_inr": str(sizing.stake),
               "drawdown": round(inputs.drawdown, 4), "scaled": sizing.scaled, "halted": sizing.halted}
    if sizing.halted:
        return PillarResult(13, Status.FAIL, f"rolling drawdown {inputs.drawdown:.1%} is past {policy.drawdown_halt_at:.0%}: betting halts", metrics), sizing
    if sizing.stake <= 0:
        return PillarResult(13, Status.FAIL, f"the Kelly stake rounds below ₹{policy.stake_step}", metrics), sizing
    note = f" (halved: drawdown {inputs.drawdown:.1%} past {policy.drawdown_scale_at:.0%})" if sizing.scaled else ""
    return PillarResult(13, Status.PASS, f"₹{sizing.stake:,} = {sizing.fraction:.2%} of bankroll{note}", metrics), sizing


def pillar_14(kill_switch: bool | None, quotes: Sequence[Quote], now: datetime, policy: FortressPolicy, *, floors: Sequence[float] | None = None) -> PillarResult:
    if kill_switch is None:
        return PillarResult(14, Status.UNVERIFIED, "the kill switch cannot be read (Redis unavailable): nothing executes")
    if kill_switch:
        return PillarResult(14, Status.FAIL, "the kill switch is engaged: trading is halted")
    stale = [f"{q.bookmaker} {q.age(now):.0f}s" for q in quotes if q.age(now) > policy.max_quote_age_seconds]
    if stale:
        return PillarResult(14, Status.FAIL, f"prices older than {policy.max_quote_age_seconds:g}s: {', '.join(stale)}", {"stale": stale})
    if floors is not None:
        drifted = [f"{q.bookmaker} {q.odds:g} < {floor:g}" for q, floor in zip(quotes, floors, strict=True) if q.odds < floor - 1e-9]
        if drifted:
            return PillarResult(14, Status.FAIL, f"odds drifted below the floor: {', '.join(drifted)}", {"drifted": drifted})
    return PillarResult(14, Status.PASS, "kill switch off, every retail price fresh" + (" and at or above its floor" if floors is not None else ""),
                        {"max_age_seconds": round(max((q.age(now) for q in quotes), default=0.0), 1)})


def drift_floors(odds: Sequence[float], policy: FortressPolicy) -> list[float]:
    return [o * (1.0 - policy.max_odds_drift_pct) for o in odds]


def _by_fixture(legs: Sequence[LegEvidence]) -> dict[str, LegEvidence]:
    out: dict[str, LegEvidence] = {}
    for ev in legs:
        out.setdefault(ev.leg.fixture_id, ev)
    return out


# ================================================================ the verdict
def run(inputs: FortressInputs, policy: FortressPolicy, sharp_books: Sequence[str]) -> FortressVerdict:
    p10, bars = pillar_10(inputs, policy)
    p1, consensus = pillar_1(inputs, policy, bars)
    p7, edge = pillar_7(inputs, policy, sharp_books)
    p13, sizing = pillar_13(inputs, policy)
    results = [
        p1, pillar_2(inputs, policy), pillar_3(inputs, policy), pillar_4(inputs, policy), pillar_5(inputs, policy), pillar_6(inputs, policy), p7,
        pillar_8(inputs), pillar_9(inputs, policy), p10, pillar_11(inputs, policy, None if sizing is None else sizing.stake), pillar_12(inputs), p13,
        pillar_14(inputs.kill_switch, [ev.quote for ev in inputs.legs], inputs.now, policy),
    ]
    final: list[PillarResult] = []
    for r in results:
        if r.status is not Status.PASS and r.number in policy.advisory:
            final.append(PillarResult(r.number, Status.ADVISORY, f"[advisory] {r.reason}", r.metrics))
        else:
            final.append(r)
    passed = sum(1 for r in final if r.status is Status.PASS)
    vetted = all(r.status in (Status.PASS, Status.ADVISORY) for r in final)
    reasons = tuple(f"P{r.number} {PILLAR_TITLES[r.number]}: {r.reason}" for r in final if r.status in (Status.FAIL, Status.UNVERIFIED))
    return FortressVerdict(tuple(final), vetted, passed, round(passed / 14 * 100, 2), sizing, consensus, edge, reasons)
