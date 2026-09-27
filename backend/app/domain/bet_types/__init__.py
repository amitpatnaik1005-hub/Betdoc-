"""BetDoc bet types: schemas, calculator, settler, exposure."""
from app.schemas.bet_types import (
    AnyLeg,
    AsianHandicapLeg,
    AsianOverUnderLeg,
    BaseLeg,
    BetStructure,
    CalculatorResult,
    CorrectScoreLeg,
    EachWayLeg,
    ExposureResult,
    GoalscorerLeg,
    HalfTimeFullTimeLeg,
    LegSettlementContext,
    LegStatus,
    MarketType,
    OverUnderLeg,
    SettlementResult,
    SimpleLeg,
)
from app.schemas.bet_structures import (
    NAMED_SYSTEM_BETS,
    AnyBetStructure,
    CanadianBet,
    EachWayBet,
    GoliathBet,
    HeinzBet,
    LayBet,
    Lucky15Bet,
    Lucky31Bet,
    Lucky63Bet,
    NamedSystemBet,
    ParlayBet,
    PatentBet,
    SettleRequest,
    SingleBet,
    SuperHeinzBet,
    SystemBet,
    TrixieBet,
    YankeeBet,
)
from app.domain.bet_types.calculator import (
    calculate_potential_return,
    structure_combinations,
    structure_fold_sizes,
)
from app.domain.bet_types.settler import SettlementError, effective_win_odds, settle_bet
from app.domain.bet_types.asian_lines import settle_asian_handicap, settle_asian_total, split_line
from app.domain.bet_types.exposure import calculate_bet_exposure

__all__ = [
    "MarketType", "BetStructure", "LegStatus",
    "LegSettlementContext", "CalculatorResult", "SettlementResult", "ExposureResult",
    "BaseLeg", "SimpleLeg", "AsianHandicapLeg", "AsianOverUnderLeg", "OverUnderLeg",
    "CorrectScoreLeg", "GoalscorerLeg", "HalfTimeFullTimeLeg", "EachWayLeg", "AnyLeg",
    "SingleBet", "ParlayBet", "SystemBet", "NamedSystemBet", "TrixieBet", "PatentBet",
    "YankeeBet", "Lucky15Bet", "CanadianBet", "Lucky31Bet", "HeinzBet", "Lucky63Bet",
    "SuperHeinzBet", "GoliathBet", "EachWayBet", "LayBet", "AnyBetStructure",
    "NAMED_SYSTEM_BETS", "SettleRequest",
    "calculate_potential_return", "structure_combinations", "structure_fold_sizes",
    "settle_bet", "effective_win_odds", "SettlementError",
    "settle_asian_handicap", "settle_asian_total", "split_line",
    "calculate_bet_exposure",
]
