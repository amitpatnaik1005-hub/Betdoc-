from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.dashboard._common import align_to_column, to_float, utc_now
from app.models import BetLedger, ExchangeAccount
from app.models.market_signals import MarketTickModel
from app.schemas.dashboard import TipRecommendation

FALLBACK_MARKETS: tuple[str, ...] = ("MATCH_WINNER_1X2", "OVER_UNDER_GOALS")
# Strictly binary outcome markets (win/lose only, no push/half outcomes).
BINARY_MARKETS: frozenset[str] = frozenset({"MATCH_WINNER_1X2", "BOTH_TEAMS_TO_SCORE", "MONEYLINE"})
ASIAN_MARKET_KEYWORDS: tuple[str, ...] = ("ASIAN", "HANDICAP", "TOTAL", "OVER_UNDER")

ODDS_TYPE_BACK = "BACK"
ACTIVE_TICK_WINDOW_MINUTES = 60
TICK_FETCH_LIMIT = 2000
MIN_DECIMAL_ODDS = 1.5
MAX_SINGLE_TIPS = 3
MAX_PARLAY_LEGS = 3
MIN_PARLAY_LEGS = 2
QUARTER_KELLY = 0.25
MOCK_MIN_EDGE = 0.02  # relative edge over implied probability
MOCK_MAX_EDGE = 0.06
MAX_TRUE_PROBABILITY = 0.99


def kelly_fraction(p: float, decimal_odds: float) -> float:
    """f* = p - (1 - p) / (O - 1). Returns 0.0 for invalid inputs."""
    b = decimal_odds - 1.0
    if b <= 0 or not (0.0 < p < 1.0) or not math.isfinite(decimal_odds):
        return 0.0
    return p - (1.0 - p) / b


def stable_tip_id(match_id: str, selection_id: str, market_type: str) -> str:
    return hashlib.sha256(f"{match_id}|{selection_id}|{market_type}".encode()).hexdigest()


def is_strictly_binary(market_type: str, line: float | None) -> bool:
    mt = market_type.upper()
    if any(keyword in mt for keyword in ASIAN_MARKET_KEYWORDS):
        return False
    return mt in BINARY_MARKETS and line is None


def _mock_true_probability(tip_id: str, line: float | None, decimal_odds: float) -> float:
    """Deterministic mock 'sharp' probability: implied * (1 + edge), edge in [2%, 6%]."""
    digest = hashlib.sha256(f"{tip_id}|{line}".encode()).hexdigest()
    unit = int(digest[:8], 16) / 0xFFFFFFFF
    edge = MOCK_MIN_EDGE + unit * (MOCK_MAX_EDGE - MOCK_MIN_EDGE)
    return min((1.0 / decimal_odds) * (1.0 + edge), MAX_TRUE_PROBABILITY)


@dataclass(frozen=True, slots=True)
class _Candidate:
    tip_id: str
    match_id: str
    selection_id: str
    market_type: str
    line: float | None
    bookmaker_id: str
    decimal_odds: float
    true_probability: float
    kelly_full: float
    affinity: int

    @property
    def parlay_eligible(self) -> bool:
        return is_strictly_binary(self.market_type, self.line)

    @property
    def expected_value(self) -> float:
        return self.true_probability * self.decimal_odds - 1.0


class TipMasterEngine:
    async def generate_personalized_tips(self, user_id: UUID, db: AsyncSession) -> list[TipRecommendation]:
        profile = await self._profile_user(user_id, db)
        candidates = await self._detect_edges(db, profile)
        if not candidates:
            return []

        ranked = sorted(candidates, key=lambda c: (-c.affinity, -c.kelly_full, c.tip_id))
        singles: list[_Candidate] = []
        seen_matches: set[str] = set()
        for cand in ranked:
            if cand.match_id in seen_matches:
                continue
            singles.append(cand)
            seen_matches.add(cand.match_id)
            if len(singles) >= MAX_SINGLE_TIPS:
                break

        tips = [self._single_tip(c, profile) for c in singles]
        parlay = self._build_parlay(candidates)
        if parlay is not None:
            tips.append(parlay)
        return tips

    async def _profile_user(self, user_id: UUID, db: AsyncSession) -> dict[str, int]:
        count_expr = func.count(BetLedger.id)
        stmt = (
            select(BetLedger.market_type, count_expr.label("n"))
            .join(ExchangeAccount, BetLedger.exchange_account_id == ExchangeAccount.id)
            .where(ExchangeAccount.user_id == user_id, BetLedger.market_type.is_not(None))
            .group_by(BetLedger.market_type)
            .order_by(count_expr.desc(), BetLedger.market_type)
        )
        profile = {str(mt): int(n) for mt, n in (await db.execute(stmt)).all()}
        return profile or {mt: 0 for mt in FALLBACK_MARKETS}

    async def _detect_edges(self, db: AsyncSession, profile: dict[str, int]) -> list[_Candidate]:
        markets = sorted(set(profile) | BINARY_MARKETS)
        window_start = align_to_column(
            utc_now() - timedelta(minutes=ACTIVE_TICK_WINDOW_MINUTES), MarketTickModel.timestamp
        )
        stmt = (
            select(MarketTickModel)
            .where(
                MarketTickModel.odds_type == ODDS_TYPE_BACK,
                MarketTickModel.market_type.in_(markets),
                MarketTickModel.timestamp >= window_start,
            )
            .order_by(MarketTickModel.timestamp.desc(), MarketTickModel.bookmaker_id)
            .limit(TICK_FETCH_LIMIT)
        )
        ticks = (await db.execute(stmt)).scalars().all()

        # Latest price per bookmaker/outcome/line (rows are newest-first).
        latest: dict[tuple, MarketTickModel] = {}
        for t in ticks:
            if not (t.match_id and t.selection_id and t.market_type and t.bookmaker_id):
                continue
            line = None if t.line is None else to_float(t.line)
            key = (t.bookmaker_id, t.match_id, t.market_type, t.selection_id, line)
            latest.setdefault(key, t)

        best: dict[str, _Candidate] = {}
        for t in latest.values():
            odds = to_float(t.decimal_odds)
            if not math.isfinite(odds) or odds <= MIN_DECIMAL_ODDS:
                continue
            line = None if t.line is None else to_float(t.line)
            tip_id = stable_tip_id(t.match_id, t.selection_id, t.market_type)
            p = _mock_true_probability(tip_id, line, odds)
            f_star = kelly_fraction(p, odds)
            if f_star <= 0:  # -EV guard
                continue
            cand = _Candidate(
                tip_id=tip_id,
                match_id=str(t.match_id),
                selection_id=str(t.selection_id),
                market_type=str(t.market_type),
                line=line,
                bookmaker_id=str(t.bookmaker_id),
                decimal_odds=odds,
                true_probability=p,
                kelly_full=f_star,
                affinity=profile.get(t.market_type, 0),
            )
            current = best.get(tip_id)
            if current is None or (cand.kelly_full, cand.decimal_odds, cand.bookmaker_id) > (
                current.kelly_full, current.decimal_odds, current.bookmaker_id
            ):
                best[tip_id] = cand
        return list(best.values())

    @staticmethod
    def _single_tip(c: _Candidate, profile: dict[str, int]) -> TipRecommendation:
        stake_pct = round(QUARTER_KELLY * c.kelly_full * 100.0, 4)
        line_txt = f" (line {c.line:+g})" if c.line is not None else ""
        rationale = (
            f"Matches your {c.market_type} profile ({profile.get(c.market_type, 0)} historical bets). "
            f"Best BACK {c.decimal_odds:.2f} at {c.bookmaker_id}{line_txt}; model p={c.true_probability:.1%} "
            f"vs implied {1 / c.decimal_odds:.1%} (EV {c.expected_value:+.1%}). "
            f"Quarter Kelly: {stake_pct:.2f}% of bankroll."
        )
        return TipRecommendation(
            tip_id=c.tip_id,
            match_ids=[c.match_id],
            market_types=[c.market_type],
            selections=[c.selection_id],
            recommended_structure="SINGLE",
            is_parlay=False,
            confidence_score_pct=round(c.true_probability * 100.0, 2),
            rationale=rationale,
            kelly_stake_pct=stake_pct,
        )

    @staticmethod
    def _build_parlay(candidates: list[_Candidate]) -> TipRecommendation | None:
        binary = sorted(
            (c for c in candidates if c.parlay_eligible),  # Asian markets excluded
            key=lambda c: (-c.kelly_full, c.tip_id),
        )
        legs: list[_Candidate] = []
        used_matches: set[str] = set()
        for c in binary:
            if c.match_id in used_matches:  # uncorrelated: one leg per match
                continue
            legs.append(c)
            used_matches.add(c.match_id)
            if len(legs) == MAX_PARLAY_LEGS:
                break
        if len(legs) < MIN_PARLAY_LEGS:
            return None

        combined_odds = math.prod(l.decimal_odds for l in legs)
        combined_p = math.prod(l.true_probability for l in legs)
        f_star = kelly_fraction(combined_p, combined_odds)
        if f_star <= 0:
            return None
        stake_pct = round(QUARTER_KELLY * f_star * 100.0, 4)

        legs = sorted(legs, key=lambda l: l.tip_id)  # stable ordering
        tip_id = hashlib.sha256(
            ("PARLAY|" + "||".join(f"{l.match_id}|{l.selection_id}|{l.market_type}" for l in legs)).encode()
        ).hexdigest()
        rationale = (
            f"{len(legs)}-leg parlay of uncorrelated binary +EV singles. Combined odds {combined_odds:.2f}, "
            f"joint p={combined_p:.1%} vs implied {1 / combined_odds:.1%} "
            f"(EV {combined_p * combined_odds - 1:+.1%}). Quarter Kelly: {stake_pct:.2f}% of bankroll."
        )
        return TipRecommendation(
            tip_id=tip_id,
            match_ids=[l.match_id for l in legs],
            market_types=[l.market_type for l in legs],
            selections=[l.selection_id for l in legs],
            recommended_structure="PARLAY",
            is_parlay=True,
            confidence_score_pct=round(combined_p * 100.0, 2),
            rationale=rationale,
            kelly_stake_pct=stake_pct,
        )
