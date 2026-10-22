"""The BetDoc Consultant (Group 105, ASHVATTHAMA, points b.6 and b.13b): one consultation on one slip. Developed for Amit
Ashok Kumar Patnaik.

``consult`` runs the slip through the 15-pillar fortress at the user's book as a trial (nothing recorded in the twin's
audit, no shield, XP or alert: ``manual_parlay.find_cure``), with the auto-fix's search for a better leg, then hands
the measurements to the three specialists (``app.domain.consultant.diagnostics``):

* the Doctor's pathologies and prescription (the auto-fix's best quoted alternative, only when it scores higher);
* the Lawyer's settlement rules for these legs at this book;
* the Consultant's capital view at KUMBHA's stake: payout, Section 115BBJ / 194BA tax, stop-loss floor, downside,
  USD at TODAR MAL's fresh rate (none without one);
* TipMaster: each leg's measured public shares (``competitive_intel_live_service.measured_shares``) against BetDoc.

The verdict is sealed (HMAC-SHA256 over the report's canonical JSON) and recorded in ``consultant_verdicts``: every
consultation is a signed verdict, as every fortress inspection is an audit. Nothing is placed from here.
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.domain.backtesting import inplay_stoploss_math as slm
from app.domain.cfo import growth_math as gm
from app.domain.competitive_intel import live_consensus_math as lcm
from app.domain.consultant import diagnostics as dx
from app.domain.paper_trading.tax import TDS_RATE
from app.models.consultant import ConsultantVerdictModel
from app.services import ashoka_market
from app.services import manual_parlay as workbench
from app.services.competitive_intel_live_service import measured_shares
from app.services.currency_service import CurrencyService, CurrencyUnavailableError
from app.services.twin import vetting

COMMANDER = "ASHVATTHAMA"
CENT = Decimal("0.01")


def _plain(value: Any) -> Any:
    """JSON-native, so the seal covers exactly what is stored (a non-finite float is no figure: null)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _secret(settings: Settings) -> str:
    return settings.SECRET_KEY.get_secret_value()


async def _usd(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, now: datetime) -> tuple[Decimal | None, str | None]:
    """INR per USD at a fresh rate, else (None, None): no figure is shown in USD without one."""
    try:
        async with sessions() as session:
            rate = await CurrencyService(settings, redis).inr_per_unit(session, "USD", now, allow_refresh=False)  # TODAR MAL refreshes on its own beat
    except CurrencyUnavailableError:
        return None, None
    return rate.inr_per_unit, rate.as_of.isoformat()


def _slip_view(audit: Any, skin: str, margins: dict[str, float | None]) -> dict[str, Any]:
    slip = audit.slip or {}
    legs = [{
        "leg_id": leg["leg_id"], "fixture": leg.get("fixture"), "label": leg.get("label"), "market": leg.get("market"), "selection": leg.get("selection"),
        "sport_key": leg.get("sport_key"), "league": leg.get("league"), "kickoff": leg.get("kickoff"), "price": dx.leg_price(leg, audit.bookmaker),
        "prices": leg.get("prices"), "fair_probability": leg.get("fair_probability"), "ev": leg.get("ev"), "margin": margins.get(leg["leg_id"]),
    } for leg in slip.get("legs", [])]
    return {"slip_id": audit.slip_id, "kind": audit.kind, "title": slip.get("title"), "skin": skin, "book": audit.bookmaker, "leg_ids": list(audit.leg_ids),
            "odds": None if audit.total_odds is None else str(audit.total_odds), "joint_ev": audit.joint_ev, "joint_probability": audit.joint_probability,
            "pillars_passed": audit.pillars_passed, "is_vetted": audit.is_vetted, "legs": legs}


def _prescription(cure: workbench.Cure, pathologies: Sequence[dx.Pathology], cap: Decimal | None) -> tuple[dict[str, Any] | None, dx.CureView | None]:
    if cure.replaced is None:
        return None, None
    treats = sorted({p.code.value for p in pathologies if cure.replaced["leg_id"] in p.legs})
    if cure.best is None or cure.leg_ids is None or cure.replacement is None:
        message = "No quoted alternative rates higher than the slip as it is." if cure.tried else "No positive-EV leg on another fixture is quoted at this book."
        return {"available": False, "replaced": cure.replaced, "treats": treats, "before": cure.before, "tried": cure.tried, "message": message}, None
    best = cure.best.audit
    payout = None if best.total_odds is None else best.stake_inr * best.total_odds
    clean = (not any(str(p.get("status")).upper() == "FAIL" for p in best.pillars) and (best.joint_ev or 0.0) > 0
             and (cap is None or payout is None or payout <= cap))
    after = {"score": cure.best.rating.score, "tier": cure.best.rating.tier}
    view = dx.CureView(str(cure.replaced["label"]), str(cure.replacement["label"]), after["score"], after["tier"], clean)
    return {"available": True, "replaced": cure.replaced, "replacement": cure.replacement, "treats": treats, "before": cure.before, "after": after,
            "clean": clean, "leg_ids": cure.leg_ids, "joint_ev_after": best.joint_ev, "tried": cure.tried}, view


async def _tipmaster(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, audit: Any, margins: dict[str, float | None],
                     now: datetime) -> dict[str, Any]:
    fixtures = {leg_id.split("|", 1)[0] for leg_id in audit.leg_ids}
    candidates, _ = await ashoka_market.load_candidates(redis, settings, now - vetting.LIVE_LOOKBACK, fixtures=fixtures)
    wanted = set(audit.leg_ids)
    live = [leg for leg in candidates if leg.leg_id in wanted]
    async with sessions() as session:
        shares = await measured_shares(session, redis, settings, live, now)
    policy = lcm.ConsensusPolicy.from_settings(settings)
    rows = []
    for leg in (audit.slip or {}).get("legs", []):
        sh = shares.get(leg["leg_id"])
        rows.append(dx.leg_consensus(leg, audit.bookmaker, tickets=None if sh is None else sh.tickets, money=None if sh is None else sh.money,
                                     opening=None if sh is None else sh.opening, source=None if sh is None else sh.source, margin=margins.get(leg["leg_id"]),
                                     policy=policy))
    return dx.slip_consensus(rows)


async def consult(sessions: async_sessionmaker[AsyncSession], redis: Redis, settings: Settings, user_id: uuid.UUID, *, leg_ids: Sequence[str], kind: str | None,
                  skin: str, bankroll: Decimal | None, now: datetime) -> dict[str, Any]:
    """The three specialists on the slip at the skin's book, their verdict sealed and recorded."""
    if skin not in workbench.SKINS:
        raise workbench.WorkbenchRefusal("UNKNOWN_SKIN", f"skin is one of {', '.join(workbench.SKINS)}", 422)
    cure = await workbench.find_cure(sessions, redis, settings, user_id, leg_ids=leg_ids, kind=kind, book=workbench.SKINS[skin][0], bankroll=bankroll, now=now)
    run = cure.current
    audit = run.audit
    book = audit.bookmaker
    legs = (audit.slip or {}).get("legs", [])
    margins = {row["leg_id"]: row.get("account_margin") for row in run.margins["legs"]}
    lawyer = dx.LawyerPolicy.from_settings(settings)
    cap = lawyer.cap_for(book)
    stake = audit.stake_inr
    payout = None if audit.total_odds is None else (stake * audit.total_odds).quantize(CENT)

    pathologies, unexamined = dx.diagnose(audit.pillars, legs, book, margins, heavy_juice_min=settings.MANUAL_PARLAY_HEAVY_JUICE_MIN, payout=payout, max_payout=cap)
    prescription, cure_view = _prescription(cure, pathologies, cap)
    clauses = dx.counsel(legs, book, audit.pillars, lawyer, payout=payout)
    inr_per_usd, usd_as_of = await _usd(sessions, redis, settings, now)
    pillar_13 = next((p for p in audit.pillars if int(p["number"]) == 13), {})
    sizing = pillar_13.get("metrics") or {}
    simulation = (audit.slip or {}).get("simulation", {})
    tds_rate = Decimal(str(settings.ORACLE_TDS_RATE)) if settings.ORACLE_TDS_RATE is not None else TDS_RATE
    capital = dx.capital(bankroll=audit.bankroll_inr, stake=stake, odds=audit.total_odds, kelly_fraction=audit.kelly_fraction, sizing=sizing,
                         kelly_multiplier=float(settings.CFO_KELLY_FRACTION_DEFAULT), simulation=simulation, stop_policy=slm.StopLossPolicy.from_settings(settings),
                         tds_rate=tds_rate, inr_per_usd=inr_per_usd, usd_as_of=usd_as_of, max_payout=cap, sizing_reason=pillar_13.get("reason"))
    capital["what_if"]["max_fraction"] = gm.SizingPolicy.from_settings(settings).max_fraction
    tipmaster = await _tipmaster(sessions, redis, settings, audit, margins, now)
    status, title, summary = dx.verdict(pathologies, is_vetted=audit.is_vetted, score=run.rating.score, tier=run.rating.tier,
                                        gold_floor=float(settings.MANUAL_PARLAY_TIERS["GOLD"]), joint_ev=audit.joint_ev, stake=stake, cure=cure_view,
                                        unexamined=len(unexamined))
    async with sessions() as session:
        credit = await vetting.developer_credit(session)
    report_id = uuid.uuid4()
    body = _plain(json.loads(json.dumps({
        "report_id": str(report_id), "signed_at": now.isoformat(), "signed_for": str(user_id), "signed_by": f"{COMMANDER} · BetDoc Consultant",
        "verdict": {"status": status.value, "title": title, "summary": summary},
        "slip": _slip_view(audit, skin, margins),
        "rating": run.rating.as_dict(),
        "doctor": {"pathologies": [p.as_dict() for p in pathologies], "unexamined": unexamined, "prescription": prescription,
                   "clean_bill": not pathologies and not unexamined},
        "lawyer": {"bookmaker": book, "clauses": [c.as_dict(book) for c in clauses], "references_on_file": sum(c.reference is not None for c in clauses)},
        "consultant": capital,
        "tipmaster": tipmaster,
        "developer_credit": credit,
    }, default=str)))
    mark = dx.seal(body, _secret(settings))
    async with sessions() as session:
        session.add(ConsultantVerdictModel(id=report_id, user_id=user_id, slip_id=audit.slip_id, leg_ids=list(audit.leg_ids), bookmaker=book, verdict=status.value,
                                           slip_score=run.rating.score, tier=run.rating.tier, report=body, seal=mark, signed_at=now))
        await session.commit()
    return {**body, "seal": mark, "seal_algorithm": "HMAC-SHA256"}


def swap(leg_ids: Sequence[str], replace: str, with_leg: str) -> list[str]:
    """The slip with one leg swapped in place: the replacement must be on a fixture the rest of the slip does not touch."""
    legs = list(dict.fromkeys(leg_ids))
    if replace not in legs:
        raise workbench.WorkbenchRefusal("NOT_IN_SLIP", "the leg to replace is not on the slip", 422)
    taken = {leg.split("|", 1)[0] for leg in legs if leg != replace}
    if with_leg in legs or with_leg.split("|", 1)[0] in taken:
        raise workbench.WorkbenchRefusal("SAME_FIXTURE", "the replacement is on a fixture the slip already has", 422)
    return [with_leg if leg == replace else leg for leg in legs]


def _row_view(row: ConsultantVerdictModel, settings: Settings) -> dict[str, Any]:
    report = row.report or {}
    stamp = row.signed_at if row.signed_at.tzinfo else row.signed_at.replace(tzinfo=UTC)  # SQLite keeps no offset
    return {"report_id": str(row.id), "signed_at": report.get("signed_at") or stamp.isoformat(), "verdict": row.verdict, "title": (report.get("verdict") or {}).get("title"),
            "slip_score": row.slip_score, "tier": row.tier, "bookmaker": row.bookmaker, "legs": [leg.get("label") for leg in (report.get("slip") or {}).get("legs", [])],
            "odds": (report.get("slip") or {}).get("odds"), "seal": row.seal, "seal_holds": dx.seal_holds(report, _secret(settings), row.seal)}


async def history(session: AsyncSession, settings: Settings, user_id: uuid.UUID, limit: int) -> list[dict[str, Any]]:
    rows = (await session.execute(select(ConsultantVerdictModel).where(ConsultantVerdictModel.user_id == user_id)
                                  .order_by(ConsultantVerdictModel.signed_at.desc()).limit(limit))).scalars().all()
    return [_row_view(r, settings) for r in rows]


async def verdict(session: AsyncSession, settings: Settings, user_id: uuid.UUID, report_id: uuid.UUID) -> dict[str, Any]:
    """A signed verdict as recorded, its seal re-checked against the stored report."""
    row = await session.get(ConsultantVerdictModel, report_id)
    if row is None or row.user_id != user_id:
        raise workbench.WorkbenchRefusal("VERDICT_NOT_FOUND", "no such verdict of yours", 404)
    return {**row.report, "seal": row.seal, "seal_algorithm": "HMAC-SHA256", "seal_holds": dx.seal_holds(row.report, _secret(settings), row.seal)}


def rules(settings: Settings) -> dict[str, Any]:
    """The Lawyer's rule book: the general practice per rule, and what the user recorded from each book's own rules."""
    policy = dx.LawyerPolicy.from_settings(settings)
    return {
        "rules": [{"code": r.code, "title": r.title, "applies_to": r.applies_to, "summary": r.summary, "hedge": r.hedge, "basis": r.basis} for r in dx.RULES.values()],
        "references": policy.references, "max_payout_inr": {book: str(cap) for book, cap in policy.max_payout.items()},
        "cricket_min_overs": policy.cricket_min_overs, "palpable_edge": policy.palpable_edge,
        "note": "BetDoc holds no copy of any bookmaker's terms: a clause reference or maximum payout appears only when you recorded it from the book's "
                "own rules (CONSULTANT_RULE_REFERENCES, CONSULTANT_MAX_PAYOUT_INR).",
    }
