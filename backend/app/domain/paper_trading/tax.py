"""Indian Section 115BBJ tax on winnings from online games and wagering (FA-6, Group 80). Developed for Amit Ashok Kumar Patnaik.

Section 115BBJ (Finance Act 2023) taxes net winnings from online games at a flat 30%, with no deduction for
expenses and no set-off of losses on other bets; Section 194BA has the platform withhold that 30% as TDS. Per bet:

    Net Winnings          = max(0, Gross Payout - Stake)
    TDS                   = 0.30 x Net Winnings
    Net Post-Tax Payout   = Gross Payout - TDS

The 4% health and education cess is levied on the tax itself when the return is filed (30% x 1.04 = 31.2%); it is
reported alongside as ``cess`` and ``effective_rate_pct`` but is not part of the TDS withheld. A financial year runs
from 1 April to 31 March, Indian Standard Time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
TDS_RATE = Decimal("0.30")
CESS_RATE = Decimal("0.04")
PAISA = Decimal("0.01")
ZERO = Decimal(0)


def money(value: Decimal) -> Decimal:
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class TdsBreakdown:
    gross_payout: Decimal
    stake: Decimal
    net_winnings: Decimal
    tds_rate: Decimal
    tds: Decimal
    net_post_tax_payout: Decimal
    cess: Decimal

    @property
    def effective_rate_pct(self) -> float:
        return float(self.tds_rate * (1 + CESS_RATE) * 100)

    def as_dict(self) -> dict[str, float]:
        return {"gross_payout": float(self.gross_payout), "stake": float(self.stake), "net_winnings": float(self.net_winnings),
                "tds_rate_pct": float(self.tds_rate * 100), "tds": float(self.tds), "net_post_tax_payout": float(self.net_post_tax_payout),
                "cess": float(self.cess), "effective_rate_pct": round(self.effective_rate_pct, 4)}


def section_115bbj(gross_payout: Decimal, stake: Decimal, rate: Decimal = TDS_RATE) -> TdsBreakdown:
    if gross_payout < 0 or stake < 0:
        raise ValueError("payout and stake cannot be negative")
    if not ZERO <= rate < 1:
        raise ValueError("the TDS rate is a fraction in [0, 1)")
    net = money(max(ZERO, gross_payout - stake))
    tds = money(net * rate)
    return TdsBreakdown(money(gross_payout), money(stake), net, rate, tds, money(gross_payout - tds), money(tds * CESS_RATE))


def financial_year(at: datetime) -> str:
    """The Indian financial year (April-March, IST) of a moment: 2026-10-11 -> "2026-27"."""
    local = (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(IST)
    start = local.year if local.month >= 4 else local.year - 1
    return f"{start}-{(start + 1) % 100:02d}"
