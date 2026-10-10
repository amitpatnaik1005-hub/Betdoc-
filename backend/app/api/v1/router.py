from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.v1 import auth, bet, exchange, ingestion, ws, execution, admin, odds, engine, capital, bet_calculator, oracle, vault, arena, market_signals, dashboard, the_wire, the_lab, the_hive, the_core, popular_picks, competitive_intel, oracle_scout, archive, control_panel, cfo_execution, sniper, portfolio, hive_trading, lab_quant, nalanda, sentinel
from app.api.v1 import bookmakers, cfo, human_touch, omni, omni_admin, omni_fleet, parimatch_feed, phantom, sports, vault_admin, execution_router, digital_twin

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
api_router.include_router(omni_fleet.router)  # /omni/fleet: Fleet Command (reads: user, writes: admin)
api_router.include_router(cfo_execution.router, dependencies=_authenticated)  # /omni/execute-trade, bankroll, risk settings
api_router.include_router(sniper.router, dependencies=_authenticated)  # /omni/venues, executions, dlq, sniper feed
api_router.include_router(portfolio.router, dependencies=_authenticated)  # /omni/portfolio, hedge, arbitrage, fx-rates
api_router.include_router(hive_trading.router, dependencies=_authenticated)  # /hive/trading: autonomous bots, registry, master halt
api_router.include_router(lab_quant.router, dependencies=_authenticated)  # /lab/quant: backtests on the market history (Group 66)
api_router.include_router(nalanda.router, dependencies=_authenticated)  # /nalanda: the tick lake and the hash-chained settlement warehouse (Group 67)
# /sentinel: alerting, liveness and remote command (Group 68). No router-wide auth: every route declares its
# user or admin, except Telegram's webhook, which authenticates with its secret token instead of a JWT.
api_router.include_router(sentinel.router)
# Group 70: /vault (the credential vault and fleet configuration: every route admin-only) and /parimatch
# (direct odds injection: admin, or the webhook's own token)
api_router.include_router(vault_admin.router)
api_router.include_router(parimatch_feed.router)
# Group 71: /router (the Smart Order Router: multi-venue slicing, slippage guard, venue breakers). Admin only.
api_router.include_router(execution_router.router)
# Group 72: /twin (the True Digital Betting Twin: the 14-pillar fortress, booking-code ledger entries, the in-play watch).
# Per-route auth: users vet, record and watch their own slips; evidence writes and Pathway B routing are admin only.
api_router.include_router(digital_twin.router)
