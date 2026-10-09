"""Where BetDoc's events become Sentinel alerts (Group 68): the producers' side of the bus.

* Whale orders: one execution at or above ``SENTINEL_WHALE_STAKE_INR`` (WARNING; five times that is
  CRITICAL). The live odds feeds report no traded volume, so a whale here is an order of ours.
* Margin calls: an account blocked by its drawdown pillar, or whose exposure has reached
  ``SENTINEL_MARGIN_UTILISATION`` of its equity after a fill. Once an hour per account at most.
* Flash crashes: the Hive's breaker halting every bot.
* Hash-chain failures: Nalanda's settlement chain failing verification is FATAL (the ledger's
  history no longer proves itself). Once a day per failure.
* The kill switch: engaged or lifted from the Control Panel (Telegram's own commands raise theirs).

Every check is cheap and never raises into the code that called it; the order path schedules its
check (``watch_soon``) instead of awaiting it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Coroutine, Mapping
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.models.sentinel import Severity
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, emit_alert

logger = logging.getLogger("betdoc.sentinel.watch")

_BACKGROUND: set[asyncio.Task[Any]] = set()
WHALE_CRITICAL_MULTIPLE = Decimal(5)
_MARGIN_ONCE_SECONDS = 3_600
_CHAIN_ONCE_SECONDS = 86_400


def watch_soon(coro: Coroutine[Any, Any, Any]) -> None:
    """Run a watch in the background (a strong reference keeps it alive); outside a loop, drop it."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(_quietly(coro))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


async def _quietly(coro: Coroutine[Any, Any, Any]) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001 - a watch must never surface in the code it watches
        logger.exception("Sentinel watch failed")


async def _once(redis: Redis | None, settings: Settings, key: str, seconds: int) -> bool:
    """True the first time ``key`` is claimed within ``seconds`` (and when Redis cannot say: better twice than never)."""
    if redis is None:
        return True
    try:
        return bool(await redis.set(f"{SentinelKeys(settings).prefix}:once:{key}", "1", nx=True, ex=seconds))
    except (RedisError, OSError):
        return True


def _inr(value: Decimal) -> str:
    return f"₹{value.quantize(Decimal('0.01')):,.2f}"


def _account(user_id: uuid.UUID, bot_id: uuid.UUID | None) -> str:
    return f"bot {bot_id}" if bot_id is not None else f"user {user_id}"


async def watch_execution(
    redis: Redis | None,
    settings: Settings,
    *,
    user_id: uuid.UUID,
    bot_id: uuid.UUID | None,
    idempotency_key: uuid.UUID | str,
    fixture_id: str,
    market: str,
    selection: str,
    bookmaker_id: str,
    stake_inr: Decimal,
    odds: Decimal,
    available: Decimal,
    exposure: Decimal,
    mode: str,
) -> list[SentinelAlert]:
    """After a fill: a whale order, and a margin call when the account is nearly all exposure."""
    alerts: list[SentinelAlert] = []
    threshold = settings.SENTINEL_WHALE_STAKE_INR
    if stake_inr >= threshold:
        critical = stake_inr >= threshold * WHALE_CRITICAL_MULTIPLE
        alerts.append(
            SentinelAlert(
                kind=AlertKind.WHALE_ORDER,
                severity=Severity.CRITICAL if critical else Severity.WARNING,
                title=f"Whale order: {_inr(stake_inr)} on {selection} @ {odds} ({bookmaker_id})",
                body=f"{fixture_id} · {market} · {mode} execution for {_account(user_id, bot_id)}; the whale line is {_inr(threshold)}.",
                source="cfo.execution",
                dedupe_key=f"whale:{idempotency_key}",
                detail={"user_id": str(user_id), "bot_id": str(bot_id) if bot_id else None, "fixture_id": fixture_id, "market": market, "selection": selection,
                        "bookmaker_id": bookmaker_id, "stake_inr": str(stake_inr), "odds": str(odds), "mode": mode, "threshold_inr": str(threshold)},
            )
        )
    equity = available + exposure
    if equity > 0:
        utilisation = exposure / equity
        if utilisation >= settings.SENTINEL_MARGIN_UTILISATION and await _once(redis, settings, f"margin:{user_id}:{bot_id}", _MARGIN_ONCE_SECONDS):
            alerts.append(
                SentinelAlert(
                    kind=AlertKind.MARGIN_CALL,
                    severity=Severity.CRITICAL,
                    title=f"Margin call: {utilisation:.0%} of {_account(user_id, bot_id)}'s bankroll is at risk",
                    body=f"Exposure {_inr(exposure)} against equity {_inr(equity)} ({_inr(available)} free) after the last fill; the line is {settings.SENTINEL_MARGIN_UTILISATION:.0%}.",
                    source="cfo.execution",
                    dedupe_key=f"margin:{user_id}:{bot_id}",
                    detail={"user_id": str(user_id), "bot_id": str(bot_id) if bot_id else None, "exposure_inr": str(exposure), "available_inr": str(available),
                            "utilisation": str(utilisation.quantize(Decimal("0.0001"))), "trigger": "UTILISATION"},
                )
            )
    for alert in alerts:
        await emit_alert(redis, settings, alert)
    return alerts


async def watch_drawdown_block(redis: Redis | None, settings: Settings, *, user_id: uuid.UUID, bot_id: uuid.UUID | None, message: str, detail: Mapping[str, Any]) -> SentinelAlert | None:
    """The drawdown pillar refused an order: the account has hit its daily loss limit."""
    if not await _once(redis, settings, f"margin:{user_id}:{bot_id}:drawdown", _MARGIN_ONCE_SECONDS):
        return None
    alert = SentinelAlert(
        kind=AlertKind.MARGIN_CALL,
        severity=Severity.CRITICAL,
        title=f"Margin call: {_account(user_id, bot_id)} hit its daily drawdown limit",
        body=f"{message}. New orders on this account are refused until the 24h window rolls.",
        source="cfo.risk_guard",
        dedupe_key=f"margin:{user_id}:{bot_id}",
        detail={"user_id": str(user_id), "bot_id": str(bot_id) if bot_id else None, "trigger": "DRAWDOWN", **{k: v for k, v in detail.items() if isinstance(v, str | int | float | None)}},
    )
    await emit_alert(redis, settings, alert)
    return alert


def flash_crash_alert(flag: Mapping[str, Any]) -> SentinelAlert:
    detail = dict(flag.get("detail") or {})
    market = str(detail.get("market", "a market"))
    return SentinelAlert(
        kind=AlertKind.FLASH_CRASH,
        severity=Severity.CRITICAL,
        title=f"Flash crash: every Hive bot halted ({detail.get('swing_pct')}% swing on {market.split('|')[0]})",
        body=(
            f"{market}: consensus probability moved {detail.get('low')} -> {detail.get('high')} over {detail.get('points')} points "
            f"within {detail.get('window_seconds')}s. The halt holds until a person lifts it."
        ),
        source="hive.flash_crash",
        dedupe_key="hive:halt",
        detail=detail,
    )


async def watch_ledger(redis: Redis | None, settings: Settings, report: Mapping[str, Any]) -> SentinelAlert | None:
    """A failed verification of Nalanda's settlement chain is FATAL, once a day per distinct failure."""
    if report.get("ok", True):
        return None
    failures = list(report.get("failures") or [])
    first = failures[0] if failures else {}
    fingerprint = f"{first.get('seq')}:{first.get('problem')}:{report.get('head_seq')}"
    if not await _once(redis, settings, f"chain:{fingerprint}", _CHAIN_ONCE_SECONDS):
        return None
    lines = [f"#{f.get('seq')}: {f.get('problem')} ({str(f.get('detail', ''))[:80]})" for f in failures[:10]]
    alert = SentinelAlert(
        kind=AlertKind.HASH_CHAIN_BROKEN,
        severity=Severity.FATAL,
        title=f"Ledger integrity: the settlement hash chain failed verification ({report.get('failures_total', len(failures))} failure(s))",
        body="\n".join(lines) or "The chain did not verify.",
        source="nalanda.verify",
        dedupe_key="nalanda:chain",
        detail={"head_seq": report.get("head_seq"), "rows": report.get("rows"), "failures_total": report.get("failures_total"), "first": first},
    )
    await emit_alert(redis, settings, alert)
    return alert


def kill_switch_alert(*, engaged: bool, by: str) -> SentinelAlert:
    if engaged:
        return SentinelAlert(
            kind=AlertKind.KILL_SWITCH,
            severity=Severity.CRITICAL,
            title=f"Kill switch engaged ({by})",
            body="Bots disabled, daily exposure zeroed; every execution is refused until it is lifted.",
            source=by,
            dedupe_key="kill-switch",
        )
    return SentinelAlert(
        kind=AlertKind.KILL_SWITCH_LIFTED,
        severity=Severity.WARNING,
        title=f"Kill switch lifted ({by})",
        body="Trading limits restored; executions are accepted again.",
        source=by,
        dedupe_key="kill-switch",
        resolves=True,
    )
