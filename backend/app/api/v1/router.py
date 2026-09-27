from fastapi import APIRouter

from app.api.v1 import auth, bet, exchange, ingestion, ws, execution, admin, odds, engine, capital, bet_calculator, oracle, vault, arena, market_signals, dashboard

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router, prefix="/auth")
api_router.include_router(exchange.router, prefix="/exchanges")
api_router.include_router(bet.router, prefix="/ledger")
api_router.include_router(ws.router, prefix="/ws")
api_router.include_router(ingestion.router, prefix="/ingest")
api_router.include_router(execution.router, prefix="/execution")
api_router.include_router(admin.router, prefix="/admin")
api_router.include_router(odds.router, prefix="/odds")
api_router.include_router(engine.router, prefix="/engine")
api_router.include_router(capital.router, prefix="/capital")
api_router.include_router(bet_calculator.router, prefix="/calculator")
api_router.include_router(oracle.router, prefix="/oracle")
api_router.include_router(vault.router, prefix="/vault")
api_router.include_router(arena.router, prefix="/arena")
api_router.include_router(market_signals.router, prefix="/signals")
api_router.include_router(dashboard.router, prefix="/dashboard")
