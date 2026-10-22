"""Which of BetDoc's quantitative components the live smallcases use, and how (Group 107, point b.8). Developed for
Amit Ashok Kumar Patnaik.

The catalogue is the code's own: the model registry (``app.services.hive_registry.CATALOGUE``: every math model, risk
model and bet type, each naming the module that implements it), the fortress's 15 pillars
(``app.domain.oracle.fortress.PILLAR_KEYS``) and the stop-loss engine's five mechanisms
(``app.domain.risk.stop_loss.StopLossEngine``). Every implementation is imported to prove it exists.

How the smallcases use a component is stated only where the code does it:

* ``BASKET``: called on every basket (sizing and risk: ``app.domain.smallcase.portfolio``);
* ``HOLDING``: a bet type a basket holds right now (the market kinds of its legs), or the structure it is built as;
* ``INSPECTION``: run on a basket's legs once loaded into the workbench or the Consultant (the 15 pillars);
* ``ACCOUNT``: guards the account every placed leg is recorded against (the stop-loss engine, when enabled);
* anything else is implemented and catalogued, and the smallcases do not call it. Nothing is claimed for it.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

from app.domain.oracle import fortress
from app.services.hive_registry import CATALOGUE, EXPECTED

KINDS = ("BET_TYPE", "MATH_MODEL", "RISK_MODEL", "STOP_LOSS", "FORTRESS_PILLAR")
BLUEPRINT = {**{k.value: v for k, v in EXPECTED.items()}, "STOP_LOSS": 5, "FORTRESS_PILLAR": 15}

# registry key -> what every basket does with it
BASKET_USES: dict[str, str] = {
    "risk.kelly_portfolio": "each leg's independent full-Kelly fraction, before KUMBHA's policy",
    "risk.var": "the basket's 95% historical VaR over the simulated paths",
    "risk.cvar": "the basket's 95% CVaR (expected shortfall) over the simulated paths",
    "risk.concentration": "the HHI concentration of the basket's stakes",
    "risk.risk_parity": "the equal-risk-contribution weights, shown beside KUMBHA's",
    "risk.component_var": "each leg's share of the basket's 95% VaR",
}
STRUCTURES: dict[str, str] = {
    "bet.single": "every holding is a single, staked on its own",
    "bet.parlay": "the same legs as one multiple, priced for comparison",
}
# a held leg's market kind -> the bet-type component it is
MARKET_KINDS: dict[str, str] = {
    "MATCH_ODDS": "bet.match_winner_1x2", "TOTALS": "bet.over_under_goals", "ASIAN_HANDICAP": "bet.asian_handicap", "BTTS": "bet.btts",
    "DOUBLE_CHANCE": "bet.double_chance", "DRAW_NO_BET": "bet.draw_no_bet",
}
STOP_LOSS: tuple[tuple[str, str, str, str], ...] = (
    ("stop.daily_loss", "Daily loss limit", "A day's net losses reach the configured limit: betting stops until the next day.", "TRIGGER_DAILY_LOSS"),
    ("stop.consecutive_losses", "Consecutive-loss breaker", "N losses in a row lock betting out until the session resets.", "TRIGGER_CONSECUTIVE"),
    ("stop.trailing", "Trailing stop", "The session's equity falls the configured share from its peak.", "TRIGGER_TRAILING"),
    ("stop.session_loss", "Session loss limit", "The session's net losses reach the configured limit.", "TRIGGER_SESSION_LOSS"),
    ("stop.cooldown", "Cool-down lock", "Any trigger starts a cool-down during which nothing is placed.", "StopLossEngine"),
)


@dataclass(frozen=True, slots=True)
class Entry:
    key: str
    kind: str
    name: str
    description: str
    implementation: str

    def as_dict(self, importable: bool, use: tuple[str, str] | None) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "name": self.name, "description": self.description, "implementation": self.implementation,
                "implemented": importable, "used": None if use is None else {"scope": use[0], "role": use[1]}}


def entries() -> tuple[Entry, ...]:
    registry = tuple(Entry(c.key, c.kind.value, c.name, c.description, c.implementation or "") for c in CATALOGUE)
    stops = tuple(Entry(key, "STOP_LOSS", name, desc, f"app.domain.risk.stop_loss:{attr}") for key, name, desc, attr in STOP_LOSS)
    pillars = tuple(Entry(f"pillar.{n}", "FORTRESS_PILLAR", f"Pillar {n}: {fortress.PILLAR_TITLES[n]}", f"The fortress's pillar {n} ({fortress.PILLAR_KEYS[n]}).",
                          f"app.domain.oracle.fortress:pillar_{n}") for n in sorted(fortress.PILLAR_KEYS))
    return (*registry, *stops, *pillars)


@cache
def importable(reference: str) -> bool:
    """``module:attribute.path`` resolves to a real object."""
    module, _, attribute = reference.partition(":")
    if not module or not attribute:
        return False
    try:
        obj: Any = importlib.import_module(module)
        for part in attribute.split("."):
            obj = getattr(obj, part)
    except (ImportError, AttributeError):
        return False
    return True


def coverage(held_market_kinds: Iterable[str], baskets_live: int) -> dict[str, Any]:
    """Every component, implemented or not, and how the smallcases use it, with the counts per kind."""
    kinds_held = {MARKET_KINDS[k] for k in held_market_kinds if k in MARKET_KINDS}
    rows = []
    for entry in entries():
        use: tuple[str, str] | None = None
        if entry.key in BASKET_USES:
            use = ("BASKET", BASKET_USES[entry.key])
        elif entry.key in STRUCTURES:
            use = ("HOLDING", STRUCTURES[entry.key])
        elif entry.key in kinds_held:
            use = ("HOLDING", "a market the live baskets hold right now")
        elif entry.kind == "FORTRESS_PILLAR":
            use = ("INSPECTION", "run on each leg once a basket is loaded into the workbench or the Consultant")
        elif entry.kind == "STOP_LOSS":
            use = ("ACCOUNT", "guards the account each placed leg is recorded against, when the stop-loss engine is enabled")
        rows.append(entry.as_dict(importable(entry.implementation), use))
    by_kind: dict[str, dict[str, int]] = {}
    for kind in KINDS:
        mine = [r for r in rows if r["kind"] == kind]
        by_kind[kind] = {"catalogued": len(mine), "implemented": sum(r["implemented"] for r in mine), "blueprint": BLUEPRINT[kind],
                         **{scope: sum(1 for r in mine if r["used"] and r["used"]["scope"] == scope) for scope in ("BASKET", "HOLDING", "INSPECTION", "ACCOUNT")}}
    totals = {key: sum(v[key] for v in by_kind.values()) for key in ("catalogued", "implemented", "blueprint", "BASKET", "HOLDING", "INSPECTION", "ACCOUNT")}
    return {"components": rows, "by_kind": by_kind, "totals": {**totals, "used": sum(1 for r in rows if r["used"])}, "baskets_live": baskets_live,
            "note": "Implemented means the code that implements it was imported. Used means the smallcases call it, hold it, or run it on inspection "
                    "or at account level; every other component is real and catalogued, and the smallcases do not call it."}


def held_kinds(baskets: Iterable[Mapping[str, Any]]) -> set[str]:
    return {leg["market_kind"] for b in baskets for leg in b.get("holdings", [])}
