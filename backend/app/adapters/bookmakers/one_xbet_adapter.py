"""1xBet's cashout path for the in-play stop-loss shield (Group 77): manual (no 1xBet cashout API; BetDoc never drives its site)."""

from __future__ import annotations

from app.adapters.bookmakers.base_cashout_adapter import CashoutAdapter, register


class OneXBetCashoutAdapter(CashoutAdapter):
    bookmaker = "1XBET"  # PlacedBookmaker value
    label = "1xBet"


ADAPTER = register(OneXBetCashoutAdapter())
