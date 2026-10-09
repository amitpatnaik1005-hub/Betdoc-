"""Group 66 gap patch: the Hive's flash-crash breaker, end to end.

The breaker's rule: a selection whose consensus probability (the fair price across every fresh
book) swings by more than ``HIVE_FLASH_THRESHOLD_PCT`` (15%) within ``HIVE_FLASH_WINDOW_SECONDS``
(120s), measured as ``(max - min) / min``, halts every bot of every owner on every market until a
person lifts it. These tests drive the real worker: signals arrive on the Redis stream, the leader
takes its Redis lease and runs maintenance, the breaker writes the global halt, and then the same
stream decides nothing, queued TWAP slices cancel, and lifting the halt is the only thing that
brings the bots back.

The swing is checked on both sides of the line (14.9% holds, 15.1% halts) and then against the
formula itself over a few hundred seeded random price paths, including points that fall outside the
window. SQLite ledger, real Redis on the flushed test index (see ``test_hive_automation``).

Group 68 gap patch: only a selection priced at ``HIVE_FLASH_MIN_PROBABILITY`` (10%, decimal odds
10.0 or shorter) somewhere in the window can trip the breaker. A longshot drifting 3% -> 5% is a
67% relative swing and means nothing; the live scan and the backtester both ignore it.
"""

from __future__ import annotations

import random
import time
import uuid
from decimal import Decimal

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models import User
from app.models.cfo_vault import PhantomLedger
from app.models.hive_bots import BotExecutionMode, BotStatus, HiveBotEvent, HiveEventKind, HiveOrderPlan, HiveShadowPosition, PlanStatus, TradingBot
from app.services.cfo_ledger import lock_bankroll
from app.services.hive_engine import HiveKeys, Proposal, clear_halt, read_halt
from app.workers.hive_worker import HiveWorker, gateways
from tests.test_hive_automation import (  # noqa: F401 - the fixtures are used by name
    FIXTURE,
    Recorder,
    edge,
    engine_for,
    make_bot,
    owner,
    put_books,
    redis,
    rows,
    sessions,
    settings,
)

D = Decimal
CELL = f"{FIXTURE}|Match Odds|HOME"  # the market that swings
CALM = "fx-che-liv"  # trades before the crash (a swinging market's own bets meet the G62 velocity lock first)
PLANNED = "fx-mci-tot"  # the TWAP plan's market
ELSEWHERE = ("fx-new-bha", "fx-bur-wol")  # markets nowhere near the crash
RESUMED = "fx-whu-eve"  # the first market traded after the halt is lifted


def worker(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings) -> HiveWorker:
    return HiveWorker(redis, sessions, settings, gateways(settings, None), Recorder())


async def maintain(leader: HiveWorker) -> None:
    """One maintenance pass now: the worker rate-limits its own scans, so reset its scan clock."""
    leader._scanned = float("-inf")  # noqa: SLF001
    await leader.maintenance()


async def board(redis: Redis, settings: Settings, cell: str, path: list[tuple[float, float]]) -> None:
    """A selection's consensus-probability history: ``(seconds ago, probability)`` points, in the
    exact member format the live-odds board writes (``ts|odds|prob``)."""
    now = time.time()
    channel = settings.LIVE_ODDS_CHANNEL
    await redis.zadd(f"{channel}:board:ts", {cell: now})
    for ago, prob in path:
        at = now - ago
        await redis.zadd(f"{channel}:hist:{cell}", {f"{at:.6f}|{1 / prob:.4f}|{prob!r}": at})


async def stream(redis: Redis, settings: Settings, *signals: object) -> None:
    for signal in signals:
        await redis.xadd(HiveKeys(settings).signals, {"e": signal.model_dump_json()})  # type: ignore[attr-defined]


async def drain(leader: HiveWorker) -> int:
    handled = 0
    while (n := await leader.step()) > 0:
        handled += n
    return handled


async def new_owner(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> uuid.UUID:
    """A second, independent owner with its own main account (what the ``owner`` fixture builds)."""
    async with sessions() as session:
        user = User(username=f"hive_{uuid.uuid4().hex[:6]}", hashed_password="x")
        session.add(user)
        await session.commit()
        await lock_bankroll(session, user.id, settings)
        await session.commit()
        return user.id


async def bets_and_shadows(sessions: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    return len(await rows(sessions, PhantomLedger)), len(await rows(sessions, HiveShadowPosition))


# ================================================================ the brief's proof
@pytest.mark.asyncio
async def test_a_15_percent_swing_on_one_market_halts_every_active_bot_end_to_end(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID
) -> None:
    """Two owners, paper and shadow bots and a queued TWAP plan, trading through the stream. A
    14.9% swing changes nothing; the next tick takes it to 15.1% and the leader's maintenance pass
    halts the whole Hive: every later signal on every market is decided by no one, the plan's next
    slice cancels, and nothing moves in any ledger until the halt is lifted."""
    second = await new_owner(sessions, settings)
    paper = await make_bot(sessions, settings, owner, name="Paper A")
    shadow = await make_bot(sessions, settings, owner, name="Shadow A", mode=BotExecutionMode.SHADOW_MODE, capital="50000.00")
    slicer = await make_bot(sessions, settings, second, name="Slicer B", capital="1000000.00", enable_order_slicing=True, slice_size_inr=D("10000.00"))
    paused = await make_bot(sessions, settings, second, name="Paused B")
    async with sessions() as session:
        found = await session.get(TradingBot, paused.id)
        assert found is not None
        found.status = BotStatus.PAUSED
        await session.commit()

    for fixture in (FIXTURE, CALM, PLANNED, *ELSEWHERE, RESUMED):
        await put_books(redis, settings, fixture)
    leader = worker(sessions, redis, settings)
    await leader.ensure_group()

    # 1. Live and trading: 0.40 -> 0.4596 is a 14.9% swing, inside the limit
    await board(redis, settings, CELL, [(100, 0.40), (70, 0.42), (40, 0.44), (10, 0.4596)])
    await maintain(leader)
    assert await read_halt(redis, settings) is None
    await stream(redis, settings, edge("HOME", fixture=CALM))
    assert await drain(leader) == 1
    before = await bets_and_shadows(sessions)
    assert before == (1, 1)  # the paper bot bet, the shadow bot logged its position (the slicer queued a plan)
    assert [b.bot_id for b in await rows(sessions, PhantomLedger)] == [paper.id]
    assert [s.bot_id for s in await rows(sessions, HiveShadowPosition)] == [shadow.id]

    hive = engine_for(sessions, redis, settings)
    plan_decision = await hive._plan(  # noqa: SLF001 - the slicer's queued plan, its later slices still to fire
        slicer, Proposal(slicer.id, D("0.476"), D("0.0948"), D("30000.00"), D("30000.00"), {"math.consensus": "0.476"}, ()), edge("HOME", fixture=PLANNED)
    )
    assert plan_decision.event is HiveEventKind.SLICED and plan_decision.plan_id is not None

    # 2. The next tick: 0.40 -> 0.4604 is 15.1%. The leader halts everything
    await board(redis, settings, CELL, [(5, 0.4604)])
    await maintain(leader)
    flag = await read_halt(redis, settings)
    assert flag is not None and flag["reason"] == "FLASH_CRASH" and flag["by"] == "hive"
    detail = flag["detail"]
    assert detail["market"] == CELL and detail["low"] == 0.40 and detail["high"] == 0.4604
    assert detail["swing_pct"] == pytest.approx((0.4604 - 0.40) / 0.40 * 100, abs=0.01) and detail["swing_pct"] > settings.HIVE_FLASH_THRESHOLD_PCT
    halted = await rows(sessions, HiveBotEvent, HiveBotEvent.event == HiveEventKind.HALTED)
    assert {e.user_id for e in halted} == {owner, second} and all(e.reason == "FLASH_CRASH" for e in halted)  # every owner with an active bot is told

    # 3. Market-wide: a signal on every other market is decided by no bot
    signals = [edge("HOME", fixture=f) for f in (CALM, PLANNED, *ELSEWHERE)] + [edge("AWAY", odds="4.40", fixture=ELSEWHERE[0]), edge("HOME", fixture=FIXTURE)]
    await stream(redis, settings, *signals)
    assert await drain(leader) == len(signals)  # consumed (and acknowledged) ...
    assert await bets_and_shadows(sessions) == before  # ... and not one rupee or shadow position moved
    fired_after = await rows(sessions, HiveBotEvent, HiveBotEvent.event.in_([HiveEventKind.FIRED, HiveEventKind.SHADOW_FILLED, HiveEventKind.SLICED]), HiveBotEvent.created_at > halted[0].created_at)
    assert fired_after == []

    # 4. The queued TWAP plan dies when its first slice comes due, and takes the rest with it
    cancelled = await hive.run_slice(plan_decision.plan_id, 0)
    assert cancelled is not None and (cancelled.event, cancelled.reason) == (HiveEventKind.SLICE_CANCELLED, "HALTED")
    (plan,) = await rows(sessions, HiveOrderPlan, HiveOrderPlan.id == plan_decision.plan_id)
    assert plan.status is PlanStatus.CANCELLED and len(plan.slices) == 3 and all(s["status"] == "CANCELLED" for s in plan.slices)

    # 5. The halt holds through later scans (it is never lifted automatically) ...
    await board(redis, settings, CELL, [(1, 0.45)])
    await maintain(leader)
    assert (await read_halt(redis, settings) or {}).get("reason") == "FLASH_CRASH"

    # 6. ... and lifting it is what brings the bots back: the same kind of signal fires again
    await clear_halt(redis, settings)
    await stream(redis, settings, edge("HOME", fixture=RESUMED))
    assert await drain(leader) == 1
    after = await bets_and_shadows(sessions)
    assert after[0] > before[0] and after[1] == before[1] + 1


# ================================================================ the rule itself
@pytest.mark.asyncio
async def test_the_line_is_15_percent_either_side(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:  # noqa: ARG001
    """``(max - min) / min``: a rise and a fall are both swings; 14.99% holds, 15.01% halts."""
    hive = engine_for(sessions, redis, settings)
    cases = [
        ([(90, 0.40), (60, 0.42), (30, 0.45996)], False),  # +14.99%
        ([(90, 0.40), (60, 0.42), (30, 0.46004)], True),  # +15.01%
        ([(90, 0.46004), (60, 0.43), (30, 0.40)], True),  # the same swing, falling: a crash either way
        ([(90, 0.30), (60, 0.31), (30, 0.3449)], False),  # +14.97% from a lower base
        ([(90, 0.30), (60, 0.33), (30, 0.3451)], True),  # +15.03%
    ]
    for i, (path, trips) in enumerate(cases):
        cell = f"fx-line-{i}|Match Odds|HOME"
        await board(redis, settings, cell, path)
        flag = await hive.flash_crash_scan()
        assert (flag is not None) is trips, (path, flag)
        if flag is not None:
            low, high = min(p for _, p in path), max(p for _, p in path)
            assert flag["detail"]["swing_pct"] == round((high - low) / low * 100, 2)
            await clear_halt(redis, settings)
        await redis.delete(f"{settings.LIVE_ODDS_CHANNEL}:hist:{cell}")
        await redis.zrem(f"{settings.LIVE_ODDS_CHANNEL}:board:ts", cell)


@pytest.mark.asyncio
async def test_the_breaker_matches_the_formula_on_random_paths(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID) -> None:  # noqa: ARG001
    """300 seeded random paths: the breaker trips exactly when the in-window points (at least
    ``HIVE_FLASH_MIN_POINTS`` of them) swing by more than 15%. Points older than the window, however
    wild, count for nothing."""
    rng = random.Random(66)
    hive = engine_for(sessions, redis, settings)
    window, threshold, min_points = settings.HIVE_FLASH_WINDOW_SECONDS, settings.HIVE_FLASH_THRESHOLD_PCT, settings.HIVE_FLASH_MIN_POINTS
    tripped = held = 0
    for i in range(300):
        cell = f"fx-rand-{i}|Match Odds|DRAW"
        base = rng.uniform(0.02, 0.7)  # longshots under the 10% floor as well as real contenders
        inside = [(rng.uniform(1, window - 5), min(max(base * (1 + rng.gauss(0, 0.07)), 0.01), 0.99)) for _ in range(rng.randint(1, 7))]
        outside = [(rng.uniform(window + 10, window + 600), rng.uniform(0.01, 0.99)) for _ in range(rng.randint(0, 3))]
        await board(redis, settings, cell, inside + outside)
        probs = [p for _, p in inside]
        expected = len(probs) >= min_points and max(probs) >= settings.HIVE_FLASH_MIN_PROBABILITY and (max(probs) - min(probs)) / min(probs) * 100 > threshold
        flag = await hive.flash_crash_scan()
        assert (flag is not None) is expected, (inside, outside, flag)
        tripped += expected
        held += not expected
        if flag is not None:
            await clear_halt(redis, settings)
        await redis.delete(f"{settings.LIVE_ODDS_CHANNEL}:hist:{cell}")
        await redis.zrem(f"{settings.LIVE_ODDS_CHANNEL}:board:ts", cell)
    assert tripped > 20 and held > 20  # the sample exercises both sides of the line


@pytest.mark.asyncio
async def test_a_longshot_swing_never_halts_the_hive_live_or_in_a_backtest(
    sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, owner: uuid.UUID  # noqa: ARG001
) -> None:
    """Relative swings on longshots are noise: 3% -> 5% (odds 33 -> 20) is +67% and holds, as does
    9.9% -> 9.0%-> 9.99%. The floor is the window's high, so a favourite collapsing from 40% to 8%
    still halts, and 10.0% exactly (odds 10.0) is eligible. The backtester's shock detector reaches
    the same verdict on the same points."""
    from datetime import UTC, datetime, timedelta  # noqa: PLC0415

    from app.services.backtesting.replay_engine import ReplayEngine  # noqa: PLC0415
    from app.services.hive_pipeline import flash_swing  # noqa: PLC0415

    assert settings.HIVE_FLASH_MIN_PROBABILITY == 0.10
    hive = engine_for(sessions, redis, settings)
    cases = [
        ([(90, 0.03), (60, 0.04), (30, 0.05)], False),  # +67%, a longshot: noise
        ([(90, 0.099), (60, 0.0901), (30, 0.0999)], False),  # under the floor throughout
        ([(90, 0.08), (60, 0.09), (30, 0.10)], True),  # +25% and reaching 10.0% (odds 10.0): eligible
        ([(90, 0.40), (60, 0.20), (30, 0.08)], True),  # a favourite collapsing below 10%: its high counts
        ([(90, 0.05), (60, 0.30), (30, 0.06)], True),  # a longshot spiking into contention
    ]
    replay = ReplayEngine.__new__(ReplayEngine)  # only _shock is exercised: it reads settings alone
    replay.settings = settings
    t0 = datetime(2026, 10, 9, 12, tzinfo=UTC)
    for i, (path, trips) in enumerate(cases):
        cell = f"fx-long-{i}|Match Odds|AWAY"
        await board(redis, settings, cell, path)
        flag = await hive.flash_crash_scan()
        assert (flag is not None) is trips, (path, flag)
        probs = [p for _, p in path]
        pure = flash_swing(probs, threshold_pct=settings.HIVE_FLASH_THRESHOLD_PCT, min_points=settings.HIVE_FLASH_MIN_POINTS, min_probability=settings.HIVE_FLASH_MIN_PROBABILITY)
        assert (pure is not None) is trips
        points = [(t0 + timedelta(seconds=120 - ago), p) for ago, p in path]
        shock = replay._shock(cell, points, points[-1][0], timedelta(seconds=settings.HIVE_FLASH_WINDOW_SECONDS), {})  # noqa: SLF001
        assert (shock is not None) is trips, (path, shock)
        if flag is not None:
            assert flag["detail"]["swing_pct"] == shock.swing_pct  # type: ignore[union-attr]
            await clear_halt(redis, settings)
        await redis.delete(f"{settings.LIVE_ODDS_CHANNEL}:hist:{cell}")
        await redis.zrem(f"{settings.LIVE_ODDS_CHANNEL}:board:ts", cell)
