"""Parimatch's cashout path for the in-play stop-loss shield (Group 77): manual (no Parimatch cashout API; BetDoc never drives its site)."""

from __future__ import annotations

from app.adapters.bookmakers.base_cashout_adapter import CashoutAdapter, register


class ParimatchCashoutAdapter(CashoutAdapter):
    bookmaker = "PARIMATCH"  # PlacedBookmaker value
    label = "Parimatch"


ADAPTER = register(ParimatchCashoutAdapter())
