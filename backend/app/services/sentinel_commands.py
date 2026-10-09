"""Two-way Telegram: ``/halt``, ``/resume``, ``/status`` from the admin's phone (Group 68).

Who may command: a message from one of the Telegram channel's configured ``chat_ids`` and, when
``admin_user_ids`` is set, from one of those users. Anyone else gets silence (no reply that would
confirm the bot exists), and the attempt is logged.

* ``/halt`` is the Control Panel's emergency stop, exactly: bots disabled, daily exposure zeroed, the
  Redis kill switch every execution checks first, risk limits republished (stakes drop to 0). What it
  overwrote is kept (Redis, and the command log as the durable copy) so ``/resume`` can put it back.
* ``/resume`` lifts only a halt that ``/halt`` engaged, and only in two steps: it answers with a
  one-time code, and ``/resume <code>`` from the same chat within ``SENTINEL_RESUME_CONFIRM_SECONDS``
  restores the saved limits. A stop engaged from the Control Panel is lifted there, not from a phone.
* ``/status``: the last 24h realised P&L, bankroll and exposure, open bets, the bots by state, the
  kill switch and Hive halt, Garuda's heartbeat, and the dependency health.
"""

from __future__ import annotations

import hmac
import html
import json
import logging
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.control_panel.manager import ControlPanelManager
from app.models.cfo_vault import OPEN_STATUSES, AuditEvent, AuditLog, BankrollAccount, PhantomLedger
from app.models.control_panel import SETTINGS_SINGLETON_ID, SystemSettingsModel
from app.models.hive_bots import TradingBot
from app.models.sentinel import SentinelCommandLog, Severity
from app.services.aryabhata_pipeline import publish_risk_limits
from app.services.risk_guard import kill_switch_engaged, set_kill_switch
from app.services.sentinel_bus import AlertKind, SentinelAlert, SentinelKeys, emit_alert
from app.services.sentinel_health import GARUDA, read_health

logger = logging.getLogger("betdoc.sentinel.commands")

COMMANDS = ("/halt", "/resume", "/status", "/help", "/start")
HELP = (
    "<b>BetDoc Sentinel</b>\n"
    "/status: P&amp;L, bankroll, bots, kill switch, feeds\n"
    "/halt: engage the global kill switch (the emergency stop)\n"
    "/resume: lift a halt you engaged here (asks for a one-time code)"
)


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    update_id: int
    chat_id: int | None
    sender_id: int | None
    sender: str | None  # @username, else the numeric id
    text: str

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> TelegramUpdate | None:
        """A text message, or None (edits, joins, callbacks and anything else are ignored)."""
        try:
            update_id = int(body["update_id"])
        except (KeyError, TypeError, ValueError):
            return None
        message = body.get("message")
        if not isinstance(message, Mapping) or not isinstance(message.get("text"), str):
            return cls(update_id, None, None, None, "")
        chat = message.get("chat") if isinstance(message.get("chat"), Mapping) else {}
        sender = message.get("from") if isinstance(message.get("from"), Mapping) else {}
        sender_id = sender.get("id") if isinstance(sender.get("id"), int) else None
        username = sender.get("username")
        label = f"@{username}" if isinstance(username, str) and username else (str(sender_id) if sender_id is not None else None)
        chat_id = chat.get("id") if isinstance(chat.get("id"), int) else None
        return cls(update_id, chat_id, sender_id, label, message["text"].strip()[:200])

    @property
    def command(self) -> tuple[str, list[str]]:
        """``/halt@BetDocBot now`` -> ("/halt", ["now"])."""
        parts = self.text.split()
        if not parts or not parts[0].startswith("/"):
            return "", parts
        return parts[0].split("@", 1)[0].lower(), parts[1:]


@dataclass(slots=True)
class CommandReply:
    text: str | None  # None: say nothing
    outcome: str
    authorised: bool
    detail: dict[str, Any] = field(default_factory=dict)


def authorised(update: TelegramUpdate, config: Mapping[str, Any]) -> bool:
    chats = {int(c) for c in config.get("chat_ids") or [] if str(c).lstrip("-").isdigit()}
    admins = {int(u) for u in config.get("admin_user_ids") or [] if str(u).lstrip("-").isdigit()}
    if update.chat_id is None or update.chat_id not in chats:
        return False
    return not admins or (update.sender_id is not None and update.sender_id in admins)


def _inr(value: Decimal | float | int) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{abs(amount):,.2f}"


class CommandCenter:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.session_factory, self.redis, self.settings, self.clock = session_factory, redis, settings, clock
        self.keys = SentinelKeys(settings)

    async def handle(self, update: TelegramUpdate, config: Mapping[str, Any]) -> CommandReply:
        command, args = update.command
        if not authorised(update, config):
            reply = CommandReply(None, "IGNORED", False, {"command": command or None})
        elif command == "/halt":
            reply = await self.halt(update)
        elif command == "/resume":
            reply = await self.resume(update, args[0] if args else None)
        elif command == "/status":
            reply = await self.status()
        elif command in ("/help", "/start"):
            reply = CommandReply(HELP, "HELP", True)
        else:
            reply = CommandReply(f"Unknown command. {HELP}", "UNKNOWN", True)
        await self._log(update, command or "(text)", reply)
        return reply

    # -------------------------------------------------------------- /halt
    async def halt(self, update: TelegramUpdate) -> CommandReply:
        by = f"telegram:{update.sender or update.chat_id}"
        async with self.session_factory() as db:
            manager = ControlPanelManager()
            before = await manager.get_settings(db)  # created with its defaults if it never existed: those are the limits in force
            was_halted = before.max_daily_exposure <= 0
            snapshot = None if was_halted else {"max_daily_exposure": float(before.max_daily_exposure), "bots_enabled": bool(before.bots_enabled)}
            row = await manager.emergency_stop(db)
            halted_at = _aware(row.last_emergency_stop_at) if row.last_emergency_stop_at else None
        switched = await set_kill_switch(self.redis, self.settings, engaged=True)
        await publish_risk_limits(self.redis, row, self.settings)
        detail: dict[str, Any] = {"by": by, "halted_at": halted_at.isoformat() if halted_at else None, "kill_switch_flag": switched}
        if snapshot is not None:
            detail["snapshot"] = {**snapshot, "halted_at": detail["halted_at"], "by": by}
            if self.redis is not None:
                try:
                    await self.redis.set(self.keys.halt_snapshot, json.dumps(detail["snapshot"]))
                except (RedisError, OSError):
                    pass
        await emit_alert(
            self.redis,
            self.settings,
            SentinelAlert(
                kind=AlertKind.KILL_SWITCH,
                severity=Severity.CRITICAL,
                title=f"Kill switch engaged from Telegram by {update.sender or 'an admin'}",
                body="Bots disabled, daily exposure zeroed; every execution is refused until it is lifted.",
                source="sentinel.telegram",
                dedupe_key="kill-switch",
                detail=detail,
            ),
        )
        note = "" if switched else "\n⚠️ The Redis kill switch could not be set; the emergency stop in the database still blocks every trade."
        if was_halted:
            return CommandReply(f"🛑 <b>Already halted.</b> The emergency stop was on; it has been re-applied.{note}", "HALTED", True, detail)
        return CommandReply(f"🛑 <b>Kill switch engaged.</b>\nBots disabled, daily exposure zeroed at {halted_at:%H:%M:%S} UTC.{note}\nSend /resume to lift it.", "HALTED", True, detail)

    # -------------------------------------------------------------- /resume
    async def _snapshot(self) -> dict[str, Any] | None:
        if self.redis is not None:
            try:
                raw = await self.redis.get(self.keys.halt_snapshot)
                if raw:
                    return json.loads(raw)
            except (RedisError, OSError, ValueError):
                pass
        async with self.session_factory() as db:  # the durable copy
            row = await db.scalar(
                select(SentinelCommandLog).where(SentinelCommandLog.outcome == "HALTED", SentinelCommandLog.authorised.is_(True)).order_by(SentinelCommandLog.received_at.desc()).limit(1)
            )
        snapshot = (row.detail or {}).get("snapshot") if row is not None else None
        return snapshot if isinstance(snapshot, dict) else None

    async def resume(self, update: TelegramUpdate, code: str | None) -> CommandReply:
        if self.redis is None:
            return CommandReply("Redis is unreachable; a resume cannot be confirmed safely. Try again shortly.", "REFUSED", True)
        async with self.session_factory() as db:
            row = await db.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
        switch = await kill_switch_engaged(self.redis, self.settings)
        stopped = row is not None and row.max_daily_exposure <= 0
        if not stopped and not switch:
            return CommandReply("✅ Trading is not halted.", "REFUSED", True)
        snapshot = await self._snapshot()
        halted_at = row.last_emergency_stop_at if row is not None else None
        if snapshot is None or halted_at is None or snapshot.get("halted_at") != _aware(halted_at).isoformat() or snapshot.get("max_daily_exposure") in (None, 0):
            when = f" at {_aware(halted_at):%Y-%m-%d %H:%M} UTC" if halted_at else ""
            return CommandReply(f"🔒 This stop was engaged from the Control Panel{when}. Lift it there, where the limits are set.", "REFUSED", True)
        if code is None:
            issued = secrets.token_hex(3).upper()
            payload = json.dumps({"code": issued, "chat_id": update.chat_id})
            await self.redis.set(self.keys.resume_code, payload, ex=self.settings.SENTINEL_RESUME_CONFIRM_SECONDS)
            limit = _inr(snapshot["max_daily_exposure"])
            bots = "enabled" if snapshot.get("bots_enabled") else "disabled"
            return CommandReply(
                f"⚠️ Resume trading with daily exposure {limit} and bots {bots}?\nConfirm within {self.settings.SENTINEL_RESUME_CONFIRM_SECONDS}s: <code>/resume {issued}</code>",
                "CONFIRM_SENT",
                True,
            )
        raw = await self.redis.get(self.keys.resume_code)
        try:
            pending = json.loads(raw) if raw else None
        except ValueError:
            pending = None
        if pending is None or pending.get("chat_id") != update.chat_id or not hmac.compare_digest(str(pending.get("code", "")), code.strip().upper()):
            return CommandReply("That code is wrong or has expired. Send /resume again.", "REFUSED", True)
        await self.redis.delete(self.keys.resume_code)
        async with self.session_factory() as db:
            row = await db.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
            if row is None:
                return CommandReply("The control settings are missing; resume from the Control Panel.", "REFUSED", True)
            row.max_daily_exposure = float(snapshot["max_daily_exposure"])
            if snapshot.get("bots_enabled") is not None:
                row.bots_enabled = bool(snapshot["bots_enabled"])
            await db.commit()
            await db.refresh(row)
        await set_kill_switch(self.redis, self.settings, engaged=False)
        await publish_risk_limits(self.redis, row, self.settings)
        await self.redis.delete(self.keys.halt_snapshot)
        by = f"telegram:{update.sender or update.chat_id}"
        await emit_alert(
            self.redis,
            self.settings,
            SentinelAlert(
                kind=AlertKind.KILL_SWITCH_LIFTED,
                severity=Severity.WARNING,
                title=f"Kill switch lifted from Telegram by {update.sender or 'an admin'}",
                body=f"Daily exposure back to {_inr(row.max_daily_exposure)}; bots {'enabled' if row.bots_enabled else 'disabled'}.",
                source="sentinel.telegram",
                dedupe_key="kill-switch",
                resolves=True,
                detail={"by": by, "restored": snapshot},
            ),
        )
        return CommandReply(f"▶️ <b>Trading resumed.</b> Daily exposure {_inr(row.max_daily_exposure)}, bots {'enabled' if row.bots_enabled else 'disabled'}.", "RESUMED", True, {"by": by})

    # -------------------------------------------------------------- /status
    async def status(self) -> CommandReply:
        now = self.clock()
        async with self.session_factory() as db:
            pnl = Decimal(str(await db.scalar(select(func.coalesce(func.sum(AuditLog.pnl_inr), 0)).where(AuditLog.event == AuditEvent.SETTLED, AuditLog.created_at >= now - timedelta(hours=24))) or 0))
            available, exposure = (await db.execute(select(func.coalesce(func.sum(BankrollAccount.available_balance), 0), func.coalesce(func.sum(BankrollAccount.exposure_balance), 0)))).one()
            open_bets = int(await db.scalar(select(func.count()).select_from(PhantomLedger).where(PhantomLedger.status.in_(OPEN_STATUSES))) or 0)
            bots = dict((await db.execute(select(TradingBot.status, func.count()).group_by(TradingBot.status))).all())
            controls = await db.get(SystemSettingsModel, SETTINGS_SINGLETON_ID)
        switch = await kill_switch_engaged(self.redis, self.settings)
        halted = controls is not None and controls.max_daily_exposure <= 0
        lines = [
            "📊 <b>BetDoc status</b>",
            f"P&amp;L (24h, realised): <b>{_inr(pnl)}</b>",
            f"Bankroll: {_inr(available)} available · {_inr(exposure)} at risk · {open_bets} open bet(s)",
            "Bots: " + (", ".join(f"{n} {str(s).lower()}" for s, n in sorted(bots.items(), key=lambda kv: str(kv[0]))) or "none"),
            f"Kill switch: {'🛑 ENGAGED' if switch or halted else '✅ off'}" + (f" (stop since {_aware(controls.last_emergency_stop_at):%d %b %H:%M} UTC)" if halted and controls and controls.last_emergency_stop_at else ""),
        ]
        lines += await self._live_lines()
        return CommandReply("\n".join(lines), "STATUS", True, {"pnl_24h": str(pnl), "open_bets": open_bets})

    async def _live_lines(self) -> list[str]:
        if self.redis is None:
            return ["Redis: unreachable (feeds and Hive state unknown)"]
        from app.services.hive_engine import HiveUnavailable, read_halt  # noqa: PLC0415 - the Hive engine imports the CFO stack

        lines = []
        try:
            flag = await read_halt(self.redis, self.settings)
            lines.append(f"Hive: {'⏸️ halted (' + html.escape(str(flag.get('reason'))) + ')' if flag else '▶️ running'}")
        except HiveUnavailable:
            lines.append("Hive: unknown")
        try:
            raw = await self.redis.get(self.keys.heartbeat_last(GARUDA))
            beat = json.loads(raw) if raw else None
            if beat is None:
                lines.append("Garuda (odds feed): ❌ no heartbeat yet")
            else:
                age = self.clock().timestamp() - float(beat["at"])
                ok = age < self.settings.SENTINEL_LIVENESS_TIMEOUT_SECONDS
                lines.append(f"Garuda (odds feed): {'💓' if ok else '❌'} last beat {age:.0f}s ago ({html.escape(str(beat.get('runner')))})")
            checks = await read_health(self.redis, self.settings)
            down = [c["name"] for c in checks if not c.get("ok")]
            lines.append("Dependencies: " + ("✅ all up" if checks and not down else f"❌ down: {html.escape(', '.join(down))}" if down else "not checked yet"))
        except (RedisError, OSError, ValueError, KeyError, TypeError):
            lines.append("Feeds: unknown (Redis error)")
        return lines

    async def _log(self, update: TelegramUpdate, command: str, reply: CommandReply) -> None:
        try:
            async with self.session_factory() as db:
                db.add(
                    SentinelCommandLog(
                        update_id=update.update_id, chat_id=update.chat_id, sender=update.sender, command=command[:32],
                        authorised=reply.authorised, outcome=reply.outcome, detail=reply.detail, received_at=self.clock(),
                    )
                )
                await db.commit()
        except Exception:  # noqa: BLE001 - the command already ran; a missing log line must not fail the reply
            logger.exception("Sentinel: the Telegram command log could not be written")


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def reply_payload(chat_id: int, text: str) -> dict[str, Any]:
    """Answer Telegram's webhook request with the ``sendMessage`` call itself (no token needed)."""
    return {"method": "sendMessage", "chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": True}


def known_commands() -> Sequence[str]:
    return COMMANDS
