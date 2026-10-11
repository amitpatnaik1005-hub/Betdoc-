"""The 27 universal bet types across the nine sports (Group 79). Developed for Amit Ashok Kumar Patnaik.

Single-leg *market* types settle through a sport adapter's ``settle_market``; *structure* types (parlay,
teaser, pleaser, round robin, if-bet / action reverse, double action, futures) combine legs and settle in
``app.domain.sports.structures``. Each market type names the market keys it reaches in each sport, so the
desk (and the resolver API) can ask for "the point spread" without knowing that baseball calls it the run
line and hockey the puck line.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class BetType(StrEnum):
    MONEYLINE = "moneyline"
    POINT_SPREAD = "point_spread"
    GAME_TOTAL = "game_total"
    TEAM_TOTAL = "team_total"
    PERIOD_LINES = "period_lines"
    PARLAY = "parlay"
    TEASER = "teaser"
    PLEASER = "pleaser"
    ROUND_ROBIN = "round_robin"
    IF_BET = "if_bet"
    DOUBLE_ACTION = "double_action"
    FUTURES = "futures"
    PLAYER_PROPS = "player_props"
    GAME_PROPS = "game_props"
    LIVE_NEXT_EVENT = "live_next_event"
    DRAW_NO_BET = "draw_no_bet"
    BTTS = "btts"
    CORRECT_SCORE = "correct_score"
    ASIAN_HANDICAP = "asian_handicap"
    CLEAN_SHEET = "clean_sheet"
    METHOD_OF_VICTORY = "method_of_victory"
    ROUND_BETTING = "round_betting"
    MAP_WINNER = "map_winner"
    TOTAL_MAPS = "total_maps"
    FIRST_5_INNINGS = "first_5_innings"
    INNING_OF_HIGHEST_SCORE = "inning_of_highest_score"
    ALTERNATIVE_LINES = "alternative_lines"


STRUCTURE_TYPES: frozenset[BetType] = frozenset({
    BetType.PARLAY, BetType.TEASER, BetType.PLEASER, BetType.ROUND_ROBIN, BetType.IF_BET, BetType.DOUBLE_ACTION, BetType.FUTURES,
})


@dataclass(frozen=True, slots=True)
class BetTypeInfo:
    key: BetType
    label: str
    kind: str  # "market" | "structure"
    description: str
    markets: dict[str, tuple[str, ...]]  # sport -> the adapter market keys that implement it ("*": every sport offering it)


_ALL = "*"

CATALOG: dict[BetType, BetTypeInfo] = {
    info.key: info for info in (
        BetTypeInfo(BetType.MONEYLINE, "Moneyline / 1X2 / Match Winner", "market", "Who wins: 2-way, or 3-way where a draw is an outcome.",
                    {_ALL: ("moneyline",), "soccer": ("1x2_match_odds",), "ice_hockey": ("moneyline_incl_ot", "regulation_3_way"),
                     "esports": ("match_winner_series",), "cricket": ("match_winner",), "tennis": ("match_winner",)}),
        BetTypeInfo(BetType.POINT_SPREAD, "Point Spread / Run Line / Puck Line / Handicap", "market",
                    "The selected side with a handicap added; whole lines push.",
                    {_ALL: ("spread",), "basketball": ("point_spread",), "american_football": ("point_spread",), "baseball": ("run_line_handicap",),
                     "ice_hockey": ("puck_line_handicap",), "tennis": ("game_handicap",), "esports": ("map_handicap",)}),
        BetTypeInfo(BetType.GAME_TOTAL, "Game Total (Over/Under)", "market", "Combined score over or under the line.",
                    {_ALL: ("total",), "basketball": ("game_total_over_under",), "american_football": ("game_total_over_under",),
                     "baseball": ("game_total_runs",), "ice_hockey": ("game_total_goals",), "soccer": ("over_under_2_5_goals",),
                     "tennis": ("total_games_over_under",), "cricket": ("innings_total_runs",), "combat": ("round_totals_over_under",)}),
        BetTypeInfo(BetType.TEAM_TOTAL, "Team Total (Over/Under)", "market", "One side's score over or under the line.",
                    {_ALL: ("team_total",), "basketball": ("team_totals",), "american_football": ("team_total_points",)}),
        BetTypeInfo(BetType.PERIOD_LINES, "First Half / First Inning / First Quarter / Period Lines", "market",
                    "Moneyline, handicap or total on a period (params.period: h1, q1, p1, i1, f5, set1, map1).",
                    {_ALL: ("period_moneyline", "period_spread", "period_total"), "basketball": ("quarter_winner",),
                     "american_football": ("first_half_spread",), "tennis": ("first_set_winner",), "soccer": ("first_half_result",)}),
        BetTypeInfo(BetType.PARLAY, "Parlay / Accumulator", "structure", "Every leg must win; a void leg drops out at odds 1.", {}),
        BetTypeInfo(BetType.TEASER, "Teaser (6.0 / 6.5 / 7.0 points)", "structure",
                    "Spread and total legs moved in the bettor's favour; fixed payout table; a push removes the leg.", {}),
        BetTypeInfo(BetType.PLEASER, "Pleaser", "structure", "Spread and total legs moved against the bettor for a big payout; a push loses.", {}),
        BetTypeInfo(BetType.ROUND_ROBIN, "Round Robin", "structure", "Every parlay of the chosen sizes from the legs, one unit each.", {}),
        BetTypeInfo(BetType.IF_BET, "If-Bet (Action Reverse)", "structure",
                    "Singles in order; the next has action only if the previous won. Action reverse runs the order both ways.", {}),
        BetTypeInfo(BetType.DOUBLE_ACTION, "Double Action", "structure", "An if-bet that continues on a win, a push or a cancellation.", {}),
        BetTypeInfo(BetType.FUTURES, "Futures / Outrights", "structure", "A selection from a field; dead heats divide the stake.", {}),
        BetTypeInfo(BetType.PLAYER_PROPS, "Player Props", "market",
                    "Runs, points, rebounds, assists, hits, strikeouts, passing yards... over/under; a non-participant voids.",
                    {_ALL: ("player_prop",)}),
        BetTypeInfo(BetType.GAME_PROPS, "Game Props", "market", "NRFI/YRFI, first score method, first blood, most sixes, empty-net goal.",
                    {"baseball": ("nrfi_yrfi_first_inning",), "american_football": ("first_score_method",), "basketball": ("first_team_to_score",),
                     "esports": ("first_blood", "pistol_round_winner"),
                     "cricket": ("most_sixes",), "ice_hockey": ("empty_net_goal_yes_no",), "combat": ("fight_goes_the_distance",),
                     "tennis": ("tiebreak_in_match",)}),
        BetTypeInfo(BetType.LIVE_NEXT_EVENT, "Live In-Play Next Event", "market", "The side to produce the next event of a type after a moment.",
                    {_ALL: ("next_event",)}),
        BetTypeInfo(BetType.DRAW_NO_BET, "Draw No Bet", "market", "A draw refunds the stake.", {_ALL: ("draw_no_bet",)}),
        BetTypeInfo(BetType.BTTS, "Both Teams To Score", "market", "Each side scores at least once.", {_ALL: ("btts",), "soccer": ("both_teams_to_score",)}),
        BetTypeInfo(BetType.CORRECT_SCORE, "Correct Score / Set Betting", "market", "The exact final score (sets in tennis, maps in esports).",
                    {_ALL: ("correct_score",), "tennis": ("set_betting_correct_score",)}),
        BetTypeInfo(BetType.ASIAN_HANDICAP, "Asian Handicap (quarter / half ball)", "market",
                    "Handicaps on the quarter ball split the stake over the two adjacent lines.", {_ALL: ("asian_handicap",)}),
        BetTypeInfo(BetType.CLEAN_SHEET, "Clean Sheet", "market", "The side concedes nothing.", {_ALL: ("clean_sheet",)}),
        BetTypeInfo(BetType.METHOD_OF_VICTORY, "Method of Victory (KO/TKO, Submission, Decision)", "market", "How the fight ends.",
                    {"combat": ("method_of_victory",)}),
        BetTypeInfo(BetType.ROUND_BETTING, "Round Betting", "market", "Which fighter wins in which round (or by decision).", {"combat": ("round_betting",)}),
        BetTypeInfo(BetType.MAP_WINNER, "Map Winner", "market", "The winner of a given map.", {"esports": ("map_winner", "map_1_winner")}),
        BetTypeInfo(BetType.TOTAL_MAPS, "Total Maps Over/Under", "market", "Maps played in the series.", {"esports": ("map_total_over_under",)}),
        BetTypeInfo(BetType.FIRST_5_INNINGS, "First 5 Innings (F5)", "market", "Moneyline, run line or total over the first five innings.",
                    {"baseball": ("first_5_innings_winner", "first_5_innings_run_line", "first_5_innings_total")}),
        BetTypeInfo(BetType.INNING_OF_HIGHEST_SCORE, "Inning of Highest Score", "market", "The inning (or innings) with the most runs; a shared top is a tie.",
                    {"baseball": ("inning_of_highest_score",), "cricket": ("inning_of_highest_score",)}),
        BetTypeInfo(BetType.ALTERNATIVE_LINES, "Alternative Lines / Derivative Markets", "market", "Any handicap or total off the main line.",
                    {_ALL: ("alt_spread", "alt_total")}),
    )
}

if len(CATALOG) != 27:  # import-time integrity check
    raise RuntimeError(f"the universal catalogue must hold 27 bet types, holds {len(CATALOG)}")


def markets_for(bet_type: BetType, sport_key: str) -> tuple[str, ...]:
    """The adapter market keys implementing ``bet_type`` in ``sport_key`` (sport-specific names first)."""
    info = CATALOG[bet_type]
    specific = info.markets.get(sport_key, ())
    generic = info.markets.get(_ALL, ())
    return tuple(dict.fromkeys((*specific, *generic)))
