"""Request bodies of KUMBHA's capital growth API (Group 76)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunSimulationRequest(_Body):
    strategy: str = Field(default="QUARTER_KELLY", pattern=r"^[A-Z][A-Z0-9_]{2,31}$")
    horizon_days: int = Field(default=90, ge=1, le=3650)  # one of CFO_SIMULATION_HORIZONS
    paths: int | None = Field(default=None, ge=1000, le=25000)  # None: CFO_MONTE_CARLO_PATHS


class AcknowledgeRequest(_Body):
    note: str | None = Field(default=None, max_length=512)  # a halt's sign-off needs one (5 characters or more)


class TransferStatusRequest(_Body):
    status: Literal["APPROVED", "EXECUTED", "DISMISSED"]
    note: str | None = Field(default=None, max_length=512)
