from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from betdoc.domain.intelligence.advisor_models import MarketOpportunity
from betdoc.domain.intelligence.session_oracle import MarketHealthScore, SessionIntelligenceOracle
from betdoc.infrastructure.ledger.paper_ledger import PaperLedger, WalletSnapshot

ModelName = Literal["System AI", "Custom Aggressive"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class OddsTick(Contract):
    market_id: str
    team_home: str
    team_away: str
    market_type: Literal["spread", "moneyline", "total"]
    sportsbook_odds: float
    implied_probability: float
    model_win_chance: float
    edge_percentage: float


class BookLine(Contract):
    sportsbook: str
    decimal_odds: float | None
    quoted_at: str | None


class BoardMarket(OddsTick):
    selection_label: str
    fixture_id: str
    lines: list[BookLine]
    quoted_at: str


class LedgerPlacement(Contract):
    idempotency_key: UUID
    market_id: str = Field(min_length=1, max_length=128)
    stake: Decimal = Field(gt=0, le=10_000_000, decimal_places=2)
    model_used: ModelName


class ScoutRequest(Contract):
    id: UUID
    content: str = Field(min_length=1, max_length=2000)
    context_market_id: str | None = Field(default=None, max_length=128)
    model_used: ModelName


class ScoutMessage(Contract):
    id: str
    role: Literal["user", "scout"]
    content: str
    context_market_id: str | None = None


class OracleNotifier:
    async def dispatch_alert(self, payload: str) -> None:
        structlog.get_logger("board.oracle").info("oracle.alert", payload=payload)


class BoardBridge:
    """Bounded projection of real ingested quotes; absent data is never invented."""

    def __init__(self, ledger: PaperLedger) -> None:
        self.ledger = ledger
        self.quotes: OrderedDict[tuple[str, str], MarketOpportunity] = OrderedDict()

    @staticmethod
    def market_id(opportunity: MarketOpportunity) -> str:
        identity = [opportunity.fixture_id, opportunity.market_key, opportunity.outcome_key]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def ingest(self, opportunity: MarketOpportunity) -> None:
        key = (self.market_id(opportunity), opportunity.bookmaker.value)
        old = self.quotes.get(key)
        if old is not None and old.quoted_at > opportunity.quoted_at:
            return
        self.quotes[key] = opportunity
        self.quotes.move_to_end(key)
        while len(self.quotes) > 8192:
            self.quotes.popitem(last=False)

    def fresh(self) -> list[MarketOpportunity]:
        return [quote for quote in self.quotes.values() if 0 <= quote.age_seconds() <= 30]

    def health(self) -> MarketHealthScore:
        oracle = SessionIntelligenceOracle(notifier=OracleNotifier())
        for quote in self.fresh():
            oracle.ingest_opportunity(quote)
        return oracle.market_health_score()

    def markets(self, model: ModelName) -> list[BoardMarket]:
        if model != "System AI":
            raise HTTPException(503, "Custom Aggressive has no configured probability provider")
        grouped: dict[str, list[MarketOpportunity]] = {}
        for quote in self.fresh():
            grouped.setdefault(self.market_id(quote), []).append(quote)
        result: list[BoardMarket] = []
        for market_id, quotes in grouped.items():
            best = max(quotes, key=lambda quote: quote.offered_odds)
            latest = max(quotes, key=lambda quote: quote.quoted_at)
            market_type: Literal["spread", "moneyline", "total"]
            if best.market_key == "h2h":
                market_type = "moneyline"
            elif best.market_key.startswith("totals"):
                market_type = "total"
            elif best.market_key.startswith("spreads"):
                market_type = "spread"
            else:
                continue
            by_book = {quote.bookmaker.value: quote for quote in quotes}
            books = list(dict.fromkeys([*by_book, "pinnacle", "bet365", "betfair", "matchbook"]))
            implied = 1 / best.offered_odds
            result.append(BoardMarket(
                market_id=market_id,
                team_home=best.fixture_id,
                team_away="Team names not supplied by feed",
                selection_label=best.selection_label or best.outcome_key,
                fixture_id=best.fixture_id, market_type=market_type,
                sportsbook_odds=best.offered_odds,
                implied_probability=implied,
                model_win_chance=latest.fair_probability,
                edge_percentage=(latest.fair_probability - implied) * 100,
                quoted_at=best.quoted_at.isoformat(),
                lines=[BookLine(
                    sportsbook=book,
                    decimal_odds=by_book[book].offered_odds if book in by_book else None,
                    quoted_at=by_book[book].quoted_at.isoformat() if book in by_book else None,
                ) for book in books],
            ))
        return result


def bridge(request: Request) -> BoardBridge:
    value: object = getattr(request.app.state, "board_bridge", None)
    if not isinstance(value, BoardBridge):
        raise HTTPException(503, "Board bridge is not initialized")
    if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, "This paper-trading bridge is local-only")
    if request.method == "POST":
        origin = request.headers.get("origin")
        if origin and origin not in {
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:8000", "http://127.0.0.1:8000",
        }:
            raise HTTPException(403, "Origin is not allowed for paper-ledger mutations")
    return value

BridgeDep = Annotated[BoardBridge, Depends(bridge)]
router = APIRouter(prefix="/api/v1/board", tags=["local paper board"])


@router.get("/odds", response_model=list[BoardMarket])
async def odds(service: BridgeDep, model: ModelName = "System AI") -> list[BoardMarket]:
    return service.markets(model)


@router.get("/oracle/health", response_model=MarketHealthScore)
async def oracle_health(service: BridgeDep) -> MarketHealthScore:
    return service.health()


@router.post("/scout", response_model=ScoutMessage)
async def scout(body: ScoutRequest, service: BridgeDep) -> ScoutMessage:
    if body.context_market_id:
        market = next((item for item in service.markets(body.model_used)
                       if item.market_id == body.context_market_id), None)
        if market is None:
            raise HTTPException(409, "The selected market is no longer fresh")
        content = (
            f"{market.selection_label}: model probability {market.model_win_chance:.2%}; "
            f"gross implied probability {market.implied_probability:.2%} "
            f"at decimal odds {market.sportsbook_odds:.3f}. "
            f"The difference is {market.edge_percentage:+.2f} percentage points. "
            "A positive difference is an estimated edge, not a guaranteed outcome. "
            "This is a deterministic explanation of the feed, not a conversational AI response. "
            "Paper ledger only; no sportsbook execution."
        )
    else:
        health = service.health()
        content = (
            f"Market health: {health.score:.1f}/10 across {health.sample_size} fresh quotes, "
            f"including {health.positive_ev_count} positive-EV quotes. "
            "Select a market to explain its probability and implied price. "
            "Free-form conversational AI is not configured."
        )
    return ScoutMessage(id=str(uuid4()), role="scout", content=content,
                        context_market_id=body.context_market_id)


@router.get("/wallet", response_model=WalletSnapshot)
async def wallet(service: BridgeDep, pending_key: UUID | None = None) -> WalletSnapshot:
    return await service.ledger.snapshot(str(pending_key) if pending_key else None)


@router.get("/ledger/{key}", response_model=WalletSnapshot)
async def reconcile(key: UUID, service: BridgeDep) -> WalletSnapshot:
    # A missing receipt is UNKNOWN, never a rejection or permission to refund.
    return await service.ledger.snapshot(str(key))


@router.post("/ledger", response_model=WalletSnapshot)
async def place(body: LedgerPlacement, service: BridgeDep) -> WalletSnapshot:
    quotes = [quote for quote in service.fresh()
              if service.market_id(quote) == body.market_id]
    rejection = None
    if body.model_used != "System AI":
        rejection = "Custom model is not configured"
    elif not quotes:
        rejection = "Market quote expired or unavailable"
    try:
        return await service.ledger.place(
            key=str(body.idempotency_key), market_id=body.market_id, model=body.model_used,
            stake_paise=int(body.stake * 100),
            sport=quotes[0].sport_type.value if quotes else "other", rejection=rejection,
        )
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
