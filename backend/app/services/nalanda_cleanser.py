"""Ghost-spike cleansing for the tick firehose.

A ghost spike is a price that jumps absurdly (2.0 -> 500.0) and comes straight back: a feed glitch,
a fat-fingered quote, a decimal in the wrong place. Kept, it poisons every rolling statistic and
candle downstream; dropped on a z-score alone, it would also drop genuine moves.

Per cell (fixture, market, selection, source, bookmaker), the cleanser keeps the last
``window`` accepted prices in log space. A new price is a *suspect* when, against them, its z-score
(``(x - mean) / max(std, std_floor)``) is at least ``z_threshold`` and it is at least ``min_jump`` away
in log terms from the last accepted price. A suspect is not decided at once: it waits up to
``hold_seconds`` (by the ticks' own clock) for the next price of its cell.

* The price comes back to within ``z_threshold`` of the window: every held tick was a ghost spike,
  flagged ``is_anomaly`` (stored for forensics, kept out of the window and of every candle).
* It stays out there past ``hold_seconds``: the market really moved; the held ticks are released as
  ordinary prices and the window learns them. (Agreement alone never releases early: a glitch that
  repeats for two quotes and then reverts is still a ghost spike.)

Suspended quotes pass straight through and never touch the window. Deterministic and pure: the
firehose feeds it ticks in stream order and releases what it returns.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import fmean, pstdev

Cell = tuple[str, str, str, str, str]  # fixture, market, selection, source, bookmaker


@dataclass(frozen=True, slots=True)
class Tick:
    fixture_id: str
    market: str
    selection: str
    source: str
    bookmaker_id: str
    odds: Decimal
    observed_at: datetime
    is_suspended: bool = False
    stream_id: str = ""
    is_anomaly: bool = False
    anomaly_z: float | None = None

    @property
    def cell(self) -> Cell:
        return (self.fixture_id, self.market, self.selection, self.source, self.bookmaker_id)


@dataclass(slots=True)
class _CellState:
    window: deque[float]
    held: list[Tick] = field(default_factory=list)


class GhostSpikeFilter:
    def __init__(self, *, window: int = 20, z_threshold: float = 4.0, min_jump: float = 0.25, hold_seconds: float = 3.0, min_points: int = 5, std_floor: float = 0.02) -> None:
        if window < min_points or min_points < 2:
            raise ValueError("the window must hold at least min_points (>= 2) prices")
        self.window, self.z_threshold, self.min_jump = window, z_threshold, min_jump
        self.hold = timedelta(seconds=hold_seconds)
        self.min_points, self.std_floor = min_points, std_floor
        self._cells: dict[Cell, _CellState] = {}
        self.flagged = 0

    def _state(self, cell: Cell) -> _CellState:
        state = self._cells.get(cell)
        if state is None:
            state = self._cells[cell] = _CellState(deque(maxlen=self.window))
        return state

    def _z(self, state: _CellState, x: float) -> float | None:
        if len(state.window) < self.min_points:
            return None
        sd = max(pstdev(state.window), self.std_floor)
        return (x - fmean(state.window)) / sd

    def push(self, tick: Tick) -> list[Tick]:
        """Feed one tick; returns the ticks now decided (in order), possibly including earlier held ones."""
        if tick.is_suspended or tick.odds <= 1:
            return [tick]
        state = self._state(tick.cell)
        x = math.log(float(tick.odds))
        out: list[Tick] = []
        if state.held and tick.observed_at - state.held[0].observed_at > self.hold:
            out += self._release(state)  # the hold ran out before this tick: those were real
        z = self._z(state, x)
        if state.held:
            if z is not None and abs(z) < self.z_threshold:
                out += self._flag(state)  # back to normal within the hold: a ghost spike
                state.window.append(x)
                out.append(replace(tick, anomaly_z=round(z, 4)))
                return out
            state.held.append(replace(tick, anomaly_z=None if z is None else round(z, 4)))
            return out
        last = state.window[-1] if state.window else None
        if z is not None and abs(z) >= self.z_threshold and last is not None and abs(x - last) >= self.min_jump:
            state.held.append(replace(tick, anomaly_z=round(z, 4)))
            return out
        state.window.append(x)
        out.append(replace(tick, anomaly_z=None if z is None else round(z, 4)))
        return out

    def flush(self, now: datetime) -> list[Tick]:
        """Release every suspect whose hold has run out by ``now`` (the stream went quiet on its cell)."""
        out: list[Tick] = []
        for state in self._cells.values():
            if state.held and now - state.held[0].observed_at > self.hold:
                out += self._release(state)
        return out

    def drain(self) -> Iterator[Tick]:
        """Everything still held, as ordinary prices (shutdown: nothing is ever dropped)."""
        for state in self._cells.values():
            yield from self._release(state)

    def _release(self, state: _CellState) -> list[Tick]:
        released = state.held
        state.held = []
        for t in released:
            state.window.append(math.log(float(t.odds)))
        return released

    def _flag(self, state: _CellState) -> list[Tick]:
        flagged = [replace(t, is_anomaly=True) for t in state.held]
        state.held = []
        self.flagged += len(flagged)
        return flagged

    @property
    def holding(self) -> int:
        return sum(len(s.held) for s in self._cells.values())
