"""One chain of the neural event bus (Group 108, point b.9): a Wire item handed from module to module.

    VIDUR / THE WIRE -> SCORE PREDICTOR -> ORACLE & BET BUILDER -> BETDOC CONSULTANT -> THE VAULT & CAPITAL LEDGER

Each step is a real engine call on the item's fixture (``app.services.neural_chain``) and ends COMPLETED (it ran
and says what it found), SKIPPED (it had nothing to run on, and says why) or FAILED (the engine refused or raised,
and says how). A step never borrows a figure from another: what it reports is what it computed. Frames go out as
each step starts (RUNNING) and ends, so the pipeline bar shows the handoff as it happens.
Developed for Amit Ashok Kumar Patnaik.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Module(StrEnum):
    WIRE = "WIRE"
    SCORE_PREDICTOR = "SCORE_PREDICTOR"
    ORACLE = "ORACLE"
    CONSULTANT = "CONSULTANT"
    VAULT = "VAULT"


class Status(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


# (module, its name on the bar, what it does in the chain), in chain order
MODULES: tuple[tuple[Module, str, str], ...] = (
    (Module.WIRE, "VIDUR / THE WIRE", "reads the item: its source, tier and fixture"),
    (Module.SCORE_PREDICTOR, "SCORE PREDICTOR", "re-fits the fixture's scorelines to the market now and to the recorded absences"),
    (Module.ORACLE, "ORACLE & BET BUILDER", "re-reads the live market fresh: the fixture's legs priced above fair at a retail book"),
    (Module.CONSULTANT, "BETDOC CONSULTANT", "re-scores your slip (the Slip Score) when it holds a leg on the fixture"),
    (Module.VAULT, "THE VAULT & CAPITAL LEDGER", "the portfolios holding the fixture now, KUMBHA's stakes and stop-loss floors"),
)
MODULE_NAME: dict[Module, str] = {m: name for m, name, _ in MODULES}


@dataclass(slots=True)
class Step:
    number: int
    module: Module
    status: Status
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str | None = None
    ms: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "module": self.module.value, "status": self.status.value, "module_name": MODULE_NAME[self.module]}


@dataclass(slots=True)
class Chain:
    chain_id: str
    audience: str  # "system" (started by the Wire's scan) or the user's id (started from the board)
    trigger: dict[str, Any]
    started_at: str
    steps: list[Step] = field(default_factory=list)
    completed_at: str | None = None

    @property
    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in (Status.COMPLETED, Status.SKIPPED, Status.FAILED)}
        for step in self.steps:
            if step.status.value in out:
                out[step.status.value] += 1
        return out

    def outcome(self) -> str:
        c = self.counts
        parts = [f"{c[k]} {k.lower()}" for k in ("COMPLETED", "SKIPPED", "FAILED") if c[k]]
        return f"{len(self.steps)} step{'' if len(self.steps) == 1 else 's'}: " + ", ".join(parts) if parts else "no step ran"

    def as_dict(self, *, viewer: str | None = None) -> dict[str, Any]:
        return {"chain_id": self.chain_id, "origin": "SYSTEM" if self.audience == "system" else "USER", "mine": viewer is not None and viewer == self.audience,
                "trigger": self.trigger, "started_at": self.started_at, "completed_at": self.completed_at, "steps": [s.as_dict() for s in self.steps],
                "counts": self.counts, "outcome": self.outcome()}


def slip_signature(leg_ids: Sequence[str], skin: str) -> str:
    """The same legs at the same skin, in any order, are the same slip."""
    return hashlib.sha256(("|".join(sorted(set(leg_ids))) + "#" + skin).encode()).hexdigest()[:32]
