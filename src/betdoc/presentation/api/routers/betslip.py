from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Annotated, Final

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from betdoc.presentation.api.routers.board import TICK_KEY_PATTERN, get_redis
from betdoc.services.advisor.twin_engine import OddsTick, ParlayStack, TwinEngine

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/betslip", tags=["betslip"])

TICK_KEY_PREFIX: Final[str] = TICK_KEY_PATTERN.removesuffix("*").rstrip(":")
MAX_LEGS_PER_SLIP: Final[int] = 40

# --------------------------------------------------------------------------- #
# Database (async, Postgres)
# --------------------------------------------------------------------------- #
_db_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _database_url() -> str:
    url = os.environ.get("BETDOC_DATABASE_URL")
    if not url:
        raise RuntimeError("BETDOC_DATABASE_URL is not set")
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url.removeprefix("postgres://")
    return url


def get_db_engine() -> AsyncEngine:
    global _db_engine, _session_factory
    if _db_engine is None:
        _db_engine = create_async_engine(
            _database_url(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_timeout=5.0,
        )
        _session_factory = async_sessionmaker(_db_engine, expire_on_commit=False)
    return _db_engine


async def dispose_db_engine() -> None:
    global _db_engine, _session_factory
    if _db_engine is not None:
        await _db_engine.dispose()
    _db_engine = None
    _session_factory = None


async def get_session() -> AsyncIterator[AsyncSession]:
    get_db_engine()
    assert _session_factory is not None
    async with _session_factory() as session:
        yield session


def get_twin_engine(request: Request) -> TwinEngine:
    engine: TwinEngine | None = getattr(request.app.state, "twin_engine", None)
    if engine is None:
        engine = TwinEngine()
        request.app.state.twin_engine = engine
    return engine


RedisDep = Annotated[Redis, Depends(get_redis)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
EngineDep = Annotated[TwinEngine, Depends(get_twin_engine)]

# Lightweight Core table handles mirroring migration 3601079cbd79 (no ORM import).
_metadata = sa.MetaData()

wallets = sa.Table(
    "wallets",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("balance_paise", sa.BigInteger, nullable=False),
    sa.Column("reserved_paise", sa.BigInteger, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)

exposure_limits = sa.Table(
    "exposure_limits",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("wallet_id", sa.Integer, nullable=False),
    sa.Column("scope", sa.String(16), nullable=False),
    sa.Column("scope_key", sa.String(128), nullable=False),
    sa.Column("max_exposure_paise", sa.BigInteger, nullable=False),
    sa.Column("max_stake_paise", sa.BigInteger, nullable=False),
    sa.Column("max_open_tickets", sa.Integer, nullable=False),
)

parlay_tickets = sa.Table(
    "parlay_tickets",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("ticket_ref", sa.String(36), nullable=False),
    sa.Column("wallet_id", sa.Integer, nullable=False),
    sa.Column("leg_count", sa.Integer, nullable=False),
    sa.Column("stake_paise", sa.BigInteger, nullable=False),
    sa.Column("combined_decimal_odds", sa.Numeric(20, 6), nullable=False),
    sa.Column("potential_payout_paise", sa.BigInteger, nullable=False),
    sa.Column("stack_win_probability", sa.Numeric(12, 10), nullable=False),
    sa.Column("expected_value_per_unit", sa.Numeric(20, 8), nullable=False),
    sa.Column("kelly_fraction", sa.Numeric(8, 6), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
)

trade_executions = sa.Table(
    "trade_executions",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("execution_ref", sa.String(36), nullable=False),
    sa.Column("wallet_id", sa.Integer, nullable=False),
    sa.Column("ticket_id", sa.Integer, nullable=True),
    sa.Column("market_id", sa.String(128), nullable=False),
    sa.Column("event_id", sa.String(128), nullable=False),
    sa.Column("sport", sa.String(32), nullable=False),
    sa.Column("market_type", sa.String(32), nullable=False),
    sa.Column("selection", sa.String(128), nullable=False),
    sa.Column("team_home", sa.String(128), nullable=False),
    sa.Column("team_away", sa.String(128), nullable=False),
    sa.Column("american_odds", sa.Integer, nullable=False),
    sa.Column("decimal_odds", sa.Numeric(12, 6), nullable=False),
    sa.Column("implied_probability", sa.Numeric(12, 10), nullable=False),
    sa.Column("model_win_chance", sa.Numeric(12, 10), nullable=False),
    sa.Column("edge", sa.Numeric(12, 10), nullable=False),
    sa.Column("stake_paise", sa.BigInteger, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("tick_timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("placed_at", sa.DateTime(timezone=True), nullable=False),
)

ledger_entries = sa.Table(
    "ledger_entries",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("wallet_id", sa.Integer, nullable=False),
    sa.Column("ticket_id", sa.Integer, nullable=True),
    sa.Column("execution_id", sa.Integer, nullable=True),
    sa.Column("entry_type", sa.String(24), nullable=False),
    sa.Column("amount_paise", sa.BigInteger, nullable=False),
    sa.Column("balance_after_paise", sa.BigInteger, nullable=False),
    sa.Column("reserved_after_paise", sa.BigInteger, nullable=False),
    sa.Column("memo", sa.String(256), nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class PlacePaperParlayRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    wallet_id: int = Field(ge=1)
    stake_paise: int = Field(gt=0)
    market_ids: list[str] = Field(min_length=1, max_length=MAX_LEGS_PER_SLIP)

    @field_validator("market_ids")
    @classmethod
    def _unique_non_empty(cls, value: list[str]) -> list[str]:
        cleaned = [m for m in value if m]
        if len(cleaned) != len(value):
            raise ValueError("market_ids must not contain empty values")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("market_ids must be unique")
        return cleaned


class PlacePaperParlayResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_ref: str
    balance_paise: int
    leg_count: int
    stake_paise: int
    combined_decimal_odds: float
    potential_payout_paise: int
    stack_win_probability: float
    expected_value_per_unit: float
    kelly_fraction: float


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sport_from_market_id(market_id: str) -> str:
    sport, sep, _ = market_id.partition("-")
    if not sep or not sport:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"market_id '{market_id}' does not carry a sport prefix",
        )
    return sport


def _dec(value: float, places: int) -> Decimal:
    return Decimal(repr(value)).quantize(Decimal(1).scaleb(-places))


def _potential_payout(stake_paise: int, combined_decimal_odds: float) -> int:
    payout = Decimal(stake_paise) * Decimal(repr(combined_decimal_odds))
    return int(payout.to_integral_value(rounding=ROUND_DOWN))


async def _fetch_live_ticks(redis: Redis, market_ids: Sequence[str]) -> list[OddsTick]:
    keys = [f"{TICK_KEY_PREFIX}:{_sport_from_market_id(m)}:{m}" for m in market_ids]
    try:
        payloads = await redis.mget(keys)
    except RedisError as exc:
        log.error("betslip.redis_unavailable", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Live odds cache unavailable",
        ) from exc

    ticks: list[OddsTick] = []
    problems: list[str] = []
    for market_id, payload in zip(market_ids, payloads, strict=True):
        if payload is None:
            problems.append(f"{market_id}: no live price")
            continue
        try:
            tick = OddsTick.model_validate_json(payload)
        except ValidationError:
            problems.append(f"{market_id}: corrupt live price")
            continue
        if tick.market_id != market_id:
            problems.append(f"{market_id}: cache key/payload mismatch")
            continue
        if tick.suspended:
            problems.append(f"{market_id}: market suspended")
            continue
        ticks.append(tick)

    if problems:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"rejected": problems})
    return ticks


def _evaluate_slip(engine: TwinEngine, ticks: Sequence[OddsTick], market_ids: Sequence[str]) -> ParlayStack:
    # The slip is user-curated: keep the engine's probability/edge filters but relax
    # the autonomous 25-leg floor so any all-qualifying slip can execute.
    slip_config = engine.config.model_copy(update={"min_legs": 1, "max_legs": len(ticks)})
    stack = TwinEngine(slip_config).evaluate_parlay_candidates(ticks)

    accepted = {leg.market_id for leg in stack.legs}
    rejected = [m for m in market_ids if m not in accepted]
    if rejected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "one or more legs failed the edge filter",
                "rejected": rejected,
                "rejections": {k.value: v for k, v in stack.rejections.items()},
            },
        )
    if not stack.executable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "stack not executable", "reason": stack.rejection_reason},
        )
    return stack


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #
@router.post(
    "/place_paper_parlay",
    response_model=PlacePaperParlayResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Place a paper parlay against live odds",
)
async def place_paper_parlay(
    body: PlacePaperParlayRequest,
    redis: RedisDep,
    session: SessionDep,
    engine: EngineDep,
) -> PlacePaperParlayResponse:
    ticks = await _fetch_live_ticks(redis, body.market_ids)
    stack = _evaluate_slip(engine, ticks, body.market_ids)
    ticks_by_market = {t.market_id: t for t in ticks}

    ticket_ref = str(uuid.uuid4())
    now = datetime.now(UTC)
    payout_paise = _potential_payout(body.stake_paise, stack.combined_decimal_odds)

    try:
        async with session.begin():
            wallet = (
                await session.execute(
                    sa.select(wallets.c.id, wallets.c.balance_paise, wallets.c.reserved_paise)
                    .where(wallets.c.id == body.wallet_id)
                    .with_for_update()
                )
            ).one_or_none()
            if wallet is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="wallet not found")

            balance: int = int(wallet.balance_paise)
            reserved: int = int(wallet.reserved_paise)
            if balance - reserved < body.stake_paise:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "message": "insufficient available balance",
                        "available_paise": balance - reserved,
                        "stake_paise": body.stake_paise,
                    },
                )

            limit = (
                await session.execute(
                    sa.select(
                        exposure_limits.c.max_stake_paise,
                        exposure_limits.c.max_exposure_paise,
                        exposure_limits.c.max_open_tickets,
                    ).where(
                        exposure_limits.c.wallet_id == body.wallet_id,
                        exposure_limits.c.scope == "global",
                    )
                )
            ).one_or_none()
            if limit is not None:
                if body.stake_paise > int(limit.max_stake_paise):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"message": "stake exceeds max_stake_paise", "max_stake_paise": int(limit.max_stake_paise)},
                    )
                open_row = (
                    await session.execute(
                        sa.select(
                            sa.func.coalesce(sa.func.sum(parlay_tickets.c.stake_paise), 0),
                            sa.func.count(parlay_tickets.c.id),
                        ).where(
                            parlay_tickets.c.wallet_id == body.wallet_id,
                            parlay_tickets.c.status == "open",
                        )
                    )
                ).one()
                open_exposure = int(open_row[0])
                open_count = int(open_row[1])
                if open_exposure + body.stake_paise > int(limit.max_exposure_paise):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "message": "stake breaches max_exposure_paise",
                            "open_exposure_paise": open_exposure,
                            "max_exposure_paise": int(limit.max_exposure_paise),
                        },
                    )
                if open_count + 1 > int(limit.max_open_tickets):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"message": "max_open_tickets reached", "max_open_tickets": int(limit.max_open_tickets)},
                    )

            new_balance = balance - body.stake_paise
            await session.execute(
                sa.update(wallets)
                .where(wallets.c.id == body.wallet_id)
                .values(balance_paise=new_balance, updated_at=now)
            )

            ticket_id = (
                await session.execute(
                    sa.insert(parlay_tickets)
                    .values(
                        ticket_ref=ticket_ref,
                        wallet_id=body.wallet_id,
                        leg_count=stack.leg_count,
                        stake_paise=body.stake_paise,
                        combined_decimal_odds=_dec(stack.combined_decimal_odds, 6),
                        potential_payout_paise=payout_paise,
                        stack_win_probability=_dec(stack.stack_win_probability, 10),
                        expected_value_per_unit=_dec(stack.expected_value_per_unit, 8),
                        kelly_fraction=_dec(stack.kelly_fraction, 6),
                        status="open",
                        placed_at=now,
                    )
                    .returning(parlay_tickets.c.id)
                )
            ).scalar_one()

            await session.execute(
                sa.insert(trade_executions),
                [
                    {
                        "execution_ref": str(uuid.uuid4()),
                        "wallet_id": body.wallet_id,
                        "ticket_id": ticket_id,
                        "market_id": leg.market_id,
                        "event_id": leg.event_id,
                        "sport": leg.sport,
                        "market_type": leg.market_type,
                        "selection": leg.selection,
                        "team_home": leg.team_home,
                        "team_away": leg.team_away,
                        "american_odds": leg.american_odds,
                        "decimal_odds": _dec(leg.decimal_odds, 6),
                        "implied_probability": _dec(leg.implied_probability, 10),
                        "model_win_chance": _dec(leg.model_win_chance, 10),
                        "edge": _dec(leg.edge, 10),
                        "stake_paise": 0,
                        "status": "open",
                        "tick_timestamp": ticks_by_market[leg.market_id].timestamp,
                        "placed_at": now,
                    }
                    for leg in stack.legs
                ],
            )

            await session.execute(
                sa.insert(ledger_entries).values(
                    wallet_id=body.wallet_id,
                    ticket_id=ticket_id,
                    execution_id=None,
                    entry_type="stake_debit",
                    amount_paise=-body.stake_paise,
                    balance_after_paise=new_balance,
                    reserved_after_paise=reserved,
                    memo=f"paper parlay {ticket_ref} ({stack.leg_count} legs)",
                    created_at=now,
                )
            )
    except HTTPException:
        raise
    except IntegrityError as exc:
        log.error("betslip.integrity_error", error=str(exc.orig))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ledger constraint violated") from exc
    except SQLAlchemyError as exc:
        log.error("betslip.db_error", error=str(exc))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="ledger unavailable") from exc

    log.info(
        "betslip.paper_parlay_placed",
        ticket_ref=ticket_ref,
        wallet_id=body.wallet_id,
        legs=stack.leg_count,
        stake_paise=body.stake_paise,
        payout_paise=payout_paise,
        stack_win_probability=round(stack.stack_win_probability, 6),
    )
    return PlacePaperParlayResponse(
        ticket_ref=ticket_ref,
        balance_paise=new_balance,
        leg_count=stack.leg_count,
        stake_paise=body.stake_paise,
        combined_decimal_odds=round(stack.combined_decimal_odds, 6),
        potential_payout_paise=payout_paise,
        stack_win_probability=round(stack.stack_win_probability, 10),
        expected_value_per_unit=round(stack.expected_value_per_unit, 8),
        kelly_fraction=round(stack.kelly_fraction, 6),
    )
