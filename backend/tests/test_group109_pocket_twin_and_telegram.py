"""Group 109: the Pocket Twin (point b.10): the two-way Telegram desk, the phone alerts and the PWA. Developed for Amit
Ashok Kumar Patnaik.

Pure: a tapped inline button parses as the command it carries; a reply carries its keyboard; link codes are one-time;
buttons only ever open an https public host; the match card is the board's own figures.

End to end through the Sentinel's real webhook (secret header, the vault-held token, configured chats only), on SQLite
and PostgreSQL with the live market in Redis: /start shows the menu; account commands refuse an unlinked chat; a code
from the PWA links the chat once; /matches shows Parimatch and 1xBet from the board, and its button runs /score, which
fits the scorelines and acknowledges the tap; /vault reads the account's bankroll, regime and shields; /chef serves
the Chef's Table with a coupon and pages the phone row once for a DIAMOND plate; /consultant and /slip read the signed
verdict and its ticket, and a tampered report says its seal is broken; /ask answers through the Scout; a stranger's chat
hears nothing; /unlink undoes the link. The PWA ships its manifest, icons, service worker and the bottom thumb bar.
"""

from __future__ import annotations

import json
import os
import random
import re
import uuid
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
from sqlalchemy import CheckConstraint, MetaData, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.api.v1 import consultant as consultant_api
from app.api.v1 import mobile as mobile_api
from app.api.v1 import sentinel as sentinel_api
from app.core.config import Settings, get_settings
from app.core.security_vault import VaultCrypto
from app.models import User
from app.models.cfo_vault import BankrollAccount
from app.models.consultant import ConsultantVerdictModel
from app.models.omni_vault import VaultBookmakerAccount
from app.models.oracle_scout import OracleScoutHistoryModel
from app.models.real_money import RealBetTicket
from app.models.sentinel import ChannelName, SentinelAlertLog, SentinelChannel, SentinelCommandLog, SentinelDelivery, SentinelRouting, Severity
from app.models.slip_ornament import BetSlipTicketModel
from app.models.tax_and_fx import CurrencyExchangeRate
from app.models.the_wire import InjuryRosterReport
from app.services import chef_table, pocket_twin
from app.services.sentinel_bus import AlertKind, recent_alerts
from app.services.sentinel_commands import TelegramUpdate, reply_payload
from app.services.sentinel_routing import PHONE_KINDS, row_of
from tests.test_backtest_and_parlay import TABLES, app_for, make_user, redis  # noqa: F401 - fixtures
from tests.test_group104_bet_builder_and_slip_score import _market

S = get_settings()
ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
EXTRA = [ConsultantVerdictModel.__table__, CurrencyExchangeRate.__table__, RealBetTicket.__table__, BetSlipTicketModel.__table__, SentinelChannel.__table__,
         SentinelRouting.__table__, SentinelAlertLog.__table__, SentinelDelivery.__table__, SentinelCommandLog.__table__, InjuryRosterReport.__table__,
         OracleScoutHistoryModel.__table__]
ALL = list({t.name: t for t in [*TABLES, *EXTRA]}.values())
ADMIN_CHAT, ADMIN_USER, BOT_TOKEN, WEBHOOK_SECRET = 515_151, 77, "123456:AAtest-pocket-twin-token", "pocket-twin-webhook-secret"
DEVELOPER = "Amit Ashok Kumar Patnaik"


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"ARYABHATA_PREFIX": "test_arya", "TWIN_PREFIX": "test_twin_wb", "ASHOKA_MC_PATHS": 4_000, "ORACLE_TIMEZONE": "Asia/Kolkata",
                                             "SENTINEL_PREFIX": "test_sentinel_g109", "SENTINEL_STREAM": "test_sentinel_g109:alerts", "WIRE_PREFIX": "test_wire_g109",
                                             "PUBLIC_APP_URL": "https://desk.betdoc.example", "SCOUT_LLM_ENABLED": False})


@pytest.fixture
def vault() -> VaultCrypto:
    return VaultCrypto(Fernet.generate_key().decode())


def _metadata() -> MetaData:
    md = MetaData()
    for table in ALL:
        copy = table.to_metadata(md)
        for check in [c for c in copy.constraints if isinstance(c, CheckConstraint) and ("~" in str(c.sqltext) or "jsonb_typeof" in str(c.sqltext))]:
            copy.constraints.discard(check)
    return md


@pytest_asyncio.fixture(params=["sqlite", "postgres"])
async def sessions(request: pytest.FixtureRequest) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as conn:
            await conn.run_sync(_metadata().create_all)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()
        return
    if not TEST_POSTGRES_URL:
        pytest.skip("set TEST_POSTGRES_URL to a disposable PostgreSQL database")
    engine = create_async_engine(TEST_POSTGRES_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: User.metadata.create_all(sync, tables=ALL))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: User.metadata.drop_all(sync, tables=list(reversed(ALL))))
        await engine.dispose()


# ================================================================ pure
def test_a_tapped_button_is_the_command_it_carries_and_a_reply_carries_its_keyboard() -> None:
    tap = TelegramUpdate.parse({"update_id": 9, "callback_query": {"id": "cb-1", "data": "/score fx-1", "from": {"id": 77, "username": "amit"},
                                                                    "message": {"chat": {"id": 515}}}})
    assert tap is not None and tap.text == "/score fx-1" and tap.callback_id == "cb-1" and tap.chat_id == 515 and tap.sender == "@amit"
    assert tap.command == ("/score", ["fx-1"])
    plain = TelegramUpdate.parse({"update_id": 10, "message": {"text": "/vault", "chat": {"id": 515}, "from": {"id": 77}}})
    assert plain is not None and plain.callback_id is None
    keyboard = [[{"text": "Open", "url": "https://desk.example/vault"}]]
    assert reply_payload(515, "hi", keyboard)["reply_markup"] == {"inline_keyboard": keyboard} and "reply_markup" not in reply_payload(515, "hi")


def test_buttons_open_only_a_public_https_desk_and_sports_are_the_boards_own() -> None:
    for url, ok in (("https://desk.betdoc.example", True), ("http://desk.betdoc.example", False), ("https://localhost:5173", False),
                    ("https://127.0.0.1", False), (None, False), ("", False)):
        assert (pocket_twin.app_link(S.model_copy(update={"PUBLIC_APP_URL": url}), "/vault") is not None) is ok, url
    assert pocket_twin.app_link(S.model_copy(update={"PUBLIC_APP_URL": "https://desk.betdoc.example/"}), "/vault") == "https://desk.betdoc.example/vault"
    assert pocket_twin.sport_of("cricket", S) == "Cricket" and pocket_twin.sport_of("FOOTBALL", S) == "Football" and pocket_twin.sport_of("soccer", S) == "Football"
    assert pocket_twin.sport_of("quidditch", S) is None and pocket_twin.sport_of(None, S) is None


def test_the_match_card_is_the_boards_own_figures() -> None:
    card = {"fixture_id": "fx-1", "home": "Arsenal", "away": "Chelsea", "league_name": "Premier League", "kickoff": "2026-10-22T14:30:00+00:00", "in_play": False,
            "projection": {"score": "1-1", "probability": 0.142, "expected_goals": [1.41, 1.11]},
            "markets": [{"market": "Match Odds", "kind": "MATCH_ODDS", "selections": [
                {"leg_id": "fx-1|Match Odds|HOME", "label": "1X2: Arsenal", "fair_probability": 0.4275, "ev": 0.0475, "prices": {"parimatch": 2.4, "1xbet": 2.45}, "best": {"book": "1xbet", "odds": 2.45}},
                {"leg_id": "fx-1|Match Odds|DRAW", "label": "1X2: Draw", "fair_probability": 0.2874, "ev": -0.09, "prices": {"parimatch": 3.15}, "best": {"book": "parimatch", "odds": 3.15}},
                {"leg_id": "fx-1|Match Odds|AWAY", "label": "1X2: Chelsea", "fair_probability": 0.2851, "ev": -0.03, "prices": {"parimatch": 3.35, "1xbet": 3.4}, "best": {"book": "1xbet", "odds": 3.4}}]}]}
    text = pocket_twin.match_card(card, S)
    assert "<b>Arsenal v Chelsea</b>" in text and "Arsenal 2.40 / 2.45" in text and "Draw 3.15 / —" in text and "Chelsea 3.35 / 3.40" in text
    assert "BetDoc fair: Arsenal 42.8% · Draw 28.7% · Chelsea 28.5%" in text and "Projected: 1-1 (14.2%) · xG 1.41-1.11" in text
    assert "Best edge: 1X2: Arsenal @ 2.45 (1xBet), EV +4.8%" in text and "20:00 IST" in text
    card["markets"][0]["selections"][0]["ev"] = -0.01
    assert "No leg priced above BetDoc's fair chance" in pocket_twin.match_card(card, S)
    assert pocket_twin.esc("<b>&") == "&lt;b&gt;&amp;"


@pytest.mark.asyncio
async def test_a_link_code_is_one_time_and_expires(redis: Redis, settings: Settings) -> None:  # noqa: F811
    user_id = uuid.uuid4()
    code, seconds = await pocket_twin.issue_link_code(redis, settings, user_id)
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", code) and seconds == settings.POCKET_TWIN_LINK_CODE_SECONDS
    assert 0 < await redis.ttl(f"{settings.SENTINEL_PREFIX}:telegram:link:{code}") <= seconds
    assert await pocket_twin.redeem_link_code(redis, settings, "nope") is None
    assert await pocket_twin.redeem_link_code(redis, settings, code.lower()) == user_id
    assert await pocket_twin.redeem_link_code(redis, settings, code) is None  # used once
    assert pocket_twin.linked_user_id({"links": {"515": {"user_id": str(user_id)}}}, 515) == user_id and pocket_twin.linked_user_id({}, 515) is None


@pytest.mark.asyncio
async def test_a_diamond_plate_before_kickoff_pages_the_phone_row_once(redis: Redis, settings: Settings) -> None:  # noqa: F811
    now = datetime.now(UTC)

    def plate(slip: str, tier: str, kickoff: datetime) -> dict[str, Any]:
        return {"slip_id": slip, "title": "#1 Handcrafted parlay of the day", "kind": "DOUBLE", "odds": "4.2000", "book": "1xbet", "stake_inr": "500.00",
                "joint_ev": 0.12, "joint_probability": 0.27, "rating": {"score": 93.0 if tier == "DIAMOND" else 80.0, "tier": tier},
                "legs": [{"leg_id": f"{slip}|Match Odds|HOME", "fixture": "A v B", "label": "1X2: A", "kickoff": kickoff.isoformat()}]}

    plates = [plate("s-diamond", "DIAMOND", now + timedelta(hours=3)), plate("s-gold", "GOLD", now + timedelta(hours=3)),
              plate("s-started", "DIAMOND", now - timedelta(minutes=5))]
    assert await chef_table.page_diamonds(redis, settings, plates, now) == 1
    assert await chef_table.page_diamonds(redis, settings, plates, now) == 0  # once per slip
    assert await chef_table.page_diamonds(redis, settings.model_copy(update={"POCKET_TWIN_DIAMOND_ALERTS": False}), [plate("s-other", "DIAMOND", now + timedelta(hours=1))], now) == 0
    (paged,) = [a for a in await recent_alerts(redis, settings, 50) if a.kind is AlertKind.CHEF_DIAMOND]
    assert paged.severity is Severity.INFO and "Slip Score 93" in paged.title and "nothing is placed for you" in paged.body and paged.detail["slip_id"] == "s-diamond"
    assert AlertKind.CHEF_DIAMOND in PHONE_KINDS and AlertKind.TWIN_PULLOUT in PHONE_KINDS and row_of(paged) == "HYPE"  # Telegram and SMS by default


# ================================================================ end to end through the Sentinel's webhook
def _app(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user: User, vault: VaultCrypto) -> FastAPI:  # noqa: F811
    app = app_for(sessions, redis, settings, user)
    for module, prefix in ((sentinel_api, "/api/v1"), (consultant_api, "/api/v1"), (mobile_api, "/api/v1/mobile")):
        app.include_router(module.router, prefix=prefix)
    app.state.vault = vault
    return app


def _update(text: str, *, chat: int = ADMIN_CHAT, user: int = ADMIN_USER, tap: bool = False) -> dict[str, Any]:
    sender = {"id": user, "is_bot": False, "username": "amit_admin"}
    if tap:
        return {"update_id": random.randint(1, 10**9), "callback_query": {"id": f"cb-{random.randint(1, 10**6)}", "data": text, "from": sender,
                                                                           "message": {"message_id": 3, "chat": {"id": chat, "type": "private"}}}}
    return {"update_id": random.randint(1, 10**9), "message": {"message_id": 1, "date": 0, "text": text, "chat": {"id": chat, "type": "private"}, "from": sender}}


async def _say(client: httpx.AsyncClient, text: str, **kw: Any) -> dict[str, Any]:
    r = await client.post("/api/v1/sentinel/webhook/telegram", json=_update(text, **kw), headers={sentinel_api.TELEGRAM_SECRET_HEADER: WEBHOOK_SECRET})
    assert r.status_code == 200, r.text
    return r.json()


async def _telegram(sessions: async_sessionmaker[AsyncSession], vault: VaultCrypto) -> None:
    async with sessions() as session:
        session.add(SentinelChannel(channel=ChannelName.TELEGRAM.value, enabled=True, encrypted_credentials=vault.encrypt_key(json.dumps({"bot_token": BOT_TOKEN, "webhook_secret": WEBHOOK_SECRET})),
                                    config={"chat_ids": [ADMIN_CHAT], "admin_user_ids": [ADMIN_USER], "bot_username": "BetDocTwinBot"}))
        await session.commit()


@pytest.mark.asyncio
async def test_the_pocket_twin_answers_the_desk_from_telegram(sessions, redis: Redis, settings: Settings, vault: VaultCrypto, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    taps: list[tuple[str, str]] = []

    async def answered(http: httpx.AsyncClient, token: str, callback_id: str) -> bool:  # noqa: ARG001 - no network in tests
        taps.append((token, callback_id))
        return True

    monkeypatch.setattr(sentinel_api, "answer_callback", answered)
    user = await make_user(sessions)
    async with sessions() as session:
        session.add(BankrollAccount(user_id=user.id, bot_id=None, available_balance=Decimal("100000"), exposure_balance=Decimal("0")))
        await session.commit()
    await _telegram(sessions, vault)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, redis, settings, user, vault)), base_url="http://test") as client:
        await _market(redis, settings, client)

        start = await _say(client, "/start")
        assert "Pocket Twin" in start["text"] and start["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "/matches"
        assert "not linked" in (await _say(client, "/vault"))["text"]
        assert await _say(client, "/matches", chat=999_999) == {}  # a stranger's chat: silence

        # ---------------------------------------------------------- linking the chat from the PWA
        issued = (await client.post("/api/v1/mobile/telegram/link-code")).json()
        assert issued["command"] == f"/link {issued['code']}" and issued["deep_link"] == f"https://t.me/BetDocTwinBot?start={issued['code']}"
        assert "wrong, used or expired" in (await _say(client, "/link AAAAAAAA"))["text"]
        linked = await _say(client, f"/start {issued['code']}")  # through the deep link
        assert f"Linked to <b>{user.username}</b>" in linked["text"]
        assert "wrong, used or expired" in (await _say(client, f"/link {issued['code']}"))["text"]  # one-time
        status = (await client.get("/api/v1/mobile/status")).json()
        assert [c["chat_id"] for c in status["telegram"]["linked_chats"]] == [str(ADMIN_CHAT)] and status["telegram"]["ready"] is True
        assert "CHEF_DIAMOND" in status["phone_alerts"]["kinds"] and "TWIN_PULLOUT" in status["phone_alerts"]["kinds"] and status["app_link"] == "https://desk.betdoc.example/"
        assert "/matches" in status["telegram"]["commands"] and status["developer_credit"] == DEVELOPER

        # ---------------------------------------------------------- the board, a tapped button, the prediction
        board = await _say(client, "/matches football")
        assert "Match board · Football" in board["text"] and "<b>Arsenal v Chelsea</b>" in board["text"] and "(Parimatch / 1xBet)" in board["text"]
        assert "BetDoc fair:" in board["text"] and "Best edge" in board["text"]
        buttons = [b for row in board["reply_markup"]["inline_keyboard"] for b in row]
        assert {"text": "Open", "url": "https://desk.betdoc.example/oracle?tab=board&fixture=fx-good"} in buttons
        assert "No sport called quidditch" in (await _say(client, "/matches quidditch"))["text"]
        score = await _say(client, "/score fx-fair", tap=True)
        assert "<b>Arsenal v Chelsea</b>" in score["text"] and "Scorelines (Dixon-Coles, exact)" in score["text"] and "xG " in score["text"]
        assert taps and taps[-1][0] == BOT_TOKEN  # the tap was acknowledged with the vault's token
        assert "<b>Brighton v Wolves</b>" in (await _say(client, "/score wolves"))["text"]
        assert "Pick a match" in (await _say(client, "/score"))["text"] and "No fixture on the live board matches" in (await _say(client, "/score atlantis"))["text"]

        # ---------------------------------------------------------- the account
        vault_reply = (await _say(client, "/vault"))["text"]
        assert "Bankroll: <b>₹100,000</b>" in vault_reply and "regime optimal growth" in vault_reply and "Stop-loss shields: none active" in vault_reply
        assert "Last 24h settled: no bet" in vault_reply and "$" not in vault_reply  # no fresh USD rate: no dollar figure at all
        chef = await _say(client, "/chef 1500")
        assert "Slip Score <b>" in chef["text"] and "<pre>BETDOC CHEF'S TABLE · #1" in chef["text"] and "At your ₹1,500" in chef["text"] and "KUMBHA" in chef["text"]
        diamonds = [a for a in await recent_alerts(redis, settings, 200) if a.kind is AlertKind.CHEF_DIAMOND]
        table = await chef_table.chef_table(sessions, redis, settings, user.id, None, datetime.now(UTC))
        assert len(diamonds) == sum(1 for p in table["plates"] if p["rating"]["tier"] == "DIAMOND")  # once each, not again on the cached re-read
        assert "Not a sport or a stake" in (await _say(client, "/chef darts"))["text"]

        assert "No consultation yet" in (await _say(client, "/consultant"))["text"]
        assert "No Bet-Slip ticket" in (await _say(client, "/slip"))["text"]
        gold = (await client.post("/api/v1/consultant/audit", json={"leg_ids": ["fx-fair|Match Odds|HOME", "fx-good|Match Odds|HOME"], "kind": "DOUBLE",
                                                                      "skin": "one_xbet", "bankroll_inr": "100000"})).json()
        assert gold["verdict"]["status"] == "APPROVED_GOLD"
        consult = (await _say(client, "/consultant"))["text"]
        assert "APPROVED" in consult and "seal holds" in consult and "Slip Score <b>" in consult and "Doctor:" in consult
        slip = (await _say(client, "/slip"))["text"]
        assert "<pre>BETDOC BET-SLIP" in slip and "Tap the coupon to copy it" in slip
        async with sessions() as session:  # tamper with the signed report: the bot says so
            row = await session.get(ConsultantVerdictModel, uuid.UUID(gold["report_id"]))
            row.report = {**row.report, "verdict": {**row.report["verdict"], "summary": "edited"}}
            await session.commit()
        assert "seal broken" in (await _say(client, "/consultant"))["text"]

        asked = (await _say(client, "/ask what is my bankroll?"))["text"]
        assert "Scout Oracle" in asked and "Answered by" in asked
        async with sessions() as session:
            assert (await session.execute(select(OracleScoutHistoryModel.page_context))).scalars().all() == ["telegram"]

        assert "Unlinked" in (await _say(client, "/unlink"))["text"]
        assert "not linked" in (await _say(client, "/slip"))["text"]
        assert (await client.get("/api/v1/mobile/status")).json()["telegram"]["linked_chats"] == []
        assert (await client.delete(f"/api/v1/mobile/telegram/links/{ADMIN_CHAT}")).status_code == 404

    async with sessions() as session:
        log = list((await session.execute(select(SentinelCommandLog).order_by(SentinelCommandLog.id))).scalars())
    assert {r.outcome for r in log} >= {"HELP", "NOT_LINKED", "IGNORED", "LINKED", "REFUSED", "MATCHES", "SCORE", "VAULT", "CHEF", "CONSULTANT", "SLIP", "ASKED", "UNLINKED"}
    assert all(BOT_TOKEN not in json.dumps(r.detail or {}) for r in log)


@pytest.mark.asyncio
async def test_without_redis_link_codes_are_refused(sessions, settings: Settings, vault: VaultCrypto) -> None:
    user = await make_user(sessions)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(sessions, None, settings, user, vault)), base_url="http://test") as client:  # type: ignore[arg-type]
        refused = await client.post("/api/v1/mobile/telegram/link-code")
        assert refused.status_code == 503 and refused.json()["detail"]["reason"] == "NO_REDIS"
        status = (await client.get("/api/v1/mobile/status")).json()
        assert status["telegram"]["ready"] is False and status["telegram"]["bot"] is None


def test_the_routes_are_signed_in_and_the_bot_has_one_webhook() -> None:
    from app.api.v1.router import api_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(api_router)
    paths = app.openapi()["paths"]
    for path, method in (("/api/v1/mobile/status", "get"), ("/api/v1/mobile/telegram/link-code", "post"), ("/api/v1/mobile/telegram/links/{chat_id}", "delete")):
        assert paths[path][method].get("security"), path
    assert not any(p.startswith("/api/v1/mobile/telegram/webhook") or p.startswith("/api/v1/mobile/sms") for p in paths)  # one bot, the Sentinel's
    assert "/api/v1/sentinel/webhook/telegram" in paths


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend sources not present")
def test_the_pwa_is_installable_and_the_thumb_bar_is_wired() -> None:
    public, src = FRONTEND / "public", FRONTEND / "src"
    manifest = json.loads((public / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/" and manifest["short_name"]
    icons = {(i["sizes"], i.get("purpose", "any")) for i in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= icons
    for icon in manifest["icons"]:
        blob = (public / icon["src"].lstrip("/")).read_bytes()
        width, height = int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
        assert blob[:8] == b"\x89PNG\r\n\x1a\n" and f"{width}x{height}" == icon["sizes"]  # a real PNG of the size it claims
    shortcuts = {s["url"] for s in manifest["shortcuts"]}
    assert {"/oracle", "/vault", "/consultant"} <= shortcuts
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest" href="/manifest.webmanifest"' in html and 'rel="apple-touch-icon"' in html and "viewport-fit=cover" in html and "/vite.svg" not in html
    sw = (public / "sw.js").read_text(encoding="utf-8")
    assert "/api/" in sw and "navigate" in sw  # the API is never cached; navigations fall back to the shell offline
    pwa = (src / "lib" / "pwa.ts").read_text(encoding="utf-8")
    assert "startPwa()" in (src / "main.tsx").read_text(encoding="utf-8")
    assert "serviceWorker" in pwa and "import.meta.env.PROD" in pwa and "beforeinstallprompt" in pwa  # production only; the install prompt kept
    nav = (src / "components" / "layout" / "MobileBottomNav.tsx").read_text(encoding="utf-8")
    app = (src / "App.tsx").read_text(encoding="utf-8")
    assert "<MobileBottomNav" in app and "md:hidden" in nav and "material-symbols-outlined" in nav and "lucide-react" not in nav
    for target in ("/oracle", "tab=score", "/consultant", "/vault", "tab=builder"):  # Score: The Oracle's Score engine tab since Group 111
        assert target in nav, target
    panel = (src / "components" / "mobile" / "PocketTwinPanel.tsx").read_text(encoding="utf-8")
    assert "/mobile/telegram/link-code" in (src / "lib" / "pocket_twin.ts").read_text(encoding="utf-8") and "<PocketTwinPanel" in (src / "pages" / "CommandCenter.tsx").read_text(encoding="utf-8")
    assert not re.search(r"Bumrah|84\.00|94/100", panel + nav)
