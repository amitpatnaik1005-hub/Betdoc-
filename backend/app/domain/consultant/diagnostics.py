"""The BetDoc Consultant's three specialists and its signed verdict (Group 105, ASHVATTHAMA, points b.6 and b.13b).
Developed for Amit Ashok Kumar Patnaik.

Every input is something BetDoc measured on the slip: the 15-pillar fortress run at the user's book, the measured
margins, Ashoka's fair probabilities, KUMBHA's sizing, the measured public shares. Nothing here estimates a figure
that was not measured.

* **The Doctor** (the slip is the patient): pathologies from the fortress and the measurements. A failed pillar is
  CRITICAL (it vetoes the slip), an advisory pillar MILD, a heavy margin a WARNING; a leg priced under its fair value
  is CRITICAL. Pillars that lacked evidence are listed as not examined, never as healthy.
* **The Lawyer** (the user is the client): the settlement rules this slip's legs fall under. BetDoc holds no copy of
  any bookmaker's terms, so each rule is the general settlement practice (the ICC playing conditions for cricket's
  minimum overs) with the clause reference only when the user recorded it from the book's own rules page
  (``CONSULTANT_RULE_REFERENCES``), and a book's maximum payout only when the user recorded it
  (``CONSULTANT_MAX_PAYOUT_INR``).
* **The Consultant** (the bankroll is the company): KUMBHA's fractional-Kelly stake, the payout and its Section
  115BBJ / 194BA tax, the in-play stop-loss floor, the slip's simulated downside; USD only at a fresh rate.
* **TipMaster** (merged in, point b.13b): each leg's measured ticket and money shares against BetDoc's EV, classified
  by KARNA's consensus rules; with no measured share the panel says so.
* **The verdict**: APPROVED_GOLD, APPROVED_WITH_CURE or REJECTED, sealed with an HMAC-SHA256 over the report's
  canonical JSON under a key derived from the server's secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from app.domain.backtesting import inplay_stoploss_math as slm
from app.domain.bookmakers.adapters import ASHOKA_BOOKMAKERS
from app.domain.competitive_intel import live_consensus_math as lcm
from app.domain.oracle.markets import MarketKind, parse_market
from app.domain.paper_trading.tax import CESS_RATE, section_115bbj
from app.models.consultant import VerdictStatus

CENT = Decimal("0.01")


class DiseaseCode(StrEnum):
    NEGATIVE_EV = "NEGATIVE_EV"
    CORRELATED_SYNDROME = "CORRELATED_SYNDROME"
    HIGH_VIG_FEVER = "HIGH_VIG_FEVER"
    INJURY_LINEUP_SHOCK = "INJURY_LINEUP_SHOCK"
    FATIGUE_TRAVEL_PENALTY = "FATIGUE_TRAVEL_PENALTY"
    BOOKMAKER_LIMIT_TRAP = "BOOKMAKER_LIMIT_TRAP"
    PILLAR_VETO = "PILLAR_VETO"  # any other failed pillar: a clean bill of health needs every pillar


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    MILD = "MILD"


class RiskLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Divergence(StrEnum):
    PUBLIC_TRAP = "PUBLIC_TRAP"
    CONTRARIAN_ALPHA = "CONTRARIAN_ALPHA"
    SHARP_CONSENSUS = "SHARP_CONSENSUS"
    NEUTRAL_ALIGNED = "NEUTRAL_ALIGNED"
    NOT_MEASURED = "NOT_MEASURED"


DISEASE_TITLES: dict[DiseaseCode, str] = {
    DiseaseCode.NEGATIVE_EV: "Negative EV infection", DiseaseCode.CORRELATED_SYNDROME: "Correlated leg syndrome",
    DiseaseCode.HIGH_VIG_FEVER: "High-vig fever", DiseaseCode.INJURY_LINEUP_SHOCK: "Injury / lineup shock",
    DiseaseCode.FATIGUE_TRAVEL_PENALTY: "Fatigue & travel penalty", DiseaseCode.BOOKMAKER_LIMIT_TRAP: "Bookmaker limit trap",
    DiseaseCode.PILLAR_VETO: "Pillar veto",
}
# the fortress pillars a disease reads (fortress.PILLAR_KEYS): travel, injuries, lineups, liquidity, independence
PILLAR_DISEASE: dict[int, DiseaseCode] = {
    3: DiseaseCode.FATIGUE_TRAVEL_PENALTY, 4: DiseaseCode.INJURY_LINEUP_SHOCK, 5: DiseaseCode.INJURY_LINEUP_SHOCK,
    11: DiseaseCode.BOOKMAKER_LIMIT_TRAP, 12: DiseaseCode.CORRELATED_SYNDROME,
}
_SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.MILD: 2}
_RISK_ORDER = {RiskLevel.HIGH: 0, RiskLevel.MEDIUM: 1, RiskLevel.LOW: 2}


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def leg_price(leg: Mapping[str, Any], book: str | None) -> float | None:
    """The leg's price at the book the slip was priced at."""
    price = (leg.get("prices") or {}).get(book) if book else None
    return None if price is None else float(price)


# ================================================================ the Doctor
@dataclass(frozen=True, slots=True)
class Pathology:
    code: DiseaseCode
    severity: Severity
    diagnosis: str
    legs: tuple[str, ...] = ()
    pillar: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "severity": self.severity.value, "title": DISEASE_TITLES[self.code], "diagnosis": self.diagnosis,
                "legs": list(self.legs), "pillar": self.pillar}


def diagnose(pillars: Sequence[Mapping[str, Any]], legs: Sequence[Mapping[str, Any]], book: str | None, margins: Mapping[str, float | None], *,
             heavy_juice_min: float, payout: Decimal | None, max_payout: Decimal | None) -> tuple[list[Pathology], list[dict[str, Any]]]:
    """(the pathologies, most severe first; the pillars that could not be examined for lack of evidence).

    ``legs``: the audit's leg payloads (``ashoka_market.leg_payload``); ``margins``: leg id -> the book's overround on
    the leg's market (None when the book does not quote the whole market); ``payout``: at KUMBHA's stake."""
    found: list[Pathology] = []
    for leg in legs:
        ev, label = leg.get("ev"), f"{leg.get('label')} ({leg.get('fixture')})"
        if ev is not None and ev < 0:
            price, fair = leg_price(leg, book), leg.get("fair_probability")
            if price is not None and fair is not None:
                text = f"{label} at {price:.2f}: BetDoc's fair chance {fair:.1%} is under the {1 / price:.1%} the price implies; EV {ev:+.1%} on every rupee staked."
            else:
                text = f"{label}: EV {ev:+.1%} on every rupee staked at this book."
            found.append(Pathology(DiseaseCode.NEGATIVE_EV, Severity.CRITICAL, text, (str(leg["leg_id"]),)))
    for leg in legs:
        margin = margins.get(str(leg["leg_id"]))
        if margin is not None and margin >= heavy_juice_min:
            found.append(Pathology(DiseaseCode.HIGH_VIG_FEVER, Severity.WARNING,
                                   f"{leg.get('label')}: the book keeps {margin:.1%} on this market (the heavy-juice line is {heavy_juice_min:.1%}); the same "
                                   "selection pays more where the margin is thinner.", (str(leg["leg_id"]),)))
    fixtures: dict[str, list[str]] = {}
    for leg in legs:
        fixtures.setdefault(str(leg["leg_id"]).split("|", 1)[0], []).append(str(leg["leg_id"]))
    shared = tuple(leg_id for ids in fixtures.values() if len(ids) > 1 for leg_id in ids)
    unexamined = []
    for p in pillars:
        status, number = str(p.get("status", "")).upper(), int(p["number"])
        if status == "UNVERIFIED":
            unexamined.append({"number": number, "title": p.get("title"), "reason": p.get("reason")})
            continue
        if status not in ("FAIL", "ADVISORY"):
            continue
        code = PILLAR_DISEASE.get(number, DiseaseCode.PILLAR_VETO)
        severity = Severity.CRITICAL if status == "FAIL" else Severity.MILD
        reason = str(p.get("reason") or "no reason given").rstrip(".")
        text = f"Pillar {number}, {p.get('title')}: {reason}." if code is DiseaseCode.PILLAR_VETO else f"{reason}."
        found.append(Pathology(code, severity, text[0].upper() + text[1:], shared if code is DiseaseCode.CORRELATED_SYNDROME else (), number))
    if max_payout is not None and payout is not None and payout > max_payout:
        found.append(Pathology(DiseaseCode.BOOKMAKER_LIMIT_TRAP, Severity.CRITICAL,
                               f"At KUMBHA's stake the slip pays ₹{payout:,}, over the book's ₹{max_payout:,} maximum payout you recorded: the excess "
                               "would never be paid."))
    found.sort(key=lambda p: _SEVERITY_ORDER[p.severity])
    return found, unexamined


# ================================================================ the Lawyer
@dataclass(frozen=True, slots=True)
class Rule:
    code: str
    title: str
    applies_to: str  # when the Lawyer raises it
    summary: str  # the general practice
    hedge: str
    basis: str


RULES: dict[str, Rule] = {r.code: r for r in (
    Rule("REGULATION_TIME", "90 minutes plus stoppage time", "a football leg in a competition that can hold a knockout tie",
         "Standard football markets (match odds, totals, both teams to score, double chance, draw no bet, handicaps) settle on 90 minutes plus stoppage "
         "time. Extra time and penalties count only in markets that say so ('to qualify', 'to lift the trophy'): a side that goes through after "
         "extra time drew the 90 minutes.",
         "Backing a side to go through? Use the book's 'to qualify' market; this leg is settled on the 90 minutes.",
         "General settlement practice; the wording varies by book"),
    Rule("CRICKET_MINIMUM_OVERS", "Rain and the minimum overs", "a limited-overs cricket leg",
         "A rain-reduced limited-overs match needs a minimum number of overs a side for a result under the ICC playing conditions. With no result, match "
         "bets are commonly void, and a runs line set for a full innings is commonly void once overs are lost.",
         "Read the forecast before the toss; in a multiple a void leg counts at 1.00 and cuts the payout.",
         "ICC playing conditions; the book's cricket rules decide the settlement"),
    Rule("TENNIS_RETIREMENT", "A player retires", "a tennis leg",
         "Books differ when a player retires: some void the bet, others settle it once a set (or the first set) is complete, and the rule can differ by "
         "market.", "Read the book's retirement rule before backing a player with an injury doubt.",
         "General settlement practice; books differ"),
    Rule("LINE_PUSH", "Whole and quarter lines", "a totals or handicap leg on a whole or quarter line",
         "On a whole line the stake comes back when the result lands exactly on it (a push). A quarter line splits the stake over the two neighbouring "
         "lines, so half can win or push while the other half loses. In a multiple a pushed leg counts at 1.00.",
         "Prefer a half line (x.5) in a multiple when the price is close: it cannot push.", "Market mechanics"),
    Rule("VOID_LEG_IN_MULTIPLE", "A void leg in a multiple", "every slip of two or more legs",
         "When a leg is void (postponed, abandoned, or not played within the book's time limit) books commonly settle the rest of the multiple with that "
         "leg at 1.00: the slip stands at lower odds.",
         "Check kick-off times; a system bet (Trixie, Yankee) keeps paying on its other lines.", "General settlement practice; time limits vary by book"),
    Rule("PALPABLE_ERROR", "Obvious-error prices", "a leg priced far over BetDoc's fair value",
         "Every book reserves the right to void or re-price a bet struck at an obvious error. A price far over every other book is the kind it reaches for.",
         "Screenshot the price and confirm the selection and line before staking; expect a limit or a void.",
         "General settlement practice"),
    Rule("MAX_PAYOUT", "Maximum payout per slip", "every slip",
         "Books cap what one slip can pay; a long multiple can win more than the cap, and the excess is not paid.",
         "Split the stake over independent slips when the payout nears the cap.",
         "The maximum you recorded from the book's rules"),
)}


@dataclass(frozen=True, slots=True)
class LawyerPolicy:
    knockout_keywords: tuple[str, ...]
    cricket_formats: dict[str, tuple[str, ...]]
    cricket_min_overs: dict[str, int]
    palpable_edge: float
    references: dict[str, dict[str, str]]
    max_payout: dict[str, Decimal]

    @classmethod
    def from_settings(cls, s: Any) -> LawyerPolicy:
        return cls(tuple(k.casefold() for k in s.CONSULTANT_KNOCKOUT_KEYWORDS),
                   {fmt: tuple(k.casefold() for k in keys) for fmt, keys in s.CONSULTANT_CRICKET_FORMATS.items()}, dict(s.CONSULTANT_CRICKET_MIN_OVERS),
                   float(s.CONSULTANT_PALPABLE_EDGE),
                   {book.strip().casefold(): dict(refs) for book, refs in s.CONSULTANT_RULE_REFERENCES.items()},
                   {book.strip().casefold(): Decimal(str(cap)) for book, cap in s.CONSULTANT_MAX_PAYOUT_INR.items()})

    def cap_for(self, book: str | None) -> Decimal | None:
        return None if book is None else self.max_payout.get(book.casefold())

    def cricket_format(self, sport_key: str) -> str | None:
        key = sport_key.casefold()
        return next((fmt for fmt, keys in self.cricket_formats.items() if any(k in key for k in keys)), None)


@dataclass(frozen=True, slots=True)
class Clause:
    rule: Rule
    risk: RiskLevel
    legs: tuple[str, ...]
    finding: str  # what the rule means for this slip
    reference: str | None  # the clause the user recorded from the book's rules

    def as_dict(self, book: str | None) -> dict[str, Any]:
        r = self.rule
        return {"code": r.code, "title": r.title, "risk": self.risk.value, "legs": list(self.legs), "finding": self.finding, "summary": r.summary,
                "hedge": r.hedge, "basis": r.basis, "bookmaker": book, "reference": self.reference}


def counsel(legs: Sequence[Mapping[str, Any]], book: str | None, pillars: Sequence[Mapping[str, Any]], policy: LawyerPolicy, *,
            payout: Decimal | None) -> list[Clause]:
    """The settlement rules this slip falls under at ``book``, the riskiest first."""
    weather = next((str(p.get("status", "")).upper() for p in pillars if int(p["number"]) == 2), "UNVERIFIED")
    refs = policy.references.get((book or "").casefold(), {})
    out: list[Clause] = []

    def add(code: str, risk: RiskLevel, legs_: Sequence[str], finding: str) -> None:
        out.append(Clause(RULES[code], risk, tuple(legs_), finding, refs.get(code)))

    knockout = [leg for leg in legs if str(leg.get("sport_key") or "").startswith("soccer")
                and any(k in f"{leg.get('sport_key') or ''} {leg.get('league') or ''}".casefold() for k in policy.knockout_keywords)]
    if knockout:
        add("REGULATION_TIME", RiskLevel.MEDIUM, [str(leg["leg_id"]) for leg in knockout],
            f"{', '.join(str(leg.get('fixture')) for leg in knockout)} may be a knockout tie: these legs settle on the 90 minutes.")
    cricket: dict[str, list[Mapping[str, Any]]] = {}
    for leg in legs:
        key = str(leg.get("sport_key") or "")
        fmt = policy.cricket_format(key) if key.startswith("cricket") else None
        if fmt is not None:
            cricket.setdefault(fmt, []).append(leg)
    for fmt, group in cricket.items():
        overs = policy.cricket_min_overs.get(fmt)
        risk = {"FAIL": RiskLevel.HIGH, "PASS": RiskLevel.LOW}.get(weather, RiskLevel.MEDIUM)
        sky = {"FAIL": "the weather pillar failed for this slip", "PASS": "the weather pillar passed"}.get(weather, "no weather evidence was checked")
        need = f"at least {overs} overs a side for a result" if overs else "a minimum number of overs a side for a result"
        add("CRICKET_MINIMUM_OVERS", risk, [str(leg["leg_id"]) for leg in group], f"A rain-reduced {fmt} needs {need}; {sky}.")
    tennis = [str(leg["leg_id"]) for leg in legs if str(leg.get("sport_key") or "").startswith("tennis")]
    if tennis:
        add("TENNIS_RETIREMENT", RiskLevel.MEDIUM, tennis, f"{len(tennis)} tennis leg{'s' if len(tennis) > 1 else ''}: a retirement may void or settle it.")
    pushes = []
    for leg in legs:
        ref = parse_market(str(leg.get("market") or ""))
        frac = None if ref is None or ref.line is None or ref.kind not in (MarketKind.TOTALS, MarketKind.ASIAN_HANDICAP) else ref.line % 1
        if frac is not None and frac != 0.5:  # a half line (x.5) cannot push
            pushes.append((str(leg["leg_id"]), str(leg.get("label")), "whole" if frac == 0 else "quarter"))
    if pushes:
        add("LINE_PUSH", RiskLevel.LOW, [p[0] for p in pushes], "; ".join(f"{label}: a {kind} line" for _, label, kind in pushes) + ".")
    if len(legs) > 1:
        add("VOID_LEG_IN_MULTIPLE", RiskLevel.LOW, [str(leg["leg_id"]) for leg in legs], f"{len(legs)} legs: a void one counts at 1.00 and the rest stands.")
    for leg in legs:
        ev, fair, price = leg.get("ev"), leg.get("fair_probability"), leg_price(leg, book)
        if ev is not None and ev >= policy.palpable_edge and fair:
            add("PALPABLE_ERROR", RiskLevel.HIGH, [str(leg["leg_id"])],
                f"{leg.get('label')} pays {price if price is not None else '—'} against BetDoc's fair {1 / fair:.2f} ({ev:+.0%} EV): far enough over the "
                "market for the book to call it an error.")
    cap, name = policy.cap_for(book), ASHOKA_BOOKMAKERS.get(book or "", book or "this book")
    if cap is None:
        add("MAX_PAYOUT", RiskLevel.LOW, [], f"No maximum payout is recorded for {name}: read its maximum-winnings rule before a long multiple.")
    elif payout is not None and payout > cap:
        add("MAX_PAYOUT", RiskLevel.HIGH, [], f"₹{payout:,} at KUMBHA's stake is over the ₹{cap:,} you recorded for {name}.")
    else:
        add("MAX_PAYOUT", RiskLevel.LOW, [], f"₹{payout or 0:,} at KUMBHA's stake is inside the ₹{cap:,} you recorded for {name}.")
    out.sort(key=lambda c: _RISK_ORDER[c.risk])
    return out


# ================================================================ the Consultant (capital)
def capital(*, bankroll: Decimal | None, stake: Decimal, odds: Decimal | None, kelly_fraction: float, sizing: Mapping[str, Any], kelly_multiplier: float,
            simulation: Mapping[str, Any], stop_policy: slm.StopLossPolicy, tds_rate: Decimal, inr_per_usd: Decimal | None,
            usd_as_of: str | None, max_payout: Decimal | None, sizing_reason: str | None = None) -> dict[str, Any]:
    """The bankroll's view of the slip at KUMBHA's stake: payout, tax, stop-loss floor, downside, and what the client
    needs to recompute them for another stake (the same formulas, linear in the stake). ``sizing_reason``: pillar 13's
    own account of the stake (why it is what it is, or why there is none)."""
    payout = _money(stake * odds) if odds is not None else None
    tax = section_115bbj(payout, stake, tds_rate) if payout is not None else None
    p = float(simulation.get("joint_probability") or 0.0)
    stop_pct = slm.recommended_pct(p, stop_policy)
    usd = None if inr_per_usd is None or inr_per_usd <= 0 else _money(stake / inr_per_usd)
    joint_ev = simulation.get("joint_ev")
    return {
        "bankroll_inr": None if bankroll is None else str(bankroll),
        "stake_inr": str(stake), "stake_usd": None if usd is None else str(usd),
        "stake_pct_of_bankroll": None if not bankroll else round(float(stake / bankroll), 6),
        "kelly_multiplier": kelly_multiplier, "kelly_fraction": round(kelly_fraction, 6), "full_kelly": sizing.get("full_kelly"), "sizing_reason": sizing_reason,
        "regime": sizing.get("regime"), "drawdown": sizing.get("drawdown"), "capped": sizing.get("capped"),
        "odds": None if odds is None else str(odds),
        "payout_inr": None if payout is None else str(payout),
        "tax": None if tax is None else {**tax.as_dict(), "basis": "Section 115BBJ (30% on net winnings) / 194BA TDS; 4% cess when the return is filed"},
        "expected_profit_inr": None if joint_ev is None or stake <= 0 else str(_money(stake * Decimal(str(joint_ev)))),
        "stop_loss": {"pct": stop_pct, "floor_inr": str(slm.floor(stake, stop_pct)), "min_pct": stop_policy.min_pct, "max_pct": stop_policy.max_pct,
                      "basis": "the in-play shield's stop: wider for a long shot, tighter for a near-certainty"},
        "downside": {key: simulation.get(key) for key in ("joint_probability", "p_total_loss", "var_95", "cvar_95", "std_return")},
        "max_payout_inr": None if max_payout is None else str(max_payout),
        "payout_over_cap": None if max_payout is None or payout is None else payout > max_payout,
        "fx": {"inr_per_usd": None if inr_per_usd is None else str(inr_per_usd), "as_of": usd_as_of},
        "what_if": {"tds_rate": float(tds_rate), "cess_rate": float(CESS_RATE), "stop_loss_pct": stop_pct},
    }


# ================================================================ TipMaster (point b.13b)
@dataclass(frozen=True, slots=True)
class LegConsensus:
    leg_id: str
    label: str
    tickets_pct: float | None
    money_pct: float | None
    source: str | None
    price: float | None
    fair_probability: float | None
    evaluation: lcm.ConsensusEvaluation | None
    divergence: Divergence

    def as_dict(self) -> dict[str, Any]:
        e = self.evaluation
        return {"leg_id": self.leg_id, "label": self.label, "tickets_pct": self.tickets_pct, "money_pct": self.money_pct, "source": self.source,
                "price": self.price, "fair_probability": self.fair_probability, "implied_probability": None if not self.price else round(1 / self.price, 4),
                "ev_pct": None if e is None else float(e.betdoc_ev_pct), "divergence_pct": None if e is None or e.divergence_pct is None else float(e.divergence_pct),
                "rlm": None if e is None else e.rlm_detected, "edge_score": None if e is None else float(e.contrarian_edge_score), "verdict": self.divergence.value}


_KARNA = {"PUBLIC_TRAP": Divergence.PUBLIC_TRAP, "SHARP_CONTRARIAN": Divergence.CONTRARIAN_ALPHA, "SHARP_CONSENSUS": Divergence.SHARP_CONSENSUS}
_DIVERGENCE_ORDER = (Divergence.PUBLIC_TRAP, Divergence.CONTRARIAN_ALPHA, Divergence.SHARP_CONSENSUS, Divergence.NEUTRAL_ALIGNED, Divergence.NOT_MEASURED)
DIVERGENCE_LABELS = {
    Divergence.PUBLIC_TRAP: "Public trap", Divergence.CONTRARIAN_ALPHA: "Contrarian alpha", Divergence.SHARP_CONSENSUS: "Sharp consensus",
    Divergence.NEUTRAL_ALIGNED: "Neutral, aligned", Divergence.NOT_MEASURED: "No public share measured",
}


def leg_consensus(leg: Mapping[str, Any], book: str | None, *, tickets: float | None, money: float | None, opening: float | None, source: str | None,
                  margin: float | None, policy: lcm.ConsensusPolicy) -> LegConsensus:
    """One leg's crowd against BetDoc, by KARNA's rules; unmeasured shares fire no rule."""
    price, fair = leg_price(leg, book), leg.get("fair_probability")
    evaluation = None
    if price is not None and fair is not None:
        evaluation = lcm.evaluate_consensus_signal(public_ticket_pct=tickets, money_pct=money, opening_odds=opening, best_odds=price,
                                                   cheapest_margin_pct=None if margin is None else margin * 100.0, betdoc_true_prob=float(fair), policy=policy)
    if tickets is None:
        divergence = Divergence.NOT_MEASURED
    else:
        divergence = _KARNA.get(evaluation.verdict, Divergence.NEUTRAL_ALIGNED) if evaluation is not None else Divergence.NEUTRAL_ALIGNED
    return LegConsensus(str(leg["leg_id"]), str(leg.get("label")), tickets, money, source, price, None if fair is None else float(fair), evaluation, divergence)


def slip_consensus(rows: Sequence[LegConsensus]) -> dict[str, Any]:
    """The slip's crowd verdict: its most telling leg (a trap first), with the plain-English reading."""
    verdict = min((r.divergence for r in rows), key=_DIVERGENCE_ORDER.index, default=Divergence.NOT_MEASURED)
    lead = next((r for r in rows if r.divergence is verdict), None)
    measured = sum(r.tickets_pct is not None for r in rows)
    ev = None if lead is None or lead.evaluation is None else float(lead.evaluation.betdoc_ev_pct)
    if verdict is Divergence.NOT_MEASURED or lead is None:
        text = ("No public ticket or money share is measured for these selections. BetDoc compares only measured shares: a named feed's or an "
                "administrator's splits in the twin's evidence store, or BetDoc's own recorded bets. Nothing is estimated.")
    elif verdict is Divergence.PUBLIC_TRAP:
        text = f"{lead.label}: {lead.tickets_pct:.0f}% of tickets ({lead.source}) on a side worth {ev:+.1f}% at this price. The crowd is paying the vig."
    elif verdict is Divergence.CONTRARIAN_ALPHA:
        money = f", while {lead.money_pct:.0f}% of the money is on it" if lead.money_pct is not None else ""
        text = f"{lead.label}: only {lead.tickets_pct:.0f}% of tickets ({lead.source}){money}, and BetDoc prices it at {ev:+.1f}% EV."
    elif verdict is Divergence.SHARP_CONSENSUS:
        text = f"{lead.label}: the money ({lead.money_pct:.0f}%, {lead.source}) and BetDoc's {ev:+.1f}% EV agree."
    else:
        text = f"The measured shares ({measured} of {len(rows)} legs) raise no trap, contrarian or consensus signal against BetDoc's prices."
    return {"verdict": verdict.value, "label": DIVERGENCE_LABELS[verdict], "explanation": text, "measured_legs": measured, "legs": [r.as_dict() for r in rows]}


# ================================================================ the verdict and its seal
@dataclass(frozen=True, slots=True)
class CureView:
    """The Doctor's prescription as the verdict reads it."""

    replaced_label: str
    replacement_label: str
    score_after: float
    tier_after: str
    clean: bool  # the cured slip fails no pillar and keeps positive joint EV


def verdict(pathologies: Sequence[Pathology], *, is_vetted: bool, score: float, tier: str, gold_floor: float, joint_ev: float | None,
            stake: Decimal, cure: CureView | None, unexamined: int) -> tuple[VerdictStatus, str, str]:
    """(status, title, summary). Gold: the fortress vetted it, no critical pathology, a positive stake and joint EV, and
    a Slip Score at the gold line or over. With a cure that clears every failing pillar: approved with the prescription.
    Anything else is rejected to protect the capital. Approval says how much was checked, never that it cannot lose."""
    critical = [p for p in pathologies if p.severity is Severity.CRITICAL]
    if is_vetted and not critical and stake > 0 and joint_ev is not None and joint_ev > 0 and score >= gold_floor:
        return (VerdictStatus.APPROVED_GOLD, "APPROVED — GOLD GRADE",
                f"Every enforced pillar passed, joint EV {joint_ev:+.1%}, Slip Score {score:.0f} ({tier}). Approval says how much was checked, not that "
                "it cannot lose.")
    if cure is not None and cure.clean:
        return (VerdictStatus.APPROVED_WITH_CURE, "APPROVED WITH PRESCRIPTION",
                f"As built it scores {score:.0f} ({tier}). Swapping {cure.replaced_label} for {cure.replacement_label} lifts it to {cure.score_after:.0f} "
                f"({cure.tier_after}) with no failing pillar: apply the prescription before staking.")
    reasons = [DISEASE_TITLES[p.code].lower() for p in critical]
    if joint_ev is not None and joint_ev <= 0:
        reasons.append(f"joint EV {joint_ev:+.1%}")
    if stake <= 0:
        reasons.append("no stake sized (no bankroll, a drawdown halt, or a stake under the step)")
    if not reasons and unexamined:
        reasons.append(f"{unexamined} pillar{'s' if unexamined > 1 else ''} without evidence")
    if not reasons:
        reasons.append(f"a Slip Score of {score:.0f} ({tier}), under the gold line")
    return VerdictStatus.REJECTED, "REJECTED — CAPITAL PROTECTION", f"Do not stake it as built: {'; '.join(dict.fromkeys(reasons))}."


SEAL_CONTEXT = b"betdoc:consultant:verdict-seal:v1"


def _numbers(value: Any) -> Any:
    """One spelling per number: a whole float is its integer (PostgreSQL's jsonb keeps neither -0.0 nor 1e16 as written)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {k: _numbers(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_numbers(v) for v in value]
    return value


def canonical(body: Mapping[str, Any]) -> bytes:
    return json.dumps(_numbers(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _key(secret: str) -> bytes:
    """A key for sealing verdicts only, derived from the server's secret (never the secret itself)."""
    return hmac.new(secret.encode(), SEAL_CONTEXT, hashlib.sha256).digest()


def seal(body: Mapping[str, Any], secret: str) -> str:
    return hmac.new(_key(secret), canonical(body), hashlib.sha256).hexdigest()


def seal_holds(body: Mapping[str, Any], secret: str, mark: str) -> bool:
    return hmac.compare_digest(seal(body, secret), mark)
