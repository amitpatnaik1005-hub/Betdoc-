from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.v1 import auth, bet, exchange, ingestion, ws, execution, admin, odds, engine, capital, bet_calculator, oracle, vault, arena, market_signals, dashboard, the_wire, the_lab, the_hive, the_core, popular_picks, competitive_intel, oracle_scout, archive, control_panel
from app.api.v1 import bookmakers, cfo, human_touch, omni, omni_admin, phantom, sports

api_router = APIRouter(prefix="/api/v1")

# Section routers that carry no auth of their own require a signed-in user at the mount.
_authenticated = [Depends(get_current_user)]

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
api_router.include_router(the_wire.router, prefix="/the-wire")
api_router.include_router(the_lab.router, prefix="/lab")
api_router.include_router(the_hive.router)
api_router.include_router(the_core.router, prefix='/core')
api_router.include_router(popular_picks.router, prefix='/popular-picks', dependencies=_authenticated)
api_router.include_router(competitive_intel.router, prefix='/rnd/competitive-intel', dependencies=_authenticated)
api_router.include_router(oracle_scout.router, prefix='/oracle-scout', dependencies=_authenticated)
api_router.include_router(archive.router, prefix='/archive', dependencies=_authenticated)
api_router.include_router(control_panel.router, prefix='/control-panel', dependencies=_authenticated)

# Groups 54-58 (prefixes match the ones their test suites mount)
api_router.include_router(bookmakers.router, dependencies=_authenticated)  # /bookmakers
api_router.include_router(cfo.router, prefix='/the-vault/cfo', dependencies=_authenticated)
api_router.include_router(human_touch.router, dependencies=_authenticated)  # /human-touch
api_router.include_router(phantom.router, prefix='/phantom', dependencies=_authenticated)
api_router.include_router(sports.router, dependencies=_authenticated)  # /sports
api_router.include_router(omni.router)  # /omni/ws/stream: token-checked in the handshake
api_router.include_router(omni_admin.router)  # /admin/omni: X-Omni-Admin-Token
