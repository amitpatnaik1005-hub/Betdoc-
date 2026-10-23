"""Group 106: the Bet-Slip ornament (ASHVATTHAMA, point b.7) and its six delivery channels (point b.12). Developed for
Amit Ashok Kumar Patnaik.

Pure: a ticket is issued only for an APPROVED (gold grade) verdict whose seal holds, from the signed figures alone;
each leg carries the book's own code; the coupon, SMS line and deep links say the same thing; the HTML card escapes
everything; the Word package is a complete, well-formed WordprocessingML document; the Sentinel's new EMAIL channel
composes multipart mail, validates its settings, and maps SMTP failures to final or retried.

End to end, on SQLite and PostgreSQL with the live market in Redis: a gold verdict lists as a ticket and a
prescription-only verdict does not; the booking code is kept outside the seal (which still holds); the Word export
downloads; sending is an administrator's action, refused while a channel is not set up, and through Telegram and
Email (fake transports) it is delivered, logged as a BET_SLIP alert and kept on the ticket.
"""

from __future__ import annotations

import io
import json
import os
import re
import smtplib
import urllib.parse
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.adapters.notifications import email as email_adapter
from app.adapters.notifications.base import DispatcherConfigurationError, OutboundMessage
from app.adapters.notifications.email import EmailDispatcher, validate_address, validate_port
from app.api.deps import get_current_admin
from app.api.v1 import consultant as consultant_api
from app.api.v1 import slip_ornament as ornament_api
from app.core.config import Settings, get_settings
from app.core.security_vault import VaultCrypto
from app.domain.slip_ornament import ticket as tk
from app.domain.slip_ornament.docx import docx_bytes
from app.domain.slip_ornament.html_card import email_html
from app.models import User
from app.models.consultant import ConsultantVerdictModel
from app.models.real_money import RealBetTicket
from app.models.sentinel import ChannelName, SentinelAlertLog, SentinelChannel, SentinelDelivery, Severity
from app.models.slip_ornament import BetSlipTicketModel
from app.models.tax_and_fx import CurrencyExchangeRate
from app.services import slip_ornament as ornament_service
from tests.test_backtest_and_parlay import TABLES, _sqlite_metadata, app_for, make_user, redis, settings  # noqa: F401 - fixtures
from tests.test_group104_bet_builder_and_slip_score import _market

S = get_settings()
D = Decimal
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
EXTRA = [ConsultantVerdictModel.__table__, CurrencyExchangeRate.__table__, RealBetTicket.__table__, BetSlipTicketModel.__table__,
         SentinelChannel.__table__, SentinelAlertLog.__table__, SentinelDelivery.__table__]
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    tables = [*TABLES, *EXTRA]
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        md = _sqlite_metadata()
        for table in EXTRA:
            table.to_metadata(md)
        async with engine.begin() as conn:
            await conn.run_sync(md.create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=tables))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(tables))))
        await engine.dispose()


# ================================================================ a signed report, as the Consultant writes it
def _report(**over: Any) -> dict[str, Any]:
    legs = [
        {"leg_id": "fx-a|Match Odds|HOME", "fixture": "Arsenal vs Chelsea", "label": "1X2: Arsenal", "market": "Match Odds", "selection": "HOME",
         "sport_key": "soccer_epl", "league": "EPL", "kickoff": "2026-10-22T14:30:00+00:00", "price": 2.45, "fair_probability": 0.4339, "ev": 0.0631},
        {"leg_id": "fx-b|Totals 2.5|OVER", "fixture": "Mumbai Indians vs Chennai Super Kings", "label": "Totals: Over 2.5", "market": "Totals 2.5",
         "selection": "OVER", "sport_key": "cricket_ipl", "league": "IPL", "kickoff": "2026-10-22T14:00:00+00:00", "price": 1.95, "fair_probability": 0.55, "ev": 0.0725},
    ]
    report: dict[str, Any] = {
        "report_id": "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d", "signed_at": "2026-10-22T08:45:00+00:00", "signed_by": "ASHVATTHAMA · BetDoc Consultant",
        "verdict": {"status": "APPROVED_GOLD", "title": "APPROVED — GOLD GRADE", "summary": "Every enforced pillar passed."},
        "slip": {"slip_id": "abc", "kind": "DOUBLE", "title": "ASHOKA Double", "book": "1xbet", "odds": "4.7775", "joint_ev": 0.14, "joint_probability": 0.24, "legs": legs},
        "rating": {"score": 96.0, "tier": "DIAMOND"},
        "consultant": {"stake_inr": "700.00", "stake_usd": None, "fx": {"inr_per_usd": None}, "payout_inr": "3344.25",
                       "tax": {"net_winnings": 2644.25, "tds": 793.28, "net_post_tax_payout": 2550.97, "tds_rate_pct": 30.0},
                       "stop_loss": {"pct": 0.3536, "floor_inr": "452.48"}, "sizing_reason": "₹700 = 0.70% of bankroll", "regime": "OPTIMAL",
                       "drawdown": 0.0, "downside": {"p_total_loss": 0.76}},
        "doctor": {"pathologies": [], "unexamined": [{"number": 9, "title": "Referee profile", "reason": "no referee"}]},
        "lawyer": {"clauses": [{"code": "MAX_PAYOUT", "title": "Maximum payout per slip", "risk": "LOW", "finding": "No maximum payout is recorded.",
                                "hedge": "Split the stake.", "basis": "The maximum you recorded", "reference": None}]},
        "tipmaster": {"label": "No public share measured", "explanation": "Nothing is estimated."},
        "developer_credit": "Amit Ashok Kumar Patnaik",
    }
    report.update(over)
    return report


def _issue(report: dict[str, Any] | None = None, *, holds: bool = True, now: datetime = NOW, code: str | None = None) -> tk.Ticket:
    return tk.issue(report or _report(), "ab" * 32, seal_holds=holds, booking_code=code, now=now, timezone="Asia/Kolkata", fresh_minutes=30,
                    sports=S.MANUAL_PARLAY_SPORTS)


# ================================================================ the ticket (pure)
def test_a_ticket_is_issued_only_for_a_sealed_gold_approval_from_its_signed_figures() -> None:
    t = _issue()
    assert t.ticket_no == "BD-1A2B3C4D5E" and t.book_label == "1xBet" and t.badge == ("#007ACC", "#FFFFFF")
    assert [(leg.code, leg.sport, str(leg.price)) for leg in t.legs] == [("W1", "Football", "2.45"), ("Total Over (2.5)", "Cricket", "1.95")]
    assert t.legs[0].kickoff == "Thu 22 Oct 2026, 20:00 IST" and t.signed_at_local == "Thu 22 Oct 2026, 14:15 IST"  # the Oracle's time zone
    assert (t.stake_inr, t.payout, t.tds, t.take_home, t.stop_floor) == (D("700.00"), D("3344.25"), D("793.28"), D("2550.97"), D("452.48"))  # as signed
    assert t.stake_usd is None and t.fresh is True and t.age_minutes == 15 and t.watermark.endswith("Architect: Amit Ashok Kumar Patnaik")
    parimatch = _issue(_report(slip={**_report()["slip"], "book": "parimatch"}))
    assert parimatch.legs[0].code == "1" and parimatch.badge == ("#F8FF13", "#111111")  # Parimatch's own code and badge
    assert _issue(now=NOW + timedelta(hours=2)).fresh is False
    for status, reason in (("APPROVED_WITH_CURE", "NOT_APPROVED"), ("REJECTED", "NOT_APPROVED")):
        with pytest.raises(tk.NotIssuable) as refused:
            _issue(_report(verdict={"status": status, "title": "x", "summary": "y"}))
        assert refused.value.reason == reason
    with pytest.raises(tk.NotIssuable) as broken:
        _issue(holds=False)
    assert broken.value.reason == "SEAL_BROKEN"
    with pytest.raises(tk.NotIssuable) as unstaked:
        _issue(_report(consultant={**_report()["consultant"], "stake_inr": "0.00"}))
    assert unstaked.value.reason == "NO_STAKE"


def test_the_text_channels_say_the_same_thing() -> None:
    t = _issue(code="1XB-77A")
    coupon = tk.coupon_text(t)
    for text in ("BETDOC BET-SLIP · BD-1A2B3C4D5E", "1xBet · Double @ 4.7775", "Slip Score 96/100 DIAMOND", "1) Arsenal vs Chelsea (EPL · Thu 22 Oct 2026, 20:00 IST)",
                 "1X2: Arsenal [W1] @ 2.45", "Stake ₹700", "TDS ₹793.28", "take home ₹2,550.97", "under ₹452.48", "Booking code: 1XB-77A", "Seal abababababababab…"):
        assert text in coupon, text
    assert "$" not in coupon  # no fresh USD rate was signed: no dollar figure
    assert "($" not in tk.coupon_text(t) and "($8.32)" in tk.coupon_text(_issue(_report(consultant={**_report()["consultant"], "stake_usd": "8.32"})))
    assert "re-check them at 1xBet before placing" in tk.coupon_text(_issue(now=NOW + timedelta(hours=2)))
    assert urllib.parse.unquote(tk.whatsapp_url(t).removeprefix("https://wa.me/?text=")) == coupon
    assert urllib.parse.parse_qs(tk.mailto_url(t).removeprefix("mailto:?"))["body"][0] == coupon
    sms = tk.sms_text(t)
    assert len(sms) <= tk.SMS_LIMIT and "Arsenal vs Chelsea W1 2.45" in sms and "floor ₹452.48" in sms
    many = _report(slip={**_report()["slip"], "legs": [{**_report()["slip"]["legs"][0], "leg_id": f"fx-{i}|Match Odds|HOME", "fixture": f"Club Number {i} vs Rivals Of {i}"}
                                                       for i in range(12)]})
    crowded = tk.sms_text(_issue(many))
    assert len(crowded) <= tk.SMS_LIMIT and re.search(r"\+\d+ more", crowded) and crowded.endswith(f"Seal {'ab' * 4}")


def test_the_html_card_escapes_everything() -> None:
    hostile = _report(slip={**_report()["slip"], "legs": [{**_report()["slip"]["legs"][0], "fixture": '<script>alert("x")</script> vs FC'}]})
    card = email_html(_issue(hostile, code="AB-1"))
    assert "<script>" not in card and "&lt;script&gt;" in card and "BD-1A2B3C4D5E" in card and "#007ACC" in card and "AB-1" in card
    assert "Handcrafted by BetDoc Core Engine · Architect: Amit Ashok Kumar Patnaik" in card and "Nothing was placed by BetDoc" in card


def _parts(blob: bytes) -> dict[str, ET.Element]:
    with zipfile.ZipFile(io.BytesIO(blob)) as package:
        assert package.testzip() is None and package.namelist()[0] == "[Content_Types].xml"
        return {name: ET.fromstring(package.read(name)) for name in package.namelist()}


def test_the_word_ticket_is_a_complete_wordprocessingml_package() -> None:
    report = _report(lawyer={"clauses": [{**_report()["lawyer"]["clauses"][0], "finding": "Bad \x07char & <tag>"}]})
    blob = docx_bytes(_issue(report, code="PM-9921"), report)
    parts = _parts(blob)
    assert set(parts) == {"[Content_Types].xml", "_rels/.rels", "docProps/core.xml", "docProps/app.xml", "word/_rels/document.xml.rels", "word/styles.xml",
                          "word/footer1.xml", "word/document.xml"}
    overrides = {o.get("PartName") for o in parts["[Content_Types].xml"]}
    assert {f"/{name}" for name in parts if name.endswith(".xml") and name != "[Content_Types].xml"} <= overrides  # every part has its content type
    for rels, base in (("_rels/.rels", ""), ("word/_rels/document.xml.rels", "word/")):
        assert all(f"{base}{r.get('Target')}" in parts for r in parts[rels])  # every relationship points at a part that exists
    document = parts["word/document.xml"]
    body = document.find(f"{W}body")
    assert body is not None and body[-1].tag == f"{W}sectPr"
    assert body[-1][0].tag == f"{W}footerReference"  # the watermark sits in the page footer, on every page
    footer = "".join(t.text or "" for t in parts["word/footer1.xml"].iter(f"{W}t"))
    assert footer == "Handcrafted by BetDoc Core Engine · Architect: Amit Ashok Kumar Patnaik · BD-1A2B3C4D5E"
    for table in body.iter(f"{W}tbl"):
        columns = len(table.find(f"{W}tblGrid"))  # type: ignore[arg-type]
        for row in table.iter(f"{W}tr"):
            cells = row.findall(f"{W}tc")
            assert len(cells) == columns and all(cell.find(f"{W}p") is not None for cell in cells)  # Word refuses a cell with no paragraph
    styles = {s.get(f"{W}styleId") for s in parts["word/styles.xml"].iter(f"{W}style")}
    used = {p.get(f"{W}val") for p in document.iter(f"{W}pStyle")} | {t.get(f"{W}val") for t in document.iter(f"{W}tblStyle")}
    assert used <= styles
    text = "".join(t.text or "" for t in document.iter(f"{W}t"))
    for expected in ("BETDOC BET-SLIP", "BD-1A2B3C4D5E", "1X2: Arsenal", "W1", "₹700", "PM-9921", "ab" * 32, "Bad char & <tag>", "Referee profile",
                     "No fresh USD rate at signing", "/api/v1/consultant/verdicts/1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"):
        assert expected in text, expected
    assert docx_bytes(_issue(report, code="PM-9921"), report) == blob  # the same verdict gives the same bytes


# ================================================================ the Sentinel's EMAIL channel (pure)
class FakeSMTP:
    sent: list[Any] = []
    fail: list[BaseException] = []

    def __init__(self, host: str, port: int, timeout: float = 0, **_: Any) -> None:
        self.host, self.port, self.tls, self.user = host, port, False, None

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def starttls(self, context: Any = None) -> None:  # noqa: ARG002
        self.tls = True

    def login(self, user: str, password: str) -> None:
        assert self.tls or isinstance(self, FakeSSL), "never log in before TLS"
        if FakeSMTP.fail:
            raise FakeSMTP.fail.pop(0)
        self.user = (user, password)

    def send_message(self, mail: Any) -> None:
        FakeSMTP.sent.append(mail)


class FakeSSL(FakeSMTP):
    pass


async def _no_sleep(_: float) -> None:
    return None


def _mailer(**over: Any) -> EmailDispatcher:
    args: dict[str, Any] = {"host": "smtp.example.com", "port": "587", "username": "", "password": "pw-secret", "from_address": "desk@example.com",
                            "to_addresses": ["owner@example.com", "partner@example.org"], "sleep": _no_sleep}
    return EmailDispatcher(None, **{**args, **over})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_email_channel_sends_multipart_over_tls_and_maps_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_adapter.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_adapter.smtplib, "SMTP_SSL", FakeSSL)
    FakeSMTP.sent, FakeSMTP.fail = [], []
    message = OutboundMessage(Severity.INFO, "BET_SLIP", "BetDoc Bet-Slip BD-1", "plain body", NOW, "slip_ornament", detail={"html": "<b>card</b>"})
    result = await _mailer().send(message)
    assert result.ok and result.deliveries == 2 and [m["To"] for m in FakeSMTP.sent] == ["owner@example.com", "partner@example.org"]
    parts = [p.get_content_type() for p in FakeSMTP.sent[0].iter_parts()]
    assert parts == ["text/plain", "text/html"] and FakeSMTP.sent[0]["From"] == "desk@example.com"
    FakeSMTP.sent, FakeSMTP.fail = [], [smtplib.SMTPAuthenticationError(535, b"pw-secret rejected")]
    refused = await _mailer().send(message)
    assert not refused.ok and refused.attempts == 1 and "pw-secret" not in (refused.error or "")  # final: no retry, the password never shows
    FakeSMTP.sent, FakeSMTP.fail = [], [smtplib.SMTPServerDisconnected("gone")]
    retried = await _mailer(use_ssl=True, port="465").send(message)
    assert retried.ok and retried.attempts == 2  # transient: tried again
    assert validate_port("465") == 465 and validate_address(" me@example.com ") == "me@example.com"
    for bad in (lambda: validate_port("smtp"), lambda: validate_port(70000), lambda: validate_address("not-an-address"), lambda: _mailer(host=""),
                lambda: _mailer(to_addresses=["x@y"])):
        with pytest.raises(DispatcherConfigurationError):
            bad()


# ================================================================ end to end
def _app(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User, vault: VaultCrypto | None = None) -> FastAPI:
    app = app_for(sessions, redis, settings, user)
    app.dependency_overrides.pop(get_current_admin, None)  # the real role check, over the signed-in user
    app.include_router(consultant_api.router, prefix="/api/v1")
    app.include_router(ornament_api.router, prefix="/api/v1")
    app.state.vault = vault
    return app


GOOD = {"leg_ids": ["fx-fair|Match Odds|HOME", "fx-good|Match Odds|HOME"], "kind": "DOUBLE", "skin": "one_xbet", "bankroll_inr": "100000"}
BAD = {**GOOD, "leg_ids": ["fx-fair|Match Odds|HOME", "fx-bad|Match Odds|HOME"]}


async def _count(sessions: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with sessions() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


@pytest.mark.asyncio
async def test_a_gold_verdict_becomes_a_ticket_with_its_booking_code_and_word_export(sessions, redis: Redis, settings: Settings) -> None:  # noqa: F811
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, user)), base_url="http://test") as client:
        await _market(redis, settings, client)
        gold = (await client.post("/api/v1/consultant/audit", json=GOOD)).json()
        cure = (await client.post("/api/v1/consultant/audit", json=BAD)).json()
        assert gold["verdict"]["status"] == "APPROVED_GOLD" and cure["verdict"]["status"] != "APPROVED_GOLD"
        listed = (await client.get("/api/v1/slip-ornament/tickets")).json()["tickets"]
        assert [t["report_id"] for t in listed] == [gold["report_id"]] and listed[0]["issuable"] is True  # only the approval is a ticket
        ticket = (await client.get(f"/api/v1/slip-ornament/{gold['report_id']}")).json()
        assert ticket["book_label"] == "1xBet" and [leg["code"] for leg in ticket["legs"]] == ["W1", "W1"] and ticket["stake_inr"] == gold["consultant"]["stake_inr"]
        assert ticket["seal"] == gold["seal"] and ticket["coupon_text"].startswith(f"BETDOC BET-SLIP · {ticket['ticket_no']}") and ticket["deliveries"] == []
        refused = await client.get(f"/api/v1/slip-ornament/{cure['report_id']}")
        assert refused.status_code == 409 and refused.json()["detail"]["reason"] == "NOT_APPROVED"

        assert (await client.put(f"/api/v1/slip-ornament/{gold['report_id']}/booking-code", json={"booking_code": "bad code!"})).status_code == 422
        coded = (await client.put(f"/api/v1/slip-ornament/{gold['report_id']}/booking-code", json={"booking_code": "1XB-77A9"})).json()
        assert coded["booking_code"] == "1XB-77A9" and "Booking code: 1XB-77A9" in coded["coupon_text"] and coded["booking_recorded_at"]
        verdict = (await client.get(f"/api/v1/consultant/verdicts/{gold['report_id']}")).json()
        assert verdict["seal_holds"] is True  # the booking code lives outside the sealed report
        word = await client.get(f"/api/v1/slip-ornament/{gold['report_id']}/export-docx")
        assert word.status_code == 200 and word.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        assert f'filename="betdoc-bet-slip-{ticket["ticket_no"]}.docx"' in word.headers["content-disposition"]
        assert "1XB-77A9" in "".join(t.text or "" for t in _parts(word.content)["word/document.xml"].iter(f"{W}t"))
        assert await _count(sessions, BetSlipTicketModel) == 1
        cleared = (await client.put(f"/api/v1/slip-ornament/{gold['report_id']}/booking-code", json={"booking_code": None})).json()
        assert cleared["booking_code"] is None and cleared["booking_recorded_at"] is None

    stranger = await make_user(sessions, role="QUANT")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, stranger)), base_url="http://test") as client:
        assert (await client.get(f"/api/v1/slip-ornament/{gold['report_id']}")).status_code == 404  # nobody else's ticket
        assert (await client.post(f"/api/v1/slip-ornament/{gold['report_id']}/send-telegram")).status_code == 403  # sending is an administrator's action
        assert (await client.get("/api/v1/slip-ornament/channels")).json()["admin"] is False


async def _store_channel(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto, channel: ChannelName, secrets: dict[str, str], config: dict[str, Any]) -> None:
    async with sessions() as session:
        session.add(SentinelChannel(channel=channel.value, enabled=True, encrypted_credentials=vault.encrypt_key(json.dumps(secrets)), credentials_hint="test",
                                    config=config))
        await session.commit()


@pytest.mark.asyncio
async def test_sending_is_refused_until_a_channel_is_ready_then_delivered_and_logged(sessions, redis: Redis, settings: Settings,  # noqa: F811
                                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    vault = VaultCrypto(Fernet.generate_key().decode())
    admin = await make_user(sessions)
    calls: list[httpx.Request] = []

    def telegram(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(email_adapter.smtplib, "SMTP", FakeSMTP)
    FakeSMTP.sent, FakeSMTP.fail = [], []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, admin, vault)), base_url="http://test") as client:
        await _market(redis, settings, client)
        gold = (await client.post("/api/v1/consultant/audit", json=GOOD)).json()
        # the Bot API behind a fake transport: only clients built from here on (the service's own) use it
        monkeypatch.setattr(ornament_service.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(telegram), **kw))
        url = f"/api/v1/slip-ornament/{gold['report_id']}"
        channels = (await client.get("/api/v1/slip-ornament/channels")).json()
        assert channels["admin"] is True and not any(c["ready"] for c in channels["channels"].values())
        not_ready = await client.post(f"{url}/send-telegram")
        assert not_ready.status_code == 409 and not_ready.json()["detail"]["reason"] == "CHANNEL_NOT_READY"  # never reported as sent
        assert await _count(sessions, SentinelAlertLog) == 0

        await _store_channel(sessions, vault, ChannelName.TELEGRAM, {"bot_token": "123456:SECRET-token"}, {"chat_ids": [4242]})
        sent = (await client.post(f"{url}/send-telegram")).json()
        assert sent["ok"] is True and sent["deliveries"] == 1 and len(calls) == 1
        payload = json.loads(calls[0].content)
        assert payload["chat_id"] == 4242 and "BETDOC BET-SLIP" in payload["text"] and "W1" in payload["text"]

        await _store_channel(sessions, vault, ChannelName.EMAIL, {"password": "smtp-pw"},
                             {"host": "smtp.example.com", "port": "587", "from_address": "desk@example.com", "to_addresses": ["owner@example.com"]})
        mailed = (await client.post(f"{url}/send-email")).json()
        assert mailed["ok"] is True and mailed["deliveries"] == 1
        html = next(p for p in FakeSMTP.sent[0].iter_parts() if p.get_content_type() == "text/html").get_content()
        assert gold["seal"] in html and "1xBet".upper() in html

        async with sessions() as session:
            alerts = (await session.execute(select(SentinelAlertLog))).scalars().all()
            deliveries = (await session.execute(select(SentinelDelivery))).scalars().all()
        assert {a.kind for a in alerts} == {"BET_SLIP"} and sorted(d.channel for d in deliveries) == ["EMAIL", "TELEGRAM"] and {d.status for d in deliveries} == {"SENT"}
        assert all("html" not in (a.detail or {}) for a in alerts)  # the card is sent, not stored in the trail
        logged = (await client.get(url)).json()["deliveries"]
        assert [d["channel"] for d in logged] == ["EMAIL", "TELEGRAM"] and all(d["ok"] for d in logged)  # newest first


# ================================================================ routes and the front end
def test_the_routes_are_signed_in_and_sending_is_admin_only() -> None:
    from app.api.v1.router import api_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    for path in ("/api/v1/slip-ornament/tickets", "/api/v1/slip-ornament/channels", "/api/v1/slip-ornament/{report_id}", "/api/v1/slip-ornament/{report_id}/export-docx"):
        assert paths[path]["get"].get("security"), path
    for via in ("telegram", "sms", "email"):
        assert paths[f"/api/v1/slip-ornament/{{report_id}}/send-{via}"]["post"].get("security")
    assert ChannelName.EMAIL.value in [c.value for c in ChannelName]


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
def test_the_bet_slip_page_is_wired_and_reads_the_signed_ticket() -> None:
    app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
    assert re.search(r"to: ['\"]/bet-slip['\"].*bot: ['\"]ASHVATTHAMA['\"]", app) and 'path="/bet-slip"' in app
    config = (FRONTEND / "config" / "commanders.config.ts").read_text(encoding="utf-8")
    assert re.search(r'routePrefixes: \["/consultant", "/bet-slip"\]', config)
    page = (FRONTEND / "pages" / "TheBetSlip.tsx").read_text(encoding="utf-8")
    assert "/slip-ornament/" in (FRONTEND / "lib" / "slip_ornament.ts").read_text(encoding="utf-8")
    if "<CommanderHero" in page:  # the page opens with its own one-line header since Group 111
        assert page.index("<CommanderHero") < page.index("<ComponentGuideBanner")
    sources = [page, (FRONTEND / "lib" / "slip_ornament.ts").read_text(encoding="utf-8"),
               *(p.read_text(encoding="utf-8") for p in (FRONTEND / "components" / "slip_ornament").glob("*.tsx"))]
    for source in sources:
        assert "lucide-react" not in source and "Arsenal ML" not in source and "India v Australia" not in source and not re.search(r"/\s*84(\.0+)?\b", source)
    assert "@media print" in (FRONTEND / "index.css").read_text(encoding="utf-8")  # channel 2: the print sheet holds the ticket alone
    assert '"EMAIL"' in (FRONTEND / "lib" / "sentinel.ts").read_text(encoding="utf-8")
