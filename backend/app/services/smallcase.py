"""Live smallcases (Group 107, point b.8): the six thematic baskets, rebuilt from the live market, sized by KUMBHA.
Developed for Amit Ashok Kumar Patnaik.

* ``scan``: every quoted leg of Ashoka's live market (``ashoka_market.load_candidates``, pre-match and in play within
  ``LIVE_LOOKBACK``) priced at its best fresh retail book (Parimatch, 1xBet, Stake) against BetDoc's fair probability.
  The scan is shared by every viewer for ``SMALLCASE_SCAN_SECONDS`` (Redis); a rebalance forces a fresh one.
* ``baskets``: each theme (``app.domain.smallcase.baskets``) takes its holdings from the scan; KUMBHA sizes them
  against the user's CFO bankroll, rolling drawdown, the models' skill and any latched halt, all read without side
  effects (``app.domain.smallcase.portfolio``); each basket carries its risk, its parlay alternative, each holding's
  in-play stop-loss floor, and USD only at TODAR MAL's fresh rate.
* ``rebalance_slip``: the user's own slip turned into a smallcase at the chosen book: one holding per fixture, legs
  priced under fair left out (KUMBHA stakes nothing on them), the rest sized and measured, beside the slip as one
  parlay.
* ``components``: the catalogue and how the smallcases use it (``app.domain.smallcase.components``).

Nothing is placed from here: loading a basket puts its legs on the Oracle's slip, where the fortress inspects them.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.backtesting import inplay_stoploss_math as slm
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS, EXCHANGE_BOOKMAKERS, fixture_name, market_selection_label
from app.domain.cfo import growth_math as gm
from app.domain.smallcase import baskets as bk
from app.domain.smallcase import components as cmp
from app.domain.smallcase import portfolio as pf
from app.services import ashoka_market
from app.services import manual_parlay as workbench
from app.services.cfo import growth_optimizer as growth
from app.services.currency_service import CurrencyService, CurrencyUnavailableError
from app.services.twin import vetting
from app.services.twin.intel import model_weights

CENT = Decimal("0.01")


class SmallcaseRefusal(Exception):
    def __init__(self, reason: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.reason, self.message, self.status_code = reason, message, status_code


def _key(settings: Settings) -> str:
    return f"{settings.ARYABHATA_PREFIX}:smallcase:scan"


def _pick(leg: Any, quote: Any, now: datetime, settings: Settings) -> bk.Pick | None:
    try:
        ev, fair = leg.ev(quote.net_odds), leg.probability
    except ValueError:  # a market without a fair price (no model and no consensus): it cannot be valued
        return None
    kickoff = leg.kickoff
    return bk.Pick(leg.leg_id, leg.fixture_id, fixture_name(leg.home, leg.away), market_selection_label(leg.market.key, leg.selection, leg.home, leg.away),
                   leg.market.key, leg.market.kind.value, leg.sport_key or "", workbench.sport_of(leg.sport_key, settings), leg.league,
                   None if kickoff is None else kickoff.isoformat(), kickoff is not None and kickoff <= now, quote.bookmaker, float(quote.odds),
                   round(float(fair), 6), round(float(ev), 6), {b: float(q.odds) for b, q in leg.quotes.items() if b not in EXCHANGE_BOOKMAKERS})


async def scan(redis: Redis, settings: Settings, now: datetime, *, fresh: bool = False) -> tuple[list[bk.Pick], str, int]:
    """(every valued leg at its best retail book, when the market was read, legs scanned)."""
    if not fresh:
        try:
            cached = await redis.get(_key(settings))
        except (RedisError, OSError):
            cached = None
        if cached:
            data = json.loads(cached)
            return [bk.Pick.of(row) for row in data["picks"]], data["as_of"], data["scanned"]
    candidates, _ = await ashoka_market.load_candidates(redis, settings, now - vetting.LIVE_LOOKBACK)
    picks = []
    for leg in candidates:
        quote = leg.best_quote(now, settings.ASHOKA_MAX_QUOTE_AGE_SECONDS, workbench.ACCOUNT_BOOKS)
        pick = None if quote is None else _pick(leg, quote, now, settings)
        if pick is not None:
            picks.append(pick)
    payload = {"as_of": now.isoformat(), "scanned": len(candidates), "picks": [p.as_dict() for p in picks]}
    try:
        await redis.set(_key(settings), json.dumps(payload), ex=settings.SMALLCASE_SCAN_SECONDS)
    except (RedisError, OSError):
        pass
    return picks, payload["as_of"], len(candidates)


async def _capital(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, bankroll: Decimal | None,
                   now: datetime) -> tuple[Decimal | None, float, float | None, bool]:
    """KUMBHA's inputs, read only: the bankroll (the caller's, else the CFO main account), rolling drawdown, the models'
    skill, and whether a drawdown halt is latched."""
    weights = await model_weights(redis, settings)
    async with sessions() as session:
        wallet = await vetting.bankroll_for(session, user_id, bankroll)
        drawdown = await vetting.drawdown_for(session, user_id, wallet, now, settings.CFO_DRAWDOWN_WINDOW_DAYS)
        skill = await growth.fleet_skill(session, weights)
        latched = await growth.halt_latched(session, user_id)
    return wallet, drawdown, skill, latched


async def _usd(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, now: datetime) -> Decimal | None:
    try:
        async with sessions() as session:
            return (await CurrencyService(settings, redis).inr_per_unit(session, "USD", now, allow_refresh=False)).inr_per_unit
    except CurrencyUnavailableError:
        return None


def _usd_of(amount: Decimal, rate: Decimal | None) -> str | None:
    return None if rate is None or rate <= 0 or amount <= 0 else str((amount / rate).quantize(CENT))


def _measure(picks: Sequence[bk.Pick], wallet: Decimal | None, drawdown: float, skill: float | None, latched: bool, settings: Settings,
             rate: Decimal | None) -> dict[str, Any]:
    sizing = pf.size(picks, bankroll=wallet, drawdown=drawdown, policy=gm.SizingPolicy.from_settings(settings), step=Decimal(settings.TWIN_STAKE_STEP_INR),
                     basket_cap=float(settings.SMALLCASE_MAX_BASKET_FRACTION), skill=skill, latched=latched)
    stop = slm.StopLossPolicy.from_settings(settings)
    holdings = []
    for h in sizing.holdings:
        pct = slm.recommended_pct(h.pick.fair_probability, stop)
        holdings.append({**h.as_dict(), "book_label": ASHOKA_BOOKMAKERS.get(h.pick.book, h.pick.book), "stake_usd": _usd_of(h.stake, rate),
                         "stop_loss": {"pct": pct, "floor_inr": str(slm.floor(h.stake, pct))}})
    measured = pf.risk(sizing.holdings, settings.ASHOKA_MC_PATHS)
    stake = sizing.stake
    expected = None if not measured or stake <= 0 else str((stake * Decimal(str(measured["expected_return"]))).quantize(CENT))
    return {"holdings": holdings, "stake_inr": str(stake), "stake_usd": _usd_of(stake, rate), "fraction": round(sizing.fraction, 6),
            "cap_fraction": float(settings.SMALLCASE_MAX_BASKET_FRACTION), "scaled_to_cap": sizing.scaled < 1.0, "expected_profit_inr": expected,
            "risk": measured or None, "parlay": pf.as_parlay(picks), "regime": sizing.regime, "damper": sizing.damper, "halted": sizing.halted}


async def baskets(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, *, bankroll: Decimal | None,
                  now: datetime, fresh: bool = False) -> dict[str, Any]:
    """The six baskets from the live market, sized for this user."""
    picks, as_of, scanned = await scan(redis, settings, now, fresh=fresh)
    policy = bk.ThemePolicy.from_settings(settings)
    wallet, drawdown, skill, latched = await _capital(sessions, redis, settings, user_id, bankroll, now)
    rate = await _usd(sessions, redis, settings, now)
    chosen = {theme.id: bk.choose(theme, picks, policy) for theme in bk.THEMES}
    holders: dict[str, list[str]] = defaultdict(list)
    for theme_id, legs in chosen.items():
        for p in legs:
            holders[p.leg_id].append(theme_id)
    out = []
    for theme in bk.THEMES:
        legs = chosen[theme.id]
        body = _measure(legs, wallet, drawdown, skill, latched, settings, rate) if legs else {"holdings": [], "stake_inr": "0.00", "risk": None, "parlay": None}
        for h in body["holdings"]:
            h["also_in"] = [b for b in holders[h["leg_id"]] if b != theme.id]
        out.append({"id": theme.id, "name": theme.name, "tier": theme.tier, "scope": theme.scope, "status": "LIVE" if legs else "EMPTY",
                    "empty_reason": None if legs else theme.empty, "sports": sorted({p.sport or p.sport_key for p in legs}), **body})
    async with sessions() as session:
        credit = await vetting.developer_credit(session)
    return {"as_of": as_of, "computed_at": now.isoformat(), "scanned": scanned, "valued": len(picks), "eligible": len(bk.eligible(picks, policy)),
            "bankroll_inr": None if wallet is None else str(wallet), "drawdown": round(drawdown, 6), "skill_bss": skill, "halt_latched": latched,
            "inr_per_usd": None if rate is None else str(rate), "refresh_seconds": settings.SMALLCASE_SCAN_SECONDS,
            "policy": {"min_ev": policy.min_ev, "max_legs": policy.max_legs, "safe_min_probability": policy.safe_min_probability,
                       "underdog_min_odds": policy.underdog_min_odds, "european_leagues": list(policy.european_leagues),
                       "max_basket_fraction": float(settings.SMALLCASE_MAX_BASKET_FRACTION), "kelly_multiplier": float(settings.CFO_KELLY_FRACTION_DEFAULT)},
            "baskets": out, "developer_credit": credit}


def basket(board: dict[str, Any], basket_id: str) -> dict[str, Any]:
    found = next((b for b in board["baskets"] if b["id"] == basket_id.upper()), None)
    if found is None:
        raise SmallcaseRefusal("UNKNOWN_BASKET", f"the baskets are {', '.join(t.id for t in bk.THEMES)}", 404)
    return {**{k: v for k, v in board.items() if k != "baskets"}, "basket": found}


def ticker(board: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": b["id"], "name": b["name"], "tier": b["tier"], "status": b["status"], "legs": len(b["holdings"]), "stake_inr": b["stake_inr"],
             "expected_return": (b["risk"] or {}).get("expected_return"), "p_profit": (b["risk"] or {}).get("p_profit"), "sports": b["sports"]}
            for b in board["baskets"]]


async def rebalance_slip(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, *, leg_ids: Sequence[str],
                         skin: str, bankroll: Decimal | None, now: datetime) -> dict[str, Any]:
    """The user's slip as a smallcase at the skin's book: one holding per fixture, legs under fair left out."""
    if skin not in workbench.SKINS:
        raise SmallcaseRefusal("UNKNOWN_SKIN", f"skin is one of {', '.join(workbench.SKINS)}", 422)
    book = workbench.SKINS[skin][0]
    books = (book,) if book else workbench.ACCOUNT_BOOKS
    wanted = list(dict.fromkeys(leg_ids))
    candidates, _ = await ashoka_market.load_candidates(redis, settings, now - vetting.LIVE_LOOKBACK, fixtures={leg_id.split("|", 1)[0] for leg_id in wanted})
    by_id = {leg.leg_id: leg for leg in candidates}
    priced: list[bk.Pick] = []
    excluded: list[dict[str, Any]] = []
    for leg_id in wanted:
        leg = by_id.get(leg_id)
        quote = None if leg is None else leg.best_quote(now, settings.ASHOKA_MAX_QUOTE_AGE_SECONDS, books)
        pick = None if leg is None or quote is None else _pick(leg, quote, now, settings)
        if pick is None:
            where = ASHOKA_BOOKMAKERS.get(book, book) if book else "a retail book"
            excluded.append({"leg_id": leg_id, "reason": "NOT_QUOTED", "message": f"not freshly quoted at {where}, or it has no fair price"})
            continue
        priced.append(pick)
    held: list[bk.Pick] = []
    for pick in sorted(priced, key=lambda p: (-p.ev, p.leg_id)):
        if pick.ev <= 0:
            excluded.append({"leg_id": pick.leg_id, "label": pick.label, "reason": "UNDER_FAIR",
                             "message": f"{pick.price} at {ASHOKA_BOOKMAKERS.get(pick.book, pick.book)} is under BetDoc's fair chance ({pick.ev:+.1%} EV): KUMBHA stakes nothing"})
        elif any(h.fixture_id == pick.fixture_id for h in held):
            excluded.append({"leg_id": pick.leg_id, "label": pick.label, "reason": "SAME_FIXTURE",
                             "message": "a second leg on a fixture the smallcase already holds: singles on one match move together"})
        else:
            held.append(pick)
    wallet, drawdown, skill, latched = await _capital(sessions, redis, settings, user_id, bankroll, now)
    rate = await _usd(sessions, redis, settings, now)
    body = _measure(held, wallet, drawdown, skill, latched, settings, rate) if held else {"holdings": [], "stake_inr": "0.00", "risk": None, "parlay": None}
    return {**body, "excluded": excluded, "as_built": pf.as_parlay(priced), "book": book, "book_label": ASHOKA_BOOKMAKERS.get(book, book) if book else "best retail price",
            "bankroll_inr": None if wallet is None else str(wallet), "computed_at": now.isoformat()}


async def components(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    board = await baskets(sessions, redis, settings, user_id, bankroll=None, now=now)
    live = [b for b in board["baskets"] if b["status"] == "LIVE"]
    return {**cmp.coverage(cmp.held_kinds(live), len(live)), "as_of": board["as_of"]}
