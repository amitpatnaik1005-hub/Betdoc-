"""The 08:00 market forecast: a push to the admin's phone when today is worth it (Group 68).

Every morning at ``SENTINEL_HYPE_HOUR:SENTINEL_HYPE_MINUTE`` in ``SENTINEL_TIMEZONE`` the math engine is
asked what today looks like:

* volume: the fixtures starting today (local day), from the live board and Aryabhata's edges;
* value: Aryabhata's live +EV edges on those fixtures (the best per fixture, market and selection),
  their summed EV, and the best single line;
* volatility: how many of those lines are steam moves (sharp money moving the consensus).

``classify`` turns that into a tier. ``BUSSIN`` (twice the EV bar), ``PRIMED`` (the EV bar on enough
fixtures) and ``VOLATILE`` (enough steam) each have their own pool of lines; ``QUIET`` sends nothing.
A line is picked by a hash of the date, skipping the last few used, so mornings do not repeat. The
alert is INFO and rides the routing matrix's ``HYPE`` row (Telegram and SMS by default). Whoever runs
first (Celery beat, or the API's watchdog catching up after a late start) sends it; a per-day key
makes sure nobody sends it twice. The numbers travel with the hype: the message never claims more
than the board shows, and the risk guard still decides every stake.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.live_odds import read_snapshot
from app.models.sentinel import Severity
from app.services.aryabhata_pipeline import read_active_edges
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, emit_alert

logger = logging.getLogger("betdoc.sentinel.hype")

_RECENT = 5  # lines remembered so the next mornings pick something else
CATCH_UP_HOURS = 4  # an API that starts late still sends the morning forecast until this long after


class Tier(StrEnum):
    BUSSIN = "BUSSIN"
    PRIMED = "PRIMED"
    VOLATILE = "VOLATILE"
    QUIET = "QUIET"


HYPE_LINES: dict[Tier, tuple[str, ...]] = {
    Tier.BUSSIN: (
        "No cap, the EV today is bussin. Let's secure the bag. 💰",
        "{fixtures} fixtures, {edges} live edges, +{ev}% EV on the board. It's giving main character energy. 🚀",
        "The market said 'skill issue' and left +{ev}% EV on the table. Let's eat. 🍽️",
        "EV this high should be illegal. {edges} edges across {fixtures} fixtures. Lock in. 🔒",
        "Big W day loading: {fixtures} fixtures and +{ev}% EV. Touch grass later, secure the bag now. 🌱💸",
        "Best line on the board: {best}. The vibes are immaculate. ✨",
        "Ate and left no crumbs: {edges} edges live before breakfast. 🥐",
        "This board is a whole snack. +{ev}% EV, {fixtures} fixtures. Periodt. 💅",
    ),
    Tier.PRIMED: (
        "Good day to make profits, let's go make some! {fixtures} fixtures, +{ev}% EV. 📈",
        "The board is cooking 🍳 {edges} edges across {fixtures} fixtures. Slay responsibly.",
        "Not to be dramatic, but +{ev}% EV is just sitting there. Rise and grind. ☕",
        "{fixtures} fixtures on the menu today. The EV? Chef's kiss. 👨‍🍳",
        "Your bankroll called. It wants {best}. 📞",
        "Lowkey a W morning: {edges} edges, +{ev}% EV. Let him cook. 🔥",
    ),
    Tier.VOLATILE: (
        "Market volatility is spiking. Good day to make profits, let's go make some! ⚡",
        "Sharp money is moving {steam} lines already. The market is NOT chill today. 🌪️",
        "Lines are doing parkour this morning ({steam} steam moves). Eyes on the board. 👀",
        "It's giving chaos: {steam} steam moves before breakfast. Stay locked in. 🎢",
    ),
}


@dataclass(slots=True)
class BestLine:
    fixture: str
    selection: str
    market: str
    bookmaker: str
    odds: str
    ev_percent: str

    def label(self) -> str:
        return f"{self.fixture}, {self.selection} @ {self.odds} ({self.bookmaker}, +{self.ev_percent}% EV)"


@dataclass(slots=True)
class MarketForecast:
    day: date
    timezone: str
    fixtures_today: int
    live_edges: int
    total_ev_pct: Decimal
    steam_moves: int
    sports: dict[str, int] = field(default_factory=dict)
    best: BestLine | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def mean_ev_pct(self) -> Decimal:
        return (self.total_ev_pct / self.live_edges).quantize(Decimal("0.01")) if self.live_edges else Decimal(0)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out.update(day=self.day.isoformat(), total_ev_pct=str(self.total_ev_pct), mean_ev_pct=str(self.mean_ev_pct), generated_at=self.generated_at.isoformat())
        return out


def local_day(now: datetime, tz: str) -> tuple[date, datetime, datetime]:
    """Today in ``tz``, and its bounds in UTC."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    start = datetime.combine(local.date(), time.min, tzinfo=zone)
    return local.date(), start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def build_forecast(redis: Redis, settings: Settings, now: datetime) -> MarketForecast:
    """What the math engine sees for today; raises RedisError/OSError when Redis cannot answer."""
    day, start, end = local_day(now, settings.SENTINEL_TIMEZONE)
    board = await read_snapshot(redis) or []
    edges = await read_active_edges(redis, settings, now)
    fixtures: set[str] = set()
    sports: Counter[str] = Counter()
    for tick in board:
        commence = _aware(tick.commence_time)
        if commence is not None and start <= commence < end and tick.match_id not in fixtures:
            fixtures.add(tick.match_id)
            sports[tick.sport_key or "unknown"] += 1
    best_per_line: dict[tuple[str, str, str], Any] = {}
    for edge in edges:
        commence = _aware(edge.commence_time)
        if commence is None or not start <= commence < end:
            continue
        if edge.fixture_id not in fixtures:
            fixtures.add(edge.fixture_id)
            sports[edge.sport_key or "unknown"] += 1
        key = (edge.fixture_id, edge.market_type, edge.selection)
        if key not in best_per_line or edge.ev_percent > best_per_line[key].ev_percent:
            best_per_line[key] = edge
    chosen = list(best_per_line.values())
    total = sum((Decimal(e.ev_percent) for e in chosen), Decimal(0)).quantize(Decimal("0.01"))
    top = max(chosen, key=lambda e: e.ev_percent, default=None)
    best = None
    if top is not None:
        best = BestLine(f"{top.home_team} v {top.away_team}", top.selection, top.market_type, top.bookmaker_id, f"{Decimal(top.odds):.2f}", f"{Decimal(top.ev_percent):.1f}")
    return MarketForecast(day, settings.SENTINEL_TIMEZONE, len(fixtures), len(chosen), total, sum(1 for e in chosen if e.is_steam_move), dict(sports), best, now)


def classify(forecast: MarketForecast, settings: Settings) -> Tier:
    busy = forecast.fixtures_today >= settings.SENTINEL_HYPE_MIN_FIXTURES
    if busy and forecast.total_ev_pct >= settings.SENTINEL_HYPE_MIN_TOTAL_EV_PCT * 2:
        return Tier.BUSSIN
    if busy and forecast.total_ev_pct >= settings.SENTINEL_HYPE_MIN_TOTAL_EV_PCT:
        return Tier.PRIMED
    if forecast.steam_moves >= settings.SENTINEL_HYPE_MIN_STEAM_MOVES and forecast.fixtures_today >= max(settings.SENTINEL_HYPE_MIN_FIXTURES // 2, 1):
        return Tier.VOLATILE
    return Tier.QUIET


def pick_line(tier: Tier, day: date, recent: Sequence[str]) -> tuple[str, str]:
    """(line id, template): a date-seeded order through the tier's pool, skipping recent picks."""
    pool = HYPE_LINES[tier]
    seed = int.from_bytes(hashlib.blake2b(f"{day.isoformat()}|{tier}".encode(), digest_size=8).digest(), "big")
    order = sorted(range(len(pool)), key=lambda i: hashlib.blake2b(f"{seed}|{i}".encode(), digest_size=8).digest())
    for index in order:
        line_id = f"{tier}:{index}"
        if line_id not in recent:
            return line_id, pool[index]
    return f"{tier}:{order[0]}", pool[order[0]]


def render(template: str, forecast: MarketForecast) -> str:
    best = forecast.best.label() if forecast.best else "the best line on the board"
    return template.format(fixtures=forecast.fixtures_today, edges=forecast.live_edges, ev=f"{forecast.total_ev_pct:.1f}", best=best, steam=forecast.steam_moves)


def summary(forecast: MarketForecast) -> str:
    lines = [
        f"Today ({forecast.timezone}, {forecast.day:%a %d %b}): {forecast.fixtures_today} fixtures · {forecast.live_edges} live edges · "
        f"total EV +{forecast.total_ev_pct:.1f}% (mean +{forecast.mean_ev_pct:.1f}%) · {forecast.steam_moves} steam moves",
    ]
    if forecast.best is not None:
        lines.append(f"Best line: {forecast.best.label()}")
    if forecast.sports:
        lines.append("By sport: " + ", ".join(f"{sport} {n}" for sport, n in sorted(forecast.sports.items(), key=lambda kv: -kv[1])))
    lines.append("Edges move fast; the risk guard still sizes and vets every stake.")
    return "\n".join(lines)


def is_due(now: datetime, settings: Settings) -> bool:
    """Between the configured time and ``CATCH_UP_HOURS`` after it, local time."""
    local = now.astimezone(ZoneInfo(settings.SENTINEL_TIMEZONE))
    at = local.replace(hour=settings.SENTINEL_HYPE_HOUR, minute=settings.SENTINEL_HYPE_MINUTE, second=0, microsecond=0)
    return at <= local < at + timedelta(hours=CATCH_UP_HOURS)


async def market_forecast_hype(redis: Redis, settings: Settings, *, now: datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Build today's forecast and, unless it is quiet (or a dry run), push the hype once for the day."""
    now = now or datetime.now(UTC)
    keys = SentinelKeys(settings)
    forecast = await build_forecast(redis, settings, now)
    tier = classify(forecast, settings)
    result: dict[str, Any] = {"tier": tier, "forecast": forecast.as_dict(), "sent": False, "dry_run": dry_run}
    if tier is not Tier.QUIET:
        recent = await redis.lrange(keys.hype_recent, 0, _RECENT - 1)
        line_id, template = pick_line(tier, forecast.day, recent)
        title = render(template, forecast)
        result.update(line_id=line_id, title=title, body=summary(forecast))
        if not dry_run and await redis.set(f"{keys.prefix}:hype:sent:{forecast.day.isoformat()}", line_id, nx=True, ex=2 * 86_400):
            alert = SentinelAlert(
                kind=AlertKind.MARKET_HYPE,
                severity=Severity.INFO,
                title=title[:200],
                body=summary(forecast),
                source="sentinel.hype",
                dedupe_key=f"hype:{forecast.day.isoformat()}",
                detail={"tier": tier, "line_id": line_id, **forecast.as_dict()},
                occurred_at=now,
            )
            result["sent"] = await emit_alert(redis, settings, alert)
            pipe = redis.pipeline(transaction=False)
            pipe.lpush(keys.hype_recent, line_id)
            pipe.ltrim(keys.hype_recent, 0, _RECENT - 1)
            await pipe.execute()
        elif not dry_run:
            result["already_sent_today"] = True
    if not dry_run:
        try:
            await redis.set(keys.hype_last, json.dumps({**result, "at": now.isoformat()}, default=str))
        except (RedisError, OSError):
            pass
    logger.info("Sentinel hype %s: %s (%s)", forecast.day, tier, "sent" if result["sent"] else "not sent")
    return result
