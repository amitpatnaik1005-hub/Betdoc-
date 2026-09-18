from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Literal, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from betdoc.infrastructure.database.database import AsyncSessionLocal
from betdoc.infrastructure.database.models import Wallet, Bet

router = APIRouter(prefix="/api/v1/ledger", tags=["ledger"])

class Receipt(BaseModel):
    idempotency_key: str
    status: Literal["recorded", "rejected"]
    reason: str

class WalletSnapshot(BaseModel):
    balance_paise: int
    revision: int
    currency: Literal["INR"] = "INR"
    mode: Literal["paper"] = "paper"
    receipt: Optional[Receipt] = None

class PlaceBetRequest(BaseModel):
    idempotency_key: str
    market_id: str
    stake_paise: int
    model_used: str
    sport: str = "soccer"

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

@router.get("/wallet", response_model=WalletSnapshot)
async def get_wallet(db: AsyncSession = Depends(get_db)):
    wallet = await db.scalar(select(Wallet).where(Wallet.id == 1))
    if not wallet:
        wallet = Wallet(id=1, balance_paise=1042000, revision=0)
        db.add(wallet)
        await db.commit()
        await db.refresh(wallet)
    return WalletSnapshot(balance_paise=wallet.balance_paise, revision=wallet.revision)

@router.post("/place", response_model=WalletSnapshot)
async def place_bet(req: PlaceBetRequest, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(select(Bet).where(Bet.idempotency_key == req.idempotency_key))
    wallet = await db.scalar(select(Wallet).where(Wallet.id == 1).with_for_update())
    if not wallet:
        wallet = Wallet(id=1, balance_paise=1042000, revision=0)
        db.add(wallet)
        await db.flush()

    if existing:
        return WalletSnapshot(
            balance_paise=wallet.balance_paise,
            revision=wallet.revision,
            receipt=Receipt(idempotency_key=existing.idempotency_key, status=existing.status, reason=existing.reason)
        )
    
    if wallet.balance_paise < req.stake_paise:
        status = "rejected"
        reason = "Insufficient paper bankroll"
    else:
        status = "recorded"
        reason = "Recorded in the paper ledger; no sportsbook order was sent."
        wallet.balance_paise -= req.stake_paise
    
    wallet.revision += 1
    
    bet = Bet(
        idempotency_key=req.idempotency_key,
        market_id=req.market_id,
        stake_paise=req.stake_paise,
        model_used=req.model_used,
        sport=req.sport,
        status=status,
        reason=reason
    )
    db.add(bet)
    await db.commit()
    
    return WalletSnapshot(
        balance_paise=wallet.balance_paise,
        revision=wallet.revision,
        receipt=Receipt(idempotency_key=bet.idempotency_key, status=bet.status, reason=bet.reason)
    )
