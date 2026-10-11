"""Leg settlement shared by every sport adapter (Group 79). Developed for Amit Ashok Kumar Patnaik.

A sport's raw result dict is first normalised into a ``NormalizedResult`` (final score, regulation
score, period scores, events, player statistics, sport extras). The generic markets (moneyline, 3-way,
handicap and Asian handicap with quarter balls, totals, team totals, period lines, draw no bet, BTTS,
correct score, clean sheet, double chance, player props, next event, alternative lines) settle here,
once, for all nine sports. Each settles to a ``LegStatus``: WON, LOST, VOID (push, abandoned, player
scratched), HALF_WON or HALF_LOST (a quarter line split across two adjacent lines).

Conventions:

* ``line`` on a handicap is the handicap given to the *selected* side (home -1.5 wins when
  home - away - 1.5 > 0); on a total it is the threshold.
* A whole line that lands exactly pushes (VOID); a quarter line settles half on each adjacent line.
* A 2-way moneyline in a sport whose game cannot end level (or ends level only in a void tie, NFL)
  settles a level result VOID; in a 3-way sport the draw is a selection and a backed side LOSES on it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.domain.sports.quant import split_line
from app.schemas.bet_types import LegStatus

# ================================================================ canonical market keys
MONEYLINE = "moneyline"
THREE_WAY = "three_way"
SPREAD = "spread"
ASIAN_HANDICAP = "asian_handicap"
ALT_SPREAD = "alt_spread"
TOTAL = "total"
ALT_TOTAL = "alt_total"
TEAM_TOTAL = "team_total"
PERIOD_MONEYLINE = "period_moneyline"
PERIOD_SPREAD = "period_spread"
PERIOD_TOTAL = "period_total"
DRAW_NO_BET = "draw_no_bet"
BTTS = "btts"
CORRECT_SCORE = "correct_score"
CLEAN_SHEET = "clean_sheet"
DOUBLE_CHANCE = "double_chance"
PLAYER_PROP = "player_prop"
NEXT_EVENT = "next_event"

GENERIC_MARKETS: frozenset[str] = frozenset({
    MONEYLINE, THREE_WAY, SPREAD, ASIAN_HANDICAP, ALT_SPREAD, TOTAL, ALT_TOTAL, TEAM_TOTAL, PERIOD_MONEYLINE,
    PERIOD_SPREAD, PERIOD_TOTAL, DRAW_NO_BET, BTTS, CORRECT_SCORE, CLEAN_SHEET, DOUBLE_CHANCE, PLAYER_PROP, NEXT_EVENT,
})

HOME_TOKENS = frozenset({"home", "1", "team_a", "a", "player_a", "fighter_a", "home_team"})
AWAY_TOKENS = frozenset({"away", "2", "team_b", "b", "player_b", "fighter_b", "away_team"})
DRAW_TOKENS = frozenset({"draw", "x", "tie", "level"})
VOID_STATUSES = frozenset({"abandoned", "cancelled", "canceled", "postponed", "void", "no_contest", "nc"})


class SettlementInputError(ValueError):
    """The result cannot settle the market (missing statistics, unknown selection)."""


def norm(value: Any) -> str:
    return ("" if value is None else str(value)).strip().casefold().replace(" ", "_").replace("-", "_")


def num(mapping: Mapping[str, Any], *keys: str, default: float | None = None) -> float | None:
    """The first present, finite numeric value among ``keys``."""
    for key in keys:
        if key in mapping and mapping[key] is not None:
            try:
                value = float(mapping[key])
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                return value
    return default


# ================================================================ the normalised result
@dataclass
class NormalizedResult:
    home: float | None = None
    away: float | None = None
    regulation_home: float | None = None
    regulation_away: float | None = None
    home_name: str = ""
    away_name: str = ""
    winner: str | None = None  # "home" | "away" | "draw" | None
    winner_name: str = ""  # a named winner the result could not map to a side
    periods: dict[str, tuple[float, float]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    player_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    participants: set[str] | None = None  # players who took part; None: unknown (everyone counts)
    extras: dict[str, Any] = field(default_factory=dict)
    void: bool = False

    def side_winner(self) -> str | None:
        if self.winner is not None:
            return self.winner
        if self.home is None or self.away is None:
            return None
        return "home" if self.home > self.away else "away" if self.away > self.home else "draw"


def normalize_basic(raw: Mapping[str, Any], *, home_keys: tuple[str, ...], away_keys: tuple[str, ...]) -> NormalizedResult:
    """The fields every sport shares; adapters add their own extras on top."""
    res = NormalizedResult()
    res.void = norm(raw.get("status")) in VOID_STATUSES
    res.home = num(raw, *home_keys)
    res.away = num(raw, *away_keys)
    res.regulation_home = num(raw, "reg_home_score", "regulation_home", "regulation_home_score")
    res.regulation_away = num(raw, "reg_away_score", "regulation_away", "regulation_away_score")
    res.home_name = norm(raw.get("home_name") or raw.get("home_team"))
    res.away_name = norm(raw.get("away_name") or raw.get("away_team"))
    winner = norm(raw.get("winner"))
    if winner:
        side = side_token(winner, res)
        if side is not None:
            res.winner = side
        else:
            res.winner_name = winner
    periods = raw.get("periods") or {}
    if isinstance(periods, Mapping):
        for key, value in periods.items():
            pair = _pair(value)
            if pair is not None:
                res.periods[norm(key)] = pair
    events = raw.get("events") or []
    if isinstance(events, list):
        res.events = [e for e in events if isinstance(e, Mapping)]
    stats = raw.get("player_stats") or {}
    if isinstance(stats, Mapping):
        res.player_stats = {norm(p): {norm(k): float(v) for k, v in s.items() if _finite(v)} for p, s in stats.items() if isinstance(s, Mapping)}
    if isinstance(raw.get("participants"), list):
        res.participants = {norm(p) for p in raw["participants"]}
    return res


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _pair(value: Any) -> tuple[float, float] | None:
    if isinstance(value, Mapping):
        h, a = num(value, "home", "h", "home_score"), num(value, "away", "a", "away_score")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        h, a = (float(value[0]) if _finite(value[0]) else None), (float(value[1]) if _finite(value[1]) else None)
    else:
        return None
    return None if h is None or a is None else (h, a)


def side_token(selection: str, result: NormalizedResult | None = None) -> str | None:
    s = norm(selection)
    if s in HOME_TOKENS:
        return "home"
    if s in AWAY_TOKENS:
        return "away"
    if s in DRAW_TOKENS:
        return "draw"
    if result is not None:
        if result.home_name and s == result.home_name:
            return "home"
        if result.away_name and s == result.away_name:
            return "away"
    return None


def split_selection(selection: str) -> list[str]:
    """``"home_over"``, ``"home over"``, ``"home:over"`` -> ["home", "over"]."""
    s = str(selection or "").strip().casefold()
    for sep in (":", "|", " ", "_"):
        s = s.replace(sep, ":")
    return [p for p in s.split(":") if p]


# ================================================================ primitive settlements
def _status_from(results: list[int]) -> LegStatus:
    if len(results) == 1:
        return {1: LegStatus.WON, 0: LegStatus.VOID, -1: LegStatus.LOST}[results[0]]
    a, b = results
    if a == b:
        return {1: LegStatus.WON, 0: LegStatus.VOID, -1: LegStatus.LOST}[a]
    total = a + b
    if total == 1:
        return LegStatus.HALF_WON
    if total == -1:
        return LegStatus.HALF_LOST
    return LegStatus.VOID


def _sgn(x: float) -> int:
    return 1 if x > 1e-9 else -1 if x < -1e-9 else 0


def settle_threshold(value: float, line: float, *, over: bool) -> LegStatus:
    """Over/under ``line`` (quarter lines split, whole lines push)."""
    sign = 1.0 if over else -1.0
    return _status_from([_sgn(sign * (value - h)) for h in split_line(line)])


def settle_handicap(team: float, opponent: float, handicap: float) -> LegStatus:
    """The selected team with ``handicap`` added to its score."""
    return _status_from([_sgn(team - opponent + h) for h in split_line(handicap)])


def over_under(selection: str) -> bool:
    tokens = split_selection(selection)
    if "over" in tokens:
        return True
    if "under" in tokens:
        return False
    raise SettlementInputError(f"selection {selection!r} names neither over nor under")


def yes_no(selection: str) -> bool:
    tokens = split_selection(selection)
    if "yes" in tokens or "y" in tokens:
        return True
    if "no" in tokens or "n" in tokens:
        return False
    raise SettlementInputError(f"selection {selection!r} names neither yes nor no")


def _scores(result: NormalizedResult, period: str | None) -> tuple[float, float]:
    if period:
        pair = result.periods.get(norm(period))
        if pair is None:
            raise SettlementInputError(f"the result has no {period!r} period score")
        return pair
    if result.home is None or result.away is None:
        raise SettlementInputError("the result has no final score")
    return result.home, result.away


def _side_of(selection: str, result: NormalizedResult) -> str:
    for token in [selection, *split_selection(selection)]:
        side = side_token(token, result)
        if side is not None:
            return side
    raise SettlementInputError(f"selection {selection!r} names no side")


def _team_scores(side: str, home: float, away: float) -> tuple[float, float]:
    return (home, away) if side == "home" else (away, home)


# ================================================================ generic market dispatch
def settle_generic(market: str, result: NormalizedResult, selection: str, line: float | None, params: Mapping[str, Any],
                   *, has_draw: bool) -> LegStatus:
    """Settles one of ``GENERIC_MARKETS``. Raises ``SettlementInputError`` when the result cannot settle it."""
    if result.void:
        return LegStatus.VOID
    period = params.get("period")

    if market == MONEYLINE:
        return _moneyline(result, selection, has_draw=has_draw, period=period)
    if market == THREE_WAY:
        home, away = (result.regulation_home, result.regulation_away)
        if home is None or away is None:
            if not has_draw and not period:
                # an overtime sport's final score cannot tell a regulation win from an overtime one
                raise SettlementInputError("a 3-way market in a sport with overtime needs the regulation score")
            if not period and result.winner is not None:
                side = _side_of(selection, result)
                return LegStatus.WON if side == result.winner else LegStatus.LOST
            home, away = _scores(result, period)
        side = _side_of(selection, result)
        actual = "home" if home > away else "away" if away > home else "draw"
        return LegStatus.WON if side == actual else LegStatus.LOST
    if market in (SPREAD, ASIAN_HANDICAP, ALT_SPREAD, PERIOD_SPREAD):
        if line is None:
            raise SettlementInputError(f"{market} needs a line")
        home, away = _scores(result, period)
        side = _side_of(selection, result)
        if side == "draw":
            raise SettlementInputError("a handicap has no draw selection")
        team, opp = _team_scores(side, home, away)
        return settle_handicap(team, opp, line)
    if market in (TOTAL, ALT_TOTAL, PERIOD_TOTAL):
        if line is None:
            raise SettlementInputError(f"{market} needs a line")
        home, away = _scores(result, period)
        return settle_threshold(home + away, line, over=over_under(selection))
    if market == TEAM_TOTAL:
        if line is None:
            raise SettlementInputError("a team total needs a line")
        home, away = _scores(result, period)
        side = _side_of(selection, result)
        return settle_threshold(home if side == "home" else away, line, over=over_under(selection))
    if market == PERIOD_MONEYLINE:
        if not period:
            raise SettlementInputError("a period line needs params.period")
        return _moneyline(result, selection, has_draw=True, period=period, level_is_void=bool(params.get("level_is_void")))
    if market == DRAW_NO_BET:
        home, away = _scores(result, period)
        if home == away:
            return LegStatus.VOID
        side = _side_of(selection, result)
        return LegStatus.WON if (home > away) == (side == "home") else LegStatus.LOST
    if market == DOUBLE_CHANCE:
        home, away = _scores(result, period)
        actual = "1" if home > away else "2" if away > home else "x"
        picks = _double_chance(selection)
        return LegStatus.WON if actual in picks else LegStatus.LOST
    if market == BTTS:
        home, away = _scores(result, period)
        both = home > 0 and away > 0
        return LegStatus.WON if both == yes_no(selection) else LegStatus.LOST
    if market == CORRECT_SCORE:
        home, away = _scores(result, period)
        want = _score_pair(selection)
        return LegStatus.WON if want == (int(home), int(away)) else LegStatus.LOST
    if market == CLEAN_SHEET:
        home, away = _scores(result, period)
        side = _side_of(selection, result)
        conceded = away if side == "home" else home
        tokens = split_selection(selection)
        wants_yes = "no" not in tokens
        return LegStatus.WON if (conceded == 0) == wants_yes else LegStatus.LOST
    if market == PLAYER_PROP:
        return settle_player_prop(result, selection, line, params)
    if market == NEXT_EVENT:
        return settle_next_event(result, selection, params)
    raise SettlementInputError(f"unknown generic market {market!r}")


def _moneyline(result: NormalizedResult, selection: str, *, has_draw: bool, period: str | None = None,
               level_is_void: bool = False) -> LegStatus:
    if not period and result.winner is None and result.winner_name:
        named = side_token(norm(selection), result)
        if named is None or result.home is None or result.away is None:
            return LegStatus.WON if norm(selection) == result.winner_name else LegStatus.LOST
    side = _side_of(selection, result)
    if period:
        home, away = _scores(result, period)
        actual = "home" if home > away else "away" if away > home else "draw"
    else:
        actual = result.side_winner()
        if actual is None:
            raise SettlementInputError("the result names no winner and no score")
    if actual == "draw":
        if side == "draw":
            return LegStatus.WON
        return LegStatus.LOST if (has_draw and not level_is_void) else LegStatus.VOID
    return LegStatus.WON if side == actual else LegStatus.LOST


def _double_chance(selection: str) -> set[str]:
    s = norm(selection).replace("_", "")
    mapping = {"1x": {"1", "x"}, "x1": {"1", "x"}, "12": {"1", "2"}, "21": {"1", "2"}, "x2": {"x", "2"}, "2x": {"x", "2"},
               "homedraw": {"1", "x"}, "homeaway": {"1", "2"}, "drawaway": {"x", "2"}, "awaydraw": {"x", "2"}}
    if s not in mapping:
        raise SettlementInputError(f"double chance selection {selection!r} is not 1X, 12 or X2")
    return mapping[s]


def _score_pair(selection: str) -> tuple[int, int]:
    s = str(selection).strip().replace(":", "-").replace(" ", "")
    parts = s.split("-")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise SettlementInputError(f"correct score selection {selection!r} is not 'H-A'")
    return int(parts[0]), int(parts[1])


def settle_player_prop(result: NormalizedResult, selection: str, line: float | None, params: Mapping[str, Any]) -> LegStatus:
    """Player prop over/under (runs, points, rebounds, assists, hits, strikeouts, passing yards...).
    PLAYER SCRATCH RULE: a player who did not take part voids the leg."""
    player = norm(params.get("player"))
    stat = norm(params.get("stat"))
    if not player or not stat:
        raise SettlementInputError("a player prop needs params.player and params.stat")
    if line is None:
        raise SettlementInputError("a player prop needs a line")
    if result.participants is not None and player not in result.participants:
        return LegStatus.VOID
    stats = result.player_stats.get(player)
    if stats is None:
        if result.participants is None:
            return LegStatus.VOID  # no box score line for the player: did not play
        stats = {}
    value = stats.get(stat, 0.0)
    return settle_threshold(value, line, over=over_under(selection))


def settle_next_event(result: NormalizedResult, selection: str, params: Mapping[str, Any]) -> LegStatus:
    """The next event of ``params.event_type`` after ``params.after`` (a match clock or sequence number). The
    selection is a side, or ``none`` when no such event follows."""
    kind = norm(params.get("event_type") or "score")
    after = float(params.get("after", -math.inf))
    following = [e for e in result.events if norm(e.get("type")) == kind and float(e.get("t", e.get("minute", 0.0))) > after]
    following.sort(key=lambda e: float(e.get("t", e.get("minute", 0.0))))
    want = norm(selection)
    if not following:
        return LegStatus.WON if want in ("none", "no_goal", "no_event", "neither") else LegStatus.LOST
    team = side_token(norm(following[0].get("team")), result) or norm(following[0].get("team"))
    pick = side_token(want, result) or want
    return LegStatus.WON if team == pick else LegStatus.LOST


def is_win(status: LegStatus) -> bool:
    return status in (LegStatus.WON, LegStatus.HALF_WON)
