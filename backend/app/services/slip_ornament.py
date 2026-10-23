"""The Bet-Slip ornament (Group 106, ASHVATTHAMA, points b.7 and b.12): tickets of approved, sealed verdicts and their
six delivery channels. Developed for Amit Ashok Kumar Patnaik.

* ``tickets``: the user's verdicts the Consultant APPROVED (gold grade), newest first, each seal re-checked.
* ``view``: one ticket (``app.domain.slip_ornament.ticket``) with its text channels: the coupon (1-click copy), the
  WhatsApp, SMS and mail deep links. Print / PDF and the PNG poster are drawn by the browser from the same figures.
* ``record_booking``: the bookmaker's booking code once the user placed the slip (kept outside the seal).
* ``issued`` / ``mark_booked``: the verdict, its record and its ticket, and the booking code, for the real-money desk's
  handcrafted placement (Group 114), which also puts the placed ticket on the view (``placement``).
* ``export_docx``: the editable Word ticket with the Consultant's diagnostic report (``docx_bytes``).
* ``send``: the ticket through one of the Sentinel's configured channels (Telegram, Twilio SMS, Email), to the
  administrators' own chats, phones or inboxes: an administrator's action, logged as a BET_SLIP alert with its
  delivery, and in the ticket's own delivery log. A channel that is not set up is refused, never reported as sent.

Nothing here places a bet: every ticket is placed by hand at the book.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.notifications.base import DispatcherConfigurationError, OutboundMessage
from app.adapters.notifications.registry import build
from app.core.config import Settings
from app.core.security_vault import VaultCrypto
from app.domain.consultant import diagnostics as dx
from app.domain.slip_ornament import ticket as tk
from app.domain.slip_ornament.docx import docx_bytes
from app.domain.slip_ornament.html_card import email_html
from app.models.consultant import ConsultantVerdictModel, VerdictStatus
from app.models.sentinel import ChannelName, SentinelAlertLog, SentinelChannel, SentinelDelivery, Severity
from app.models.slip_ornament import BetSlipTicketModel
from app.services.sentinel_bus import AlertKind

CHANNELS: dict[str, ChannelName] = {"telegram": ChannelName.TELEGRAM, "sms": ChannelName.TWILIO, "email": ChannelName.EMAIL}
LABELS: dict[ChannelName, str] = {ChannelName.TELEGRAM: "Telegram", ChannelName.TWILIO: "SMS", ChannelName.EMAIL: "Email"}


class OrnamentRefusal(Exception):
    def __init__(self, reason: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.reason, self.message, self.status_code = reason, message, status_code


def _secret(settings: Settings) -> str:
    return settings.SECRET_KEY.get_secret_value()


async def _verdict(session: AsyncSession, user_id: uuid.UUID, report_id: uuid.UUID) -> ConsultantVerdictModel:
    row = await session.get(ConsultantVerdictModel, report_id)
    if row is None or row.user_id != user_id:
        raise OrnamentRefusal("VERDICT_NOT_FOUND", "no such verdict of yours", 404)
    return row


async def _record(session: AsyncSession, verdict_id: uuid.UUID) -> BetSlipTicketModel | None:
    return (await session.execute(select(BetSlipTicketModel).where(BetSlipTicketModel.verdict_id == verdict_id))).scalar_one_or_none()


def _issue(row: ConsultantVerdictModel, record: BetSlipTicketModel | None, settings: Settings, now: datetime) -> tk.Ticket:
    try:
        return tk.issue(row.report, row.seal, seal_holds=dx.seal_holds(row.report, _secret(settings), row.seal), booking_code=None if record is None else record.booking_code,
                        now=now, timezone=settings.ORACLE_TIMEZONE, fresh_minutes=settings.BET_SLIP_FRESH_MINUTES, sports=settings.MANUAL_PARLAY_SPORTS)
    except tk.NotIssuable as exc:
        raise OrnamentRefusal(exc.reason, exc.message) from exc


def _deliveries(record: BetSlipTicketModel | None) -> list[dict[str, Any]]:
    return list(reversed((record.deliveries or []) if record is not None else []))


def _view(ticket: tk.Ticket, record: BetSlipTicketModel | None, placement: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**ticket.as_dict(), "coupon_text": tk.coupon_text(ticket), "sms_text": tk.sms_text(ticket), "whatsapp_url": tk.whatsapp_url(ticket),
            "mailto_url": tk.mailto_url(ticket), "sms_url": tk.sms_url(ticket), "deliveries": _deliveries(record),
            "booking_recorded_at": None if record is None or record.booking_recorded_at is None else record.booking_recorded_at.isoformat(),
            "placement": placement}


async def _placement(session: AsyncSession, settings: Settings, verdict_id: uuid.UUID) -> dict[str, Any] | None:
    from app.services.handcrafted_execution_service import placement_view  # noqa: PLC0415 - the real-money desk imports this module

    return await placement_view(session, settings, verdict_id)


async def tickets(session: AsyncSession, settings: Settings, user_id: uuid.UUID, limit: int, now: datetime) -> list[dict[str, Any]]:
    """The user's approved verdicts, newest first: what a ticket can be issued for (a broken seal says so)."""
    rows = (await session.execute(select(ConsultantVerdictModel).where(ConsultantVerdictModel.user_id == user_id,
                                                                        ConsultantVerdictModel.verdict == VerdictStatus.APPROVED_GOLD.value)
                                  .order_by(ConsultantVerdictModel.signed_at.desc()).limit(limit))).scalars().all()
    records = {} if not rows else {r.verdict_id: r for r in (await session.execute(select(BetSlipTicketModel).where(
        BetSlipTicketModel.verdict_id.in_([row.id for row in rows])))).scalars()}
    out = []
    for row in rows:
        record = records.get(row.id)
        try:
            ticket = _issue(row, record, settings, now)
        except OrnamentRefusal as exc:
            out.append({"report_id": str(row.id), "issuable": False, "reason": exc.reason, "message": exc.message, "slip_score": row.slip_score, "tier": row.tier})
            continue
        out.append({"report_id": ticket.report_id, "issuable": True, "ticket_no": ticket.ticket_no, "signed_at": ticket.signed_at.isoformat(),
                    "signed_at_local": ticket.signed_at_local, "fresh": ticket.fresh, "book_label": ticket.book_label, "legs": [leg.market for leg in ticket.legs],
                    "odds": None if ticket.odds is None else str(ticket.odds), "stake_inr": str(ticket.stake_inr), "slip_score": ticket.slip_score, "tier": ticket.tier,
                    "booking_code": ticket.booking_code})
    return out


async def view(session: AsyncSession, settings: Settings, user_id: uuid.UUID, report_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    row = await _verdict(session, user_id, report_id)
    record = await _record(session, row.id)
    return _view(_issue(row, record, settings, now), record, await _placement(session, settings, row.id))


async def issued(session: AsyncSession, settings: Settings, user_id: uuid.UUID, report_id: uuid.UUID,
                 now: datetime) -> tuple[ConsultantVerdictModel, BetSlipTicketModel | None, tk.Ticket]:
    """The user's verdict, its record and its ticket, the seal re-checked (OrnamentRefusal: not found, broken seal,
    not an approval, no stake)."""
    row = await _verdict(session, user_id, report_id)
    record = await _record(session, row.id)
    return row, record, _issue(row, record, settings, now)


async def mark_booked(session: AsyncSession, row: ConsultantVerdictModel, code: str, now: datetime) -> BetSlipTicketModel:
    """The booking code on the ticket's record (the caller commits)."""
    record = await _record_for(session, row, now)
    record.booking_code, record.booking_recorded_at = code, now
    return record


async def _record_for(session: AsyncSession, row: ConsultantVerdictModel, now: datetime) -> BetSlipTicketModel:
    record = await _record(session, row.id)
    if record is None:
        record = BetSlipTicketModel(id=uuid.uuid4(), verdict_id=row.id, user_id=row.user_id, deliveries=[], created_at=now)
        session.add(record)
    return record


async def record_booking(session: AsyncSession, settings: Settings, user_id: uuid.UUID, report_id: uuid.UUID, code: str | None, now: datetime) -> dict[str, Any]:
    """The bookmaker's booking code for the placed slip (None clears it). Only an issuable ticket takes one."""
    row = await _verdict(session, user_id, report_id)
    _issue(row, await _record(session, row.id), settings, now)
    record = await _record_for(session, row, now)
    record.booking_code, record.booking_recorded_at = code, (now if code else None)
    await session.commit()
    return _view(_issue(row, record, settings, now), record, await _placement(session, settings, row.id))


async def export_docx(session: AsyncSession, settings: Settings, user_id: uuid.UUID, report_id: uuid.UUID, now: datetime) -> tuple[str, bytes]:
    row = await _verdict(session, user_id, report_id)
    ticket = _issue(row, await _record(session, row.id), settings, now)
    return f"betdoc-bet-slip-{ticket.ticket_no}.docx", docx_bytes(ticket, row.report)


def _readiness(row: SentinelChannel | None, vault: VaultCrypto | None) -> tuple[bool, str | None]:
    if row is None:
        return False, "not set up: store its credentials in Control Panel, Sentinel"
    if not row.enabled:
        return False, "disabled in Control Panel, Sentinel"
    try:
        build(row, vault, None)  # type: ignore[arg-type] - constructs and validates only: nothing is sent
    except DispatcherConfigurationError as exc:
        return False, str(exc)
    return True, None


async def channels(session: AsyncSession, vault: VaultCrypto | None) -> dict[str, Any]:
    """Which Sentinel channels can carry a ticket now, and why not (never a secret)."""
    out = {}
    for key, channel in CHANNELS.items():
        ready, problem = _readiness(await session.get(SentinelChannel, channel.value), vault)
        out[key] = {"channel": channel.value, "label": LABELS[channel], "ready": ready, "problem": problem}
    return out


async def send(sessions: async_sessionmaker[AsyncSession], settings: Settings, vault: VaultCrypto | None, user_id: uuid.UUID, report_id: uuid.UUID, via: str,
               now: datetime) -> dict[str, Any]:
    """The ticket through one configured Sentinel channel, to the administrators' own chats, phones or inboxes."""
    channel = CHANNELS.get(via)
    if channel is None:
        raise OrnamentRefusal("UNKNOWN_CHANNEL", f"the channel is one of {', '.join(CHANNELS)}", 404)
    async with sessions() as session:
        row = await _verdict(session, user_id, report_id)
        ticket = _issue(row, await _record(session, row.id), settings, now)
        channel_row = await session.get(SentinelChannel, channel.value)
        ready, problem = _readiness(channel_row, vault)
        if not ready or channel_row is None:
            raise OrnamentRefusal("CHANNEL_NOT_READY", f"{LABELS[channel]} is {problem}")
        alert_id = uuid.uuid4()
        title = f"BetDoc Bet-Slip {ticket.ticket_no} · {ticket.book_label}"
        body = tk.sms_text(ticket) if channel is ChannelName.TWILIO else tk.coupon_text(ticket)
        detail: dict[str, Any] = {"report_id": ticket.report_id, "ticket_no": ticket.ticket_no, "channel": channel.value}
        message = OutboundMessage(Severity.INFO, AlertKind.BET_SLIP, title, body, now, "slip_ornament", dedupe_key=f"bet-slip:{ticket.report_id}", alert_id=alert_id,
                                  detail={**detail, "html": email_html(ticket)} if channel is ChannelName.EMAIL else detail)
        async with httpx.AsyncClient(timeout=settings.SENTINEL_HTTP_TIMEOUT_SECONDS) as http:
            result = await build(channel_row, vault, http).send(message)
        if result.ok:
            channel_row.last_success_at = now
        else:
            channel_row.last_error, channel_row.last_error_at = (result.error or "failed")[:300], now
        session.add(SentinelAlertLog(id=alert_id, kind=AlertKind.BET_SLIP.value, severity=Severity.INFO.value, title=title[:200], body=body, source="slip_ornament",
                                     dedupe_key=f"bet-slip:{ticket.report_id}", detail=detail, occurred_at=now))
        session.add(SentinelDelivery(alert_id=alert_id, channel=channel.value, status="SENT" if result.ok else "FAILED", error=result.error,
                                     latency_ms=result.latency_ms, attempted_at=now))
        record = await _record_for(session, row, now)
        entry = {"channel": channel.value, "ok": result.ok, "deliveries": result.deliveries, "error": result.error, "at": now.isoformat()}
        record.deliveries = [*(record.deliveries or []), entry]  # a new list: the JSON column sees the change
        await session.commit()
    return {"channel": channel.value, "label": LABELS[channel], "ok": result.ok, "deliveries": result.deliveries, "error": result.error,
            "latency_ms": result.latency_ms, "status_code": result.status_code}
