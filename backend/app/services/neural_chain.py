"""The neural chain (Group 108, point b.9): one Wire item through the Score Predictor, the Oracle, the Consultant and
the Vault's smallcases, every step a real engine call on the item's fixture. Developed for Amit Ashok Kumar Patnaik.

1. **VIDUR / THE WIRE**: the board's item (``the_wire.board``), resolved on the server: its source, tier, fixture.
2. **SCORE PREDICTOR**: the fixture's de-vigged Match Odds consensus now (``news.consensus``) through the lineup model
   (the recorded absences, ``the_wire.lineups``); for football, the Dixon-Coles scorelines fitted to it
   (``score_predictor.football_from_market``, the exact matrix) and, when rated absences move it, to the adjusted
   probabilities too. Other sports' simulators need their stats: the step says so rather than guess them.
3. **ORACLE & BET BUILDER**: a fresh read of the live market (the smallcases' scan, ``fresh=True``): the fixture's
   legs priced above fair at a retail book, best first, and the best leg's EV against the last chain's.
4. **BETDOC CONSULTANT**: when your slip holds a leg on the fixture, its Slip Score now (``manual_parlay.trial``, the
   15 pillars and badges, nothing recorded) against the score the last chain gave the same slip.
5. **THE VAULT & SMALLCASES**: the smallcases holding the fixture now; for you, KUMBHA's stake and the in-play
   stop-loss floor of each holding (``smallcase.baskets``). A chain the Wire's scan starts has no account: it names
   the baskets and leaves the sizing to the chain you run.

A step that has nothing to run on is SKIPPED with the reason; a refusal or an error is FAILED with the message, and
the chain carries on (the steps read the market independently). Nothing is placed, staked or recorded as a bet.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS
from app.domain.neural_bus.chain import Chain, Module, Status, Step, slip_signature
from app.domain.oracle.match_model import MarketTargets
from app.domain.oracle.slip_formatter import inr
from app.domain.the_wire import intel_board as ib
from app.services import neural_bus as bus
from app.services.the_wire import board as wire_board
from app.services.the_wire import lineups, news
from app.services.the_wire.fixtures import Fixture

logger = logging.getLogger("betdoc.neural.chain")

Outcome = tuple[Status, str, dict[str, Any]]


class ChainRefusal(Exception):
    def __init__(self, reason: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.reason, self.message, self.status_code = reason, message, status_code


@dataclass(slots=True)
class Context:
    sessions: async_sessionmaker[AsyncSession]
    redis: Redis | None
    settings: Settings
    item: dict[str, Any]
    user_id: uuid.UUID | None
    leg_ids: list[str]
    skin: str
    now: datetime
    fixture: Fixture | None = None
    picks: list[Any] | None = field(default=None)


def _pct(p: float) -> str:
    return f"{100.0 * p:.1f}%"


def _when(stamp: str | None, settings: Settings) -> str:
    """The desk's clock (ORACLE_TIMEZONE), as the rest of the app shows times."""
    if not stamp:
        return "the last run"
    try:
        zone = ZoneInfo(settings.ORACLE_TIMEZONE)
        moment = datetime.fromisoformat(stamp).astimezone(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return "the last run"
    return moment.strftime("%H:%M ") + (moment.tzname() or settings.ORACLE_TIMEZONE)


def trigger_of(item: dict[str, Any]) -> dict[str, Any]:
    fixture = item.get("fixture")
    return {"item_id": item["id"], "kind": item["kind"], "category": item["category"], "headline": item["headline"], "impact_level": item.get("impact_level"),
            "source": item["source"]["name"], "tier": item["source"]["tier"], "sport": item.get("sport"),
            "fixture": None if not fixture else {k: fixture[k] for k in ("fixture_id", "home", "away", "kickoff", "oracle_link")}}


# ================================================================ 1. VIDUR / THE WIRE
async def wire_step(ctx: Context) -> Outcome:
    item, f = ctx.item, ctx.item.get("fixture")
    tier = item["source"]["tier"].replace("_", " ").title()
    detail = {"item_id": item["id"], "kind": item["kind"], "category": item["category"], "impact_level": item.get("impact_level"),
              "source": item["source"], "published_at": item["published_at"], "fixture": f}
    if not f:
        return Status.COMPLETED, f"{item['source']['name']} ({tier}): {item['headline']} — it names no fixture on the live board", detail
    ctx.fixture = Fixture(f["fixture_id"], f["home"], f["away"], f.get("sport_key"), datetime.fromisoformat(f["kickoff"]))
    return Status.COMPLETED, f"{item['source']['name']} ({tier}) on {f['home']} v {f['away']}: {item['headline']}", detail


# ================================================================ 2. SCORE PREDICTOR
async def predictor_step(ctx: Context) -> Outcome:
    from app.domain.prediction import score_predictor as sp  # noqa: PLC0415 - numerical engine, loaded on use

    f = ctx.fixture
    if f is None:
        return Status.SKIPPED, "No fixture to re-price: the item names none on the live board", {}
    probs = (await news.consensus(ctx.redis, ctx.settings, [f.fixture_id], ctx.now)).get(f.fixture_id)
    async with ctx.sessions() as session:
        rows = await lineups.rows_for(session, f.fixture_id)
    view = lineups.view(rows, f, ctx.settings, probs)
    detail: dict[str, Any] = {"consensus": probs, "absences": len(view["absences"]), "unrated": view["unrated"],
                              "delta_pts": {k: round(100.0 * v, 2) for k, v in view["delta"].items()}, "adjusted": view["adjusted"],
                              "basis": "the de-vigged Match Odds consensus on the live board, through the lineup model's rated absences"}
    if not probs:
        if view["absences"]:
            return Status.SKIPPED, f"{len(view['absences'])} absence(s) recorded, but the live board has no Match Odds consensus to move", detail
        return Status.SKIPPED, "The live board has no Match Odds consensus for this fixture right now", detail
    moved = view["adjusted"] is not None and any(abs(v) > 0 for v in view["delta"].values())
    sides = " · ".join(f"{k} {_pct(v)}" for k, v in probs.items())
    lineup = (f"; with {len(view['absences']) - len(view['unrated'])} rated absence(s): "
              + " · ".join(f"{k} {_pct(v)}" for k, v in (view["adjusted"] or {}).items())) if moved else (
              f"; {len(view['unrated'])} absence(s) unrated, so not counted" if view["unrated"] else "; no rated absence moves it")
    if f.family == "soccer" and all(k in probs for k in ("HOME", "DRAW", "AWAY")):
        def fit(p: dict[str, float]) -> Any:
            return sp.football_from_market(MarketTargets(p["HOME"], p["DRAW"], p["AWAY"]), heatmap_goals=ctx.settings.PREDICTION_HEATMAP_GOALS,
                                           top=ctx.settings.PREDICTION_TOP_SCORELINES)

        market = await asyncio.to_thread(fit, probs)
        detail["market_fit"] = {"expected_goals": list(market.expected_goals),
                                "top_scorelines": [{"score": s.label, "probability": round(s.probability, 4)} for s in market.top_scorelines]}
        top = market.top_scorelines[0]
        summary = f"Market now {sides}: xG {market.expected_goals[0]:.2f}-{market.expected_goals[1]:.2f}, likeliest {top.label} ({_pct(top.probability)}){lineup}"
        if moved:
            adjusted = await asyncio.to_thread(fit, view["adjusted"])
            detail["lineup_fit"] = {"expected_goals": list(adjusted.expected_goals),
                                    "top_scorelines": [{"score": s.label, "probability": round(s.probability, 4)} for s in adjusted.top_scorelines]}
            summary += f" → xG {adjusted.expected_goals[0]:.2f}-{adjusted.expected_goals[1]:.2f}"
        detail["model"] = "Dixon-Coles scorelines fitted to the probabilities (exact matrix)"
        return Status.COMPLETED, summary, detail
    detail["model"] = "probabilities only: this sport's simulator needs its stats (run it in Score Prediction)"
    return Status.COMPLETED, f"Market now {sides}{lineup}", detail


# ================================================================ 3. ORACLE & BET BUILDER
async def oracle_step(ctx: Context) -> Outcome:
    from app.services import smallcase  # noqa: PLC0415 - the oracle engine is heavy

    f = ctx.fixture
    if f is None:
        return Status.SKIPPED, "No fixture to re-read: the item names none on the live board", {}
    if ctx.redis is None:
        return Status.FAILED, "The live market is unreadable without Redis", {}
    picks, as_of, scanned = await smallcase.scan(ctx.redis, ctx.settings, ctx.now, fresh=True)
    ctx.picks = picks
    legs = [p for p in picks if p.fixture_id == f.fixture_id]
    positive = sorted((p for p in legs if p.ev > 0), key=lambda p: (-p.ev, p.leg_id))
    top = [{"leg_id": p.leg_id, "label": p.label, "market": p.market, "book": p.book, "book_label": ASHOKA_BOOKMAKERS.get(p.book, p.book), "price": p.price,
            "fair_probability": round(p.fair_probability, 4), "ev": round(p.ev, 4)} for p in positive[: ctx.settings.NEURAL_BUS_TOP_EDGES]]
    key = bus.edge_key(ctx.settings, f.fixture_id)
    before = await bus.read_value(ctx.redis, key)
    best = positive[0] if positive else None
    shift = ib.edge_shift(best.ev if best else None, before, best.leg_id if best else None)
    detail = {"market_as_of": as_of, "legs_scanned": scanned, "legs_valued": len(legs), "legs_above_fair": len(positive), "top": top,
              "edge_shift_pts": shift, "edge_recorded": before}
    if not legs:
        return Status.COMPLETED, "No leg of this fixture is freshly quoted at a retail book with a fair price", detail
    if best is None:
        await bus.write_value(ctx.redis, ctx.settings, key, {"leg_id": None, "ev": None, "at": ctx.now.isoformat()})
        return Status.COMPLETED, f"{len(legs)} legs valued; none priced above BetDoc's fair chance at a retail book", detail
    await bus.write_value(ctx.redis, ctx.settings, key, {"leg_id": best.leg_id, "label": best.label, "ev": round(best.ev, 4), "price": best.price,
                                                          "book": best.book, "at": ctx.now.isoformat()})
    since = f" ({shift:+.1f} pts since {_when(before.get('at') if before else None, ctx.settings)})" if shift is not None else ""
    return (Status.COMPLETED,
            f"{len(legs)} legs valued, {len(positive)} above fair; best {best.label} @ {best.price:.2f} at {ASHOKA_BOOKMAKERS.get(best.book, best.book)}, "
            f"EV {100 * best.ev:+.1f}%{since}", detail)


# ================================================================ 4. BETDOC CONSULTANT
async def consultant_step(ctx: Context) -> Outcome:
    from app.services import manual_parlay as workbench  # noqa: PLC0415 - the fortress is heavy

    f = ctx.fixture
    if ctx.user_id is None:
        return Status.SKIPPED, "A chain the Wire's scan starts carries no slip: run it from the board to audit yours", {}
    if not ctx.leg_ids:
        return Status.SKIPPED, "No slip was sent: use Audit my slip to have the Consultant re-score it", {}
    if f is None:
        return Status.SKIPPED, "No fixture: the item names none on the live board", {}
    on_fixture = [leg for leg in ctx.leg_ids if leg.split("|", 1)[0] == f.fixture_id]
    if not on_fixture:
        return Status.COMPLETED, f"Your slip ({len(ctx.leg_ids)} legs) holds no leg on {f.home} v {f.away}: nothing to re-score", {"legs": len(ctx.leg_ids), "on_fixture": 0}
    trial = await workbench.trial(ctx.sessions, ctx.redis, ctx.settings, ctx.user_id, leg_ids=ctx.leg_ids, kind=None,  # type: ignore[arg-type]
                                  book=workbench.SKINS[ctx.skin][0], bankroll=None, now=ctx.now)
    key = bus.slip_key(ctx.settings, str(ctx.user_id), slip_signature(ctx.leg_ids, ctx.skin))
    before = await bus.read_value(ctx.redis, key)
    score, tier = round(float(trial.rating.score)), trial.rating.tier
    await bus.write_value(ctx.redis, ctx.settings, key, {"score": score, "tier": tier, "at": ctx.now.isoformat()})
    was = f" (was {before['score']} {before.get('tier', '')} at {_when(before.get('at'), ctx.settings)})".replace(" )", ")") if before and "score" in before else ""
    joint = trial.audit.joint_ev
    detail = {"legs": len(ctx.leg_ids), "on_fixture": len(on_fixture), "score": score, "tier": tier, "previous": before, "joint_ev": joint,
              "capped": trial.rating.capped, "warnings": trial.rating.warnings[:3], "verdict": getattr(trial.audit, "verdict", None),
              "book": workbench.SKINS[ctx.skin][0] or "best retail price"}
    joint_text = f"; joint EV {100 * float(joint):+.1f}%" if joint is not None else ""
    return Status.COMPLETED, f"Slip Score {score} {tier}{was}{joint_text} ({len(on_fixture)} of {len(ctx.leg_ids)} legs on this fixture)", detail


# ================================================================ 5. THE VAULT & SMALLCASES
async def vault_step(ctx: Context) -> Outcome:
    from app.domain.smallcase import baskets as bk  # noqa: PLC0415
    from app.services import smallcase  # noqa: PLC0415 - the oracle engine is heavy

    f = ctx.fixture
    if f is None:
        return Status.SKIPPED, "No fixture: the item names none on the live board", {}
    if ctx.redis is None:
        return Status.FAILED, "The smallcases are unreadable without Redis", {}
    if ctx.user_id is None:
        picks = ctx.picks if ctx.picks is not None else (await smallcase.scan(ctx.redis, ctx.settings, ctx.now))[0]
        policy = bk.ThemePolicy.from_settings(ctx.settings)
        holding = [{"basket": t.name, "legs": [p.label for p in bk.choose(t, picks, policy) if p.fixture_id == f.fixture_id]} for t in bk.THEMES]
        holding = [h for h in holding if h["legs"]]
        names = ", ".join(h["basket"] for h in holding)
        summary = (f"Held by {names}: KUMBHA sizes each account's stake when you run the chain" if holding
                   else "No smallcase holds this fixture now: none of its legs meets a basket's theme")
        return Status.COMPLETED, summary, {"baskets": holding}
    board = await smallcase.baskets(ctx.sessions, ctx.redis, ctx.settings, ctx.user_id, bankroll=None, now=ctx.now)
    holding = []
    for b in board["baskets"]:
        for h in b["holdings"]:
            if h["fixture_id"] == f.fixture_id:
                holding.append({"basket": b["name"], "basket_id": b["id"], "leg": h["label"], "stake_inr": h["stake_inr"], "fraction": h["fraction"],
                                "stop_loss_pct": h["stop_loss"]["pct"], "stop_floor_inr": h["stop_loss"]["floor_inr"], "book": h["book_label"], "price": h["price"]})
    detail = {"baskets": holding, "bankroll_inr": board["bankroll_inr"], "halt_latched": board["halt_latched"], "drawdown": board["drawdown"]}
    if not holding:
        return Status.COMPLETED, "No smallcase holds this fixture now: none of its legs meets a basket's theme", detail
    if board["bankroll_inr"] is None:
        return Status.COMPLETED, f"Held by {', '.join(h['basket'] for h in holding)}; no CFO bankroll is set, so KUMBHA shows weights only", detail
    parts = [f"{h['basket']}: {h['leg']} {inr(float(h['stake_inr']))}, stop floor {inr(float(h['stop_floor_inr']))}" if float(h["stake_inr"]) > 0
             else f"{h['basket']}: {h['leg']} (KUMBHA stakes nothing now)" for h in holding]
    halted = " A drawdown halt is latched: KUMBHA stakes nothing until it is signed off." if board["halt_latched"] else ""
    return Status.COMPLETED, "; ".join(parts) + halted, detail


STEPS: tuple[tuple[Module, Callable[[Context], Awaitable[Outcome]]], ...] = (
    (Module.WIRE, wire_step), (Module.SCORE_PREDICTOR, predictor_step), (Module.ORACLE, oracle_step),
    (Module.CONSULTANT, consultant_step), (Module.VAULT, vault_step),
)


def _refusal(exc: Exception) -> str | None:
    """The message of an engine's own refusal (the fortress, the workbench, the smallcases), else None."""
    from app.services.manual_parlay import WorkbenchRefusal  # noqa: PLC0415
    from app.services.smallcase import SmallcaseRefusal  # noqa: PLC0415
    from app.services.twin.vetting import TwinRefusal  # noqa: PLC0415

    return exc.message if isinstance(exc, TwinRefusal | WorkbenchRefusal | SmallcaseRefusal) else None


async def run(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, *, item: dict[str, Any], user_id: uuid.UUID | None,
              leg_ids: Sequence[str] = (), skin: str = "betdoc", now: datetime | None = None) -> dict[str, Any]:
    """Run the five steps in order, a frame as each starts and ends; the chain is remembered for its audience."""
    started = now or datetime.now(UTC)
    audience = bus.SYSTEM if user_id is None else str(user_id)
    chain = Chain(uuid.uuid4().hex, audience, trigger_of(item), started.isoformat())
    ctx = Context(sessions, redis, settings, item, user_id, list(dict.fromkeys(leg_ids)), skin, started)

    async def emit(kind: str, body: dict[str, Any]) -> None:
        await bus.publish(redis, settings, {"type": kind, "audience": audience, "chain_id": chain.chain_id, "trigger": chain.trigger, **body})

    await emit("chain_started", {"started_at": chain.started_at})
    for number, (module, fn) in enumerate(STEPS, start=1):
        step = Step(number, module, Status.RUNNING, "", started_at=datetime.now(UTC).isoformat())
        await emit("step", {"step": step.as_dict()})
        t0 = time.perf_counter()
        try:
            step.status, step.summary, step.detail = await fn(ctx)
        except (RedisError, OSError, TimeoutError) as exc:
            step.status, step.summary = Status.FAILED, f"A store was unreachable ({type(exc).__name__})"
        except Exception as exc:  # noqa: BLE001 - one engine's failure never stops the chain
            message = _refusal(exc)
            if message is None:
                logger.exception("neural chain %s: step %s raised", chain.chain_id, module.value)
                message = f"The engine raised {type(exc).__name__}"
            step.status, step.summary = Status.FAILED, message
        step.ms = round(1000 * (time.perf_counter() - t0))
        step.finished_at = datetime.now(UTC).isoformat()
        chain.steps.append(step)
        await emit("step", {"step": step.as_dict()})
    chain.completed_at = datetime.now(UTC).isoformat()
    record = chain.as_dict()
    await bus.remember(redis, settings, audience, record)
    await emit("chain", {"chain": record})
    return record


async def run_item(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, item_id: str, *, user_id: uuid.UUID | None,
                   leg_ids: Sequence[str] = (), skin: str = "betdoc", now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    found = wire_board.find(await wire_board.build(sessions, redis, settings, now), item_id)
    if found is None:  # the cached board may predate the item: read it again once
        found = wire_board.find(await wire_board.build(sessions, redis, settings, now, fresh=True), item_id)
    if found is None:
        raise ChainRefusal("NOT_ON_BOARD", "That item is not on VIDUR's board (aged out, or its fixture left the live board)", 404)
    return await run(sessions, redis, settings, item=found, user_id=user_id, leg_ids=leg_ids, skin=skin, now=now)


async def auto(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, articles: Sequence[Any], catalysts: Sequence[Any],
               now: datetime) -> int:
    """After a news scan: a system chain for each new story at an automatic impact on a tracked fixture, and each new
    catalyst, at most ``NEURAL_BUS_AUTO_MAX_PER_SCAN`` and one per fixture per ``NEURAL_BUS_AUTO_COOLDOWN_SECONDS``."""
    wanted: list[tuple[str, str]] = []
    if settings.NEURAL_BUS_AUTO_CATALYSTS:
        wanted += [(f"news:{a.id}", a.catalyst["fixture_id"]) for a in catalysts if a.catalyst]
    wanted += [(f"news:{a.id}", (a.subject or (a.fixtures or [{}])[0]).get("fixture_id")) for a in articles
               if a.tactical_impact in settings.NEURAL_BUS_AUTO_IMPACTS and (a.subject or a.fixtures)]
    if not wanted or settings.NEURAL_BUS_AUTO_MAX_PER_SCAN == 0:
        return 0
    board = await wire_board.build(sessions, redis, settings, now, fresh=True)
    ran, seen = 0, set()
    for item_id, fixture_id in wanted:
        if ran >= settings.NEURAL_BUS_AUTO_MAX_PER_SCAN:
            break
        if not fixture_id or fixture_id in seen:
            continue
        seen.add(fixture_id)
        item = wire_board.find(board, item_id)
        if item is None or not item.get("fixture"):
            continue
        if not await bus.claim(redis, settings, f"auto:{fixture_id}", settings.NEURAL_BUS_AUTO_COOLDOWN_SECONDS):
            continue
        await run(sessions, redis, settings, item=item, user_id=None, now=now)
        ran += 1
    return ran
