"""The Pocket Twin on Telegram (Group 109, point b.10): BetDoc's desk answered in chat. Developed for Amit Ashok Kumar Patnaik.

The bot is the Sentinel's Telegram channel (Group 68): its token and webhook secret stay in the vault, and only its
configured chats (and admin users) are answered. Each reply below is read from the engine the PWA shows, never
written for the chat:

* ``/matches [sport]``: the Oracle's match board (``match_board_service``): the winner market at Parimatch and 1xBet,
  BetDoc's fair chances, the market-fitted projection and the fixture's best leg priced above fair;
* ``/score <match>``: the fixture's prediction (``fixture_prediction``): the consensus, the lineup model, and for
  football the Dixon-Coles scorelines; other sports say their simulators need stats no feed carries;
* ``/chef [sport] [stake]``: the Chef's Table (``chef_table``, Group 104) for the linked account, KUMBHA's stake, and
  a coupon to copy;
* ``/consultant``: the account's latest signed verdict (Group 105) and whether its seal holds;
* ``/slip``: the latest issuable Bet-Slip ticket (Group 106), its coupon text;
* ``/vault``: the CFO bankroll, KUMBHA's drawdown regime, the last 24 hours' settled P&L, the bookmaker accounts and
  the active stop-loss shields;
* ``/ask <question>``: the Scout Oracle (Group 100), briefed on the linked account's live book.

Account commands need the chat linked to a BetDoc account: a one-time code from the PWA, sent as ``/link CODE`` (or
through the bot's ``t.me`` deep link). Nothing is placed, staked or moved from a chat; money moves stay in the PWA.
"""

from __future__ import annotations

import html
import re
import secrets
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS
from app.domain.oracle.slip_formatter import inr
from app.models import User
from app.models.sentinel import ChannelName, SentinelChannel
from app.services.sentinel_bus import SentinelKeys

WINNER_BOOKS = ("parimatch", "1xbet")  # the books the desk bets at, side by side
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
CODE_LENGTH = 8
LINKS = "links"  # the Telegram channel's config: chat id -> the linked account
Keyboard = list[list[dict[str, str]]]


@dataclass(slots=True)
class TwinReply:
    text: str
    keyboard: Keyboard | None = None
    outcome: str = "ANSWERED"
    detail: dict[str, Any] = field(default_factory=dict)


# ================================================================ formatting
def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def pct(p: float | None, digits: int = 1) -> str:
    return "—" if p is None else f"{100.0 * p:.{digits}f}%"


def signed_pct(p: float | None, digits: int = 1) -> str:
    return "—" if p is None else f"{100.0 * p:+.{digits}f}%"


def local_time(iso: str | datetime | None, settings: Settings) -> str:
    if not iso:
        return "—"
    moment = iso if isinstance(iso, datetime) else datetime.fromisoformat(str(iso))
    moment = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(ZoneInfo(settings.ORACLE_TIMEZONE))
    return moment.strftime("%a %d %b %H:%M ") + (moment.tzname() or settings.ORACLE_TIMEZONE)


def app_link(settings: Settings, path: str) -> str | None:
    """A link into the PWA, when it is served over https from a public host (Telegram refuses anything else)."""
    base = (settings.PUBLIC_APP_URL or "").strip().rstrip("/")
    parsed = urlparse(base)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not host or host == "localhost" or host.endswith(".localhost") or host.startswith("127.") or host == "[::1]":
        return None
    return f"{base}{path}"


def url_button(settings: Settings, text: str, path: str) -> dict[str, str] | None:
    url = app_link(settings, path)
    return None if url is None else {"text": text, "url": url}


def rows(*candidates: Sequence[dict[str, str] | None]) -> Keyboard | None:
    out = [[b for b in row if b is not None] for row in candidates]
    out = [row for row in out if row]
    return out or None


def sport_of(arg: str | None, settings: Settings) -> str | None:
    """'cricket' -> 'Cricket', 'soccer' -> 'Football': the board's own sport labels."""
    if not arg:
        return None
    wanted = arg.casefold()
    for label, prefixes in settings.MANUAL_PARLAY_SPORTS.items():
        if wanted == label.casefold() or wanted in prefixes or any(wanted == p.split("_", 1)[0] for p in prefixes):
            return label
    return None


def _family_label(sport_key: str | None, settings: Settings) -> str | None:
    key = sport_key or ""
    return next((label for label, prefixes in settings.MANUAL_PARLAY_SPORTS.items() if any(key.startswith(p) for p in prefixes)), None)


# ================================================================ linking a chat to an account
def _code_key(settings: Settings, code: str) -> str:
    return f"{SentinelKeys(settings).prefix}:telegram:link:{code}"


async def issue_link_code(redis: Redis, settings: Settings, user_id: uuid.UUID) -> tuple[str, int]:
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    await redis.set(_code_key(settings, code), str(user_id), ex=settings.POCKET_TWIN_LINK_CODE_SECONDS)
    return code, settings.POCKET_TWIN_LINK_CODE_SECONDS


async def redeem_link_code(redis: Redis | None, settings: Settings, code: str) -> uuid.UUID | None:
    """The account a code was issued for, once (a redeemed or expired code is None)."""
    clean = code.strip().upper()
    if redis is None or not re.fullmatch(f"[{CODE_ALPHABET}]{{{CODE_LENGTH}}}", clean):
        return None
    try:
        raw = await redis.getdel(_code_key(settings, clean))
    except (RedisError, OSError):
        return None
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


def linked_user_id(config: Mapping[str, Any], chat_id: int | None) -> uuid.UUID | None:
    entry = (config.get(LINKS) or {}).get(str(chat_id)) if chat_id is not None else None
    try:
        return uuid.UUID(str(entry["user_id"])) if isinstance(entry, Mapping) and entry.get("user_id") else None
    except ValueError:
        return None


async def set_link(sessions: async_sessionmaker[AsyncSession], chat_id: int, user: User | None, now: datetime) -> None:
    """Link the chat to the account (or unlink it with None), in the Telegram channel's config."""
    async with sessions() as session:
        row = await session.get(SentinelChannel, ChannelName.TELEGRAM.value)
        if row is None:
            return
        links = dict((row.config or {}).get(LINKS) or {})
        if user is None:
            links.pop(str(chat_id), None)
        else:
            links[str(chat_id)] = {"user_id": str(user.id), "username": user.username, "linked_at": now.isoformat()}
        row.config = {**(row.config or {}), LINKS: links}
        await session.commit()


async def links_of(sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID) -> list[dict[str, Any]]:
    async with sessions() as session:
        row = await session.get(SentinelChannel, ChannelName.TELEGRAM.value)
    links = ((row.config or {}).get(LINKS) or {}) if row is not None else {}
    return [{"chat_id": chat, "linked_at": entry.get("linked_at")} for chat, entry in links.items()
            if isinstance(entry, Mapping) and entry.get("user_id") == str(user_id)]


async def user_of(sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID | None) -> User | None:
    if user_id is None:
        return None
    async with sessions() as session:
        user = await session.get(User, user_id)
    return user if user is not None and getattr(user, "is_active", True) else None


def not_linked(settings: Settings) -> TwinReply:
    where = app_link(settings, "/command-center")
    return TwinReply("🔗 This chat is not linked to a BetDoc account yet.\nIn BetDoc, open <b>Command Center → Pocket Twin → Link Telegram</b>, "
                     "then send <code>/link CODE</code> here." + (f"\n{esc(where)}" if where else ""), outcome="NOT_LINKED")


# ================================================================ /matches
def _winner(card: Mapping[str, Any]) -> Mapping[str, Any] | None:
    markets = card.get("markets") or []
    return next((m for m in markets if m.get("kind") == "MATCH_ODDS"), markets[0] if markets else None)


def _best_leg(card: Mapping[str, Any]) -> Mapping[str, Any] | None:
    legs = [s for m in card.get("markets") or [] for s in m.get("selections") or [] if s.get("best") and s.get("ev") is not None]
    best = max(legs, key=lambda s: s["ev"], default=None)
    return best if best is not None and best["ev"] > 0 else None


def match_card(card: Mapping[str, Any], settings: Settings) -> str:
    lines = [f"<b>{esc(card['home'])} v {esc(card['away'])}</b> · {esc(card.get('league_name') or card.get('league') or '')}",
             "   " + ("🔴 in play" if card.get("in_play") else f"kicks off {local_time(card.get('kickoff'), settings)}")]
    winner = _winner(card)
    if winner:
        prices = []
        for s in winner.get("selections") or []:
            quoted = " / ".join(f"{s['prices'][b]:.2f}" if s.get("prices", {}).get(b) else "—" for b in WINNER_BOOKS)
            prices.append(f"{esc(s['label'].split(': ', 1)[-1])} {quoted}")
        lines.append(f"   {esc(winner['market'])} (Parimatch / 1xBet): " + " · ".join(prices))
        lines.append("   BetDoc fair: " + " · ".join(f"{esc(s['label'].split(': ', 1)[-1])} {pct(s.get('fair_probability'))}" for s in winner.get("selections") or []))
    projection = card.get("projection")
    if projection:
        xg = projection.get("expected_goals") or []
        lines.append(f"   Projected: {esc(projection['score'])} ({pct(projection.get('probability'))})" + (f" · xG {xg[0]:.2f}-{xg[1]:.2f}" if len(xg) == 2 else ""))
    best = _best_leg(card)
    if best:
        book = best["best"]["book"]
        lines.append(f"   ⚡ Best edge: {esc(best['label'])} @ {best['best']['odds']:.2f} ({esc(ASHOKA_BOOKMAKERS.get(book, book))}), EV {signed_pct(best['ev'])}")
    else:
        lines.append("   No leg priced above BetDoc's fair chance right now")
    return "\n".join(lines)


async def matches(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, args: Sequence[str]) -> TwinReply:
    from app.services import match_board_service  # noqa: PLC0415 - the oracle engine is heavy

    sport = sport_of(args[0], settings) if args else None
    if args and sport is None:
        return TwinReply(f"No sport called {esc(args[0])}. Try: {esc(', '.join(settings.MANUAL_PARLAY_SPORTS))}.", outcome="BAD_ARGUMENT")
    if redis is None:
        return TwinReply("The live market is unreadable right now (Redis is down).", outcome="NO_MARKET")
    async with sessions() as db:
        board = await match_board_service.match_board(redis, db, settings, now, sport=sport, window="all")
    cards = board["fixtures"][: settings.POCKET_TWIN_MATCHES]
    if not cards:
        return TwinReply(f"🏟️ No {esc(sport or '')} fixture is priced on the board right now.".replace("  ", " "), outcome="EMPTY")
    head = f"🏟️ <b>Match board{' · ' + esc(sport) if sport else ''}</b> · {len(cards)} of {board['total']}"
    text = head + "\n\n" + "\n\n".join(match_card(c, settings) for c in cards)
    keyboard = rows(*[[{"text": f"🎯 {c['home']} v {c['away']}"[:60], "callback_data": f"/score {c['fixture_id']}"[:64]},
                       url_button(settings, "Open", f"/oracle?tab=board&fixture={c['fixture_id']}")] for c in cards])
    return TwinReply(text, keyboard, "MATCHES", {"fixtures": [c["fixture_id"] for c in cards], "sport": sport})


# ================================================================ /score
async def _resolve(redis: Redis | None, settings: Settings, now: datetime, query: str) -> tuple[Any | None, list[Any]]:
    from app.services.the_wire.fixtures import tracked  # noqa: PLC0415

    fixtures = await tracked(redis, settings, now)
    exact = next((f for f in fixtures if f.fixture_id == query.strip()), None)
    if exact is not None:
        return exact, []
    words = [w for w in re.findall(r"[a-z0-9]+", query.casefold()) if w not in ("v", "vs", "versus", "against")]
    if not words:
        return None, fixtures[:6]
    found = [f for f in fixtures if all(w in f"{f.home} {f.away}".casefold() for w in words)]
    return (found[0], []) if len(found) == 1 else (None, found[:6])


def _sides(probs: Mapping[str, float], home: str, away: str) -> str:
    names = {"HOME": home, "AWAY": away, "DRAW": "Draw"}
    return " · ".join(f"{esc(names.get(k, k))} {pct(v)}" for k, v in probs.items())


async def score(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime, args: Sequence[str]) -> TwinReply:
    from app.services import fixture_prediction  # noqa: PLC0415 - numerical engines

    fixture, choices = await _resolve(redis, settings, now, " ".join(args))
    if fixture is None:
        if not choices:
            what = f" matches {esc(' '.join(args))}" if args else " is tracked"
            return TwinReply(f"🎯 No fixture on the live board{what}.", outcome="NOT_FOUND")
        keyboard = rows(*[[{"text": f"{f.home} v {f.away}"[:60], "callback_data": f"/score {f.fixture_id}"[:64]}] for f in choices])
        return TwinReply("🎯 Which match?" if args else "🎯 Pick a match:", keyboard, "CHOOSE")
    p = await fixture_prediction.predict(sessions, redis, settings, fixture, now)
    lines = [f"🎯 <b>{esc(fixture.home)} v {esc(fixture.away)}</b> · kicks off {local_time(fixture.kickoff, settings)}"]
    if not p["consensus"]:
        lines.append("The live board has no Match Odds consensus for this fixture right now, so there is nothing to fit.")
        return TwinReply("\n".join(lines), outcome="NO_CONSENSUS", detail={"fixture_id": fixture.fixture_id})
    lines.append(f"Market (de-vigged consensus): {_sides(p['consensus'], fixture.home, fixture.away)}")
    if p["moved"]:
        lines.append(f"With {p['absences'] - len(p['unrated'])} rated absence(s): {_sides(p['adjusted'], fixture.home, fixture.away)}")
    elif p["absences"]:
        lines.append(f"Lineup model: {p['absences']} absence(s) recorded" + (f", {len(p['unrated'])} unrated (not counted)" if p["unrated"] else ", none moves it"))
    fit = p["market_fit"]
    if fit:
        xg = fit["expected_goals"]
        lines.append("Scorelines (Dixon-Coles, exact): " + " · ".join(f"{esc(s['score'])} {pct(s['probability'])}" for s in fit["top_scorelines"]))
        extra = [f"xG {xg[0]:.2f}-{xg[1]:.2f}"] + ([f"over 2.5 goals {pct(fit['over_2_5'])}"] if fit.get("over_2_5") is not None else []) + [f"both score {pct(fit['btts_yes'])}"]
        lines.append(" · ".join(extra))
        if p["lineup_fit"]:
            adj = p["lineup_fit"]
            lines.append(f"With the absences: xG {adj['expected_goals'][0]:.2f}-{adj['expected_goals'][1]:.2f}, likeliest {esc(adj['top_scorelines'][0]['score'])}")
    else:
        lines.append("Innings, sets and rounds need the teams' stats, which no feed carries: run them in Score Prediction (The Lab).")
    keyboard = rows([url_button(settings, "Open in the Oracle", f"/oracle?tab=board&fixture={fixture.fixture_id}"),
                     url_button(settings, "Score Prediction", "/lab#score-prediction")])
    return TwinReply("\n".join(lines), keyboard, "SCORE", {"fixture_id": fixture.fixture_id, "model": p["model"]})


# ================================================================ /chef
async def _usd_rate(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, now: datetime) -> Decimal | None:
    from app.services.currency_service import CurrencyService, CurrencyUnavailableError  # noqa: PLC0415

    try:
        async with sessions() as session:
            return (await CurrencyService(settings, redis).inr_per_unit(session, "USD", now, allow_refresh=False)).inr_per_unit
    except CurrencyUnavailableError:
        return None  # fails closed: no USD figure rather than an assumed rate


def _usd(amount: Decimal, rate: Decimal | None) -> str:
    return "" if rate is None or rate <= 0 or amount <= 0 else f" (≈ ${amount / rate:,.2f})"


def _plate_price(plate: Mapping[str, Any], i: int) -> float | None:
    recipe = plate.get("recipe") or []
    if i < len(recipe) and recipe[i].get("price"):
        return float(recipe[i]["price"])
    prices = plate["legs"][i].get("prices") or {}
    return prices.get(plate.get("book")) if plate.get("book") else None


def coupon(plate: Mapping[str, Any]) -> str:
    book = ASHOKA_BOOKMAKERS.get(plate.get("book"), plate.get("book") or "best price")
    lines = [f"BETDOC CHEF'S TABLE · {plate['title']} · {plate['kind']} · {book}"]
    for i, leg in enumerate(plate["legs"]):
        price = _plate_price(plate, i)
        lines.append(f"{i + 1}. {leg['fixture']}: {leg['label']} @ {price:.2f}" if price else f"{i + 1}. {leg['fixture']}: {leg['label']}")
    lines.append(f"Odds {float(plate['odds']):.2f} · stake {inr(Decimal(str(plate['stake_inr'])))}")
    return "\n".join(lines)


async def chef(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user_id: uuid.UUID, now: datetime,
               args: Sequence[str]) -> TwinReply:
    from app.services import chef_table  # noqa: PLC0415 - the fortress is heavy

    stake: Decimal | None = None
    sport: str | None = None
    for arg in args:
        try:
            value = Decimal(arg.replace(",", "").lstrip("₹"))
            if value > 0:
                stake = value
                continue
        except InvalidOperation:
            pass
        sport = sport_of(arg, settings)
        if sport is None:
            return TwinReply(f"Not a sport or a stake: {esc(arg)}. Try <code>/chef cricket 1500</code>.", outcome="BAD_ARGUMENT")
    if redis is None:
        return TwinReply("The live market is unreadable right now (Redis is down).", outcome="NO_MARKET")
    table = await chef_table.chef_table(sessions, redis, settings, user_id, None, now)
    plates = [p for p in table["plates"] if sport is None or all(_family_label(leg.get("sport_key"), settings) == sport for leg in p["legs"])]
    if not plates:
        why = f" all {esc(sport)}" if sport else ""
        return TwinReply(f"👨‍🍳 Nothing on the Chef's Table{why} right now: {table['rated']} of {table['considered']} vetted slips were rated, none served"
                         f"{' in that sport' if sport else ''}. A slip with negative EV at the retail books, or one a pillar vetoes, is never served.", outcome="EMPTY")
    rate = await _usd_rate(sessions, redis, settings, now)
    top = plates[0]
    rating = top["rating"]
    odds = Decimal(str(top["odds"]))
    kumbha = Decimal(str(top["stake_inr"]))
    book = ASHOKA_BOOKMAKERS.get(top["book"], top["book"] or "the best retail book")
    lines = [f"👨‍🍳 <b>{esc(top['title'])}</b> · {esc(top['kind'])}",
             f"Slip Score <b>{rating['score']:.0f} {esc(rating['tier'])}</b> · odds {odds:.2f} at {esc(book)} · joint EV {signed_pct(top.get('joint_ev'))} · lands {pct(top.get('joint_probability'))}"]
    if kumbha > 0:
        lines.append(f"KUMBHA's stake: <b>{inr(kumbha)}</b>{_usd(kumbha, rate)} → {inr(kumbha * odds)} if it lands")
    else:
        lines.append("KUMBHA stakes nothing on it now (no bankroll, or the drawdown regime holds it back)")
    if stake is not None:
        lines.append(f"At your {inr(stake)}: {inr(stake * odds)} if it lands{_usd(stake, rate)}")
    for i, leg in enumerate(top["legs"]):
        price = _plate_price(top, i)
        lines.append(f"{i + 1}. {esc(leg['fixture'])}: {esc(leg['label'])}" + (f" @ {price:.2f}" if price else "") +
                     f" (fair {pct(leg.get('fair_probability'))}, EV {signed_pct(leg.get('ev'))})")
    lines.append(f"<pre>{esc(coupon(top))}</pre>")
    for other in plates[1:]:
        lines.append(f"{esc(other['title'])}: Slip Score {other['rating']['score']:.0f} {esc(other['rating']['tier'])}, odds {float(other['odds']):.2f}, "
                     f"{len(other['legs'])} leg{'' if len(other['legs']) == 1 else 's'}")
    lines.append("<i>Tap the coupon to copy it. Nothing is placed from here: place it at the book, then record it in BetDoc.</i>")
    keyboard = rows([url_button(settings, "🩺 Consult in BetDoc", "/consultant"), url_button(settings, "Open the Chef's Table", "/oracle?tab=builder&mode=chef")])
    return TwinReply("\n".join(lines), keyboard, "CHEF", {"slip_id": top["slip_id"], "plates": len(plates)})


# ================================================================ /consultant
async def consultant(sessions: async_sessionmaker[AsyncSession], settings: Settings, user_id: uuid.UUID) -> TwinReply:
    from app.domain.consultant import diagnostics as dx  # noqa: PLC0415
    from app.models.consultant import ConsultantVerdictModel  # noqa: PLC0415

    async with sessions() as session:
        row = (await session.execute(select(ConsultantVerdictModel).where(ConsultantVerdictModel.user_id == user_id)
                                     .order_by(ConsultantVerdictModel.signed_at.desc()).limit(1))).scalar_one_or_none()
    if row is None:
        return TwinReply("🩺 No consultation yet. Put a slip on the Oracle's workbench and open BetDoc Consultant.", rows([url_button(settings, "Open the Consultant", "/consultant")]), "EMPTY")
    report = row.report or {}
    verdict = report.get("verdict") or {}
    sealed = dx.seal_holds(report, settings.SECRET_KEY.get_secret_value(), row.seal)
    doctor = report.get("doctor") or {}
    lawyer = report.get("lawyer") or {}
    money = report.get("consultant") or {}
    lines = [f"🩺 <b>{esc(verdict.get('title') or row.verdict)}</b>", f"Slip Score <b>{row.slip_score:.0f} {esc(row.tier)}</b> · signed {local_time(row.signed_at, settings)} · "
             + ("🔏 seal holds" if sealed else "⚠️ seal broken: the report was altered after signing")]
    if verdict.get("summary"):
        lines.append(esc(verdict["summary"]))
    if doctor.get("clean_bill"):
        lines.append("Doctor: a clean bill, no pathology found")
    else:
        found = doctor.get("pathologies") or []
        lines.append(f"Doctor: {len(found)} patholog{'y' if len(found) == 1 else 'ies'}" + (f": {esc(found[0].get('title', ''))}" if found and isinstance(found[0], Mapping) else ""))
        if doctor.get("prescription"):
            lines.append("Prescription waiting: apply it in BetDoc (it changes the slip on your workbench)")
    risks = [c.get("risk") for c in lawyer.get("clauses") or [] if isinstance(c, Mapping)]
    if risks:
        lines.append(f"Lawyer: {len(risks)} clause(s) at {esc(ASHOKA_BOOKMAKERS.get(lawyer.get('bookmaker'), lawyer.get('bookmaker') or 'the book'))}"
                     + (f", {risks.count('HIGH')} high-risk" if "HIGH" in risks else ""))
    if money.get("stake_inr"):
        tax = money.get("tax") or {}
        lines.append(f"Consultant: stake {inr(Decimal(str(money['stake_inr'])))} → payout {inr(Decimal(str(money.get('payout_inr') or 0)))}"
                     + (f", {inr(Decimal(str(tax['net_post_tax_payout'])))} after TDS" if tax.get("net_post_tax_payout") is not None else ""))
    keyboard = rows([url_button(settings, "Open in BetDoc Consultant", "/consultant"), url_button(settings, "Bet-Slip", "/bet-slip")])
    return TwinReply("\n".join(lines), keyboard, "CONSULTANT", {"report_id": str(row.id), "seal_holds": sealed})


# ================================================================ /slip
async def slip(sessions: async_sessionmaker[AsyncSession], settings: Settings, user_id: uuid.UUID, now: datetime) -> TwinReply:
    from app.services import slip_ornament  # noqa: PLC0415

    async with sessions() as session:
        listed = await slip_ornament.tickets(session, settings, user_id, 5, now)
        first = next((t for t in listed if t.get("issuable")), None)
        view = None if first is None else await slip_ornament.view(session, settings, user_id, uuid.UUID(first["report_id"]), now)
    if view is None:
        reason = f" The latest approved verdict cannot be issued: {esc(listed[0]['message'])}" if listed else ""
        return TwinReply("🎟️ No Bet-Slip ticket to show: a ticket is issued from an APPROVED (GOLD) Consultant verdict." + reason,
                         rows([url_button(settings, "Open the Bet-Slip", "/bet-slip")]), "EMPTY")
    text = f"🎟️ <b>Bet-Slip {esc(view['ticket_no'])}</b>\n<pre>{esc(view['coupon_text'])}</pre>\n<i>Tap the coupon to copy it.</i>"
    return TwinReply(text, rows([url_button(settings, "Open the Bet-Slip", "/bet-slip")]), "SLIP", {"report_id": view["report_id"], "ticket_no": view["ticket_no"]})


# ================================================================ /vault
async def vault(sessions: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, user_id: uuid.UUID, now: datetime) -> TwinReply:
    from app.domain.cfo import growth_math as gm  # noqa: PLC0415
    from app.models.cfo_vault import BankrollAccount  # noqa: PLC0415
    from app.models.omni_vault import VaultBookmakerAccount  # noqa: PLC0415
    from app.models.user_bets_ledger import PlacedStatus, UserPlacedBet  # noqa: PLC0415
    from app.services import manual_parlay  # noqa: PLC0415
    from app.services.cfo import growth_optimizer as growth  # noqa: PLC0415
    from app.services.twin import vetting  # noqa: PLC0415

    async with sessions() as session:
        account = await session.scalar(select(BankrollAccount).where(BankrollAccount.user_id == user_id, BankrollAccount.bot_id.is_(None)))
        wallet = await vetting.bankroll_for(session, user_id, None)
        drawdown = await vetting.drawdown_for(session, user_id, wallet, now, settings.CFO_DRAWDOWN_WINDOW_DAYS)
        latched = await growth.halt_latched(session, user_id)
        settled = (await session.execute(select(func.coalesce(func.sum(UserPlacedBet.pnl_inr), 0), func.count()).where(
            UserPlacedBet.user_id == user_id, UserPlacedBet.status != PlacedStatus.PENDING.value, UserPlacedBet.pnl_inr.is_not(None),
            UserPlacedBet.settled_at >= now - timedelta(hours=24)))).one()
        books = list((await session.execute(select(VaultBookmakerAccount).where(VaultBookmakerAccount.is_active.is_(True))
                                            .order_by(VaultBookmakerAccount.bookmaker_id, VaultBookmakerAccount.priority))).scalars())
        shields = await manual_parlay.shields(session, user_id, active_only=True)
    rate = await _usd_rate(sessions, redis, settings, now)
    regime = gm.damper(drawdown, gm.SizingPolicy.from_settings(settings), latched=latched)
    lines = ["🛡️ <b>The Vault</b> · KUMBHA"]
    if account is None or wallet is None:
        lines.append("No CFO bankroll is set for this account: KUMBHA sizes nothing until one is.")
    else:
        lines.append(f"Bankroll: <b>{inr(wallet)}</b>{_usd(wallet, rate)} · available {inr(account.available_balance)} · at risk {inr(account.exposure_balance)}")
        lines.append(f"Drawdown ({settings.CFO_DRAWDOWN_WINDOW_DAYS:g} days): {pct(drawdown)} · regime {esc(regime.name.replace('_', ' ').lower())} ×{regime.multiplier:g}"
                     + (" · ⛔ halt latched until signed off" if latched else ""))
    pnl, count = Decimal(str(settled[0] or 0)), int(settled[1] or 0)
    lines.append(f"Last 24h settled: {('+' if pnl > 0 else '')}{inr(pnl)} over {count} bet{'' if count == 1 else 's'}" if count else "Last 24h settled: no bet")
    if books:
        lines.append("Bookmaker accounts: " + " · ".join(
            f"{esc(ASHOKA_BOOKMAKERS.get(b.bookmaker_id, b.label))} {esc(b.currency)} {b.balance:,.2f}" + (f" (reserved {b.reserved:,.2f})" if b.reserved else "")
            if b.balance is not None else f"{esc(ASHOKA_BOOKMAKERS.get(b.bookmaker_id, b.label))} balance not entered" for b in books))
    if shields:
        lines.append(f"Stop-loss shields: {len(shields)} active")
        for s in shields[:5]:
            bet = s.get("bet") or {}
            lines.append(f" • {inr(Decimal(bet.get('stake_inr') or 0))} at {esc(ASHOKA_BOOKMAKERS.get(bet.get('bookmaker'), bet.get('bookmaker') or '—'))}: "
                         f"win {pct(s['initial_win_prob'])} → {pct(s['current_win_prob'])}, stop at {pct(s.get('stop_loss_pct'), 0)} down, "
                         f"{esc((s.get('last_advice') or 'watching').replace('_', ' ').lower())}" + (" ⚠️ pull out" if s.get("pullout_triggered") else ""))
    else:
        lines.append("Stop-loss shields: none active")
    lines.append("<i>Cashouts and deposits are recorded in the PWA, never from a chat.</i>")
    keyboard = rows([url_button(settings, "🏦 Open The Vault", "/vault")])
    return TwinReply("\n".join(lines), keyboard, "VAULT", {"shields": len(shields), "books": len(books)})


# ================================================================ /ask
_SOURCES = {"ollama": "the local model", "quant_nlg": "BetDoc's rule engine", "guardrail": "the guardrail"}


async def ask(sessions: async_sessionmaker[AsyncSession], settings: Settings, user_id: uuid.UUID | None, args: Sequence[str]) -> TwinReply:
    from app.domain.oracle_scout.errors import ScoutDomainError  # noqa: PLC0415
    from app.domain.oracle_scout.manager import OracleScoutManager  # noqa: PLC0415
    from app.services.scout_facts import facts_for  # noqa: PLC0415

    question = " ".join(args).strip()
    if not question:
        return TwinReply("🤖 Ask the Scout Oracle anything about the desk: <code>/ask how is my bankroll doing?</code>", outcome="BAD_ARGUMENT")
    manager = OracleScoutManager(settings=settings, latency_s=0)
    async with sessions() as db:
        try:
            entry, _, source = await manager.chat(db, question, "telegram", user_id, await facts_for(user_id, db))
        except ScoutDomainError as exc:
            return TwinReply(f"🤖 {esc(exc.message)}", outcome="REFUSED")
    return TwinReply(f"🤖 <b>Scout Oracle</b>\n{esc(entry.oracle_response)}\n<i>Answered by {_SOURCES.get(source, source)}.</i>", outcome="ASKED", detail={"source": source})
