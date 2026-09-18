from sqlalchemy import Column, Integer, String, BigInteger, DateTime
from sqlalchemy.sql import func
from .database import Base

class Wallet(Base):
    __tablename__ = 'wallet'
    id = Column(Integer, primary_key=True, index=True)
    balance_paise = Column(BigInteger, default=1042000)
    revision = Column(Integer, default=0)

class Bet(Base):
    __tablename__ = 'bets'
    id = Column(Integer, primary_key=True, index=True)
    idempotency_key = Column(String, unique=True, index=True, nullable=False)
    market_id = Column(String, nullable=False)
    stake_paise = Column(BigInteger, nullable=False)
    model_used = Column(String, nullable=False)
    sport = Column(String, nullable=False)
    status = Column(String, nullable=False)
    reason = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
