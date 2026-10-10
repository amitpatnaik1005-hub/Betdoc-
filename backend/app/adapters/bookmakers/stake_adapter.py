"""Stake's cashout path for the in-play stop-loss shield (Group 77): manual (no Stake cashout API; BetDoc never drives its site)."""

from __future__ import annotations

from app.adapters.bookmakers.base_cashout_adapter import CashoutAdapter, register


class StakeCashoutAdapter(CashoutAdapter):
    bookmaker = "STAKE"  # PlacedBookmaker value
    label = "Stake"


ADAPTER = register(StakeCashoutAdapter())
