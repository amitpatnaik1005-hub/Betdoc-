"""The Bet-Slip ticket as an HTML card for email (Group 106, point b.12). Developed for Amit Ashok Kumar Patnaik.

Mail clients ignore stylesheets and flexbox, so the card is nested tables with inline styles: ivory paper (#FDFCF7),
gold rules (#C89B3C), emerald for the money that comes back (#059669), the book's own badge colours. Every value is
escaped; the figures are the ticket's, which are the signed report's.
"""

from __future__ import annotations

from html import escape

from app.domain.oracle.slip_formatter import inr
from app.domain.slip_ornament.ticket import Ticket

IVORY, GOLD, EMERALD, INK, MUTED, RULE, ROSE = "#FDFCF7", "#C89B3C", "#059669", "#1C1917", "#78716C", "#E7E5E4", "#BE123C"


def _e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def _figure(label: str, value: str, colour: str = INK) -> str:
    return (f'<td style="padding:10px 12px;vertical-align:top;"><div style="font-size:10px;letter-spacing:1px;text-transform:uppercase;color:{MUTED};">{_e(label)}</div>'
            f'<div style="font-family:Consolas,Menlo,monospace;font-size:17px;font-weight:bold;color:{colour};">{_e(value)}</div></td>')


def email_html(ticket: Ticket) -> str:
    fg, bg = ticket.badge
    rows = "".join(
        f'<tr style="border-top:1px solid {RULE};">'
        f'<td style="padding:10px 12px;color:{MUTED};font-family:Consolas,Menlo,monospace;vertical-align:top;">{leg.number}</td>'
        f'<td style="padding:10px 12px;vertical-align:top;"><div style="font-weight:bold;color:{INK};">{_e(leg.fixture)}</div>'
        f'<div style="font-size:11px;color:{MUTED};">{_e(" · ".join(p for p in (leg.sport, leg.league, leg.kickoff) if p))}</div>'
        f'<div style="margin-top:4px;color:{EMERALD};font-weight:bold;">{_e(leg.market)} <span style="color:{MUTED};font-weight:normal;">[{_e(leg.code)}]</span></div></td>'
        f'<td style="padding:10px 12px;text-align:right;vertical-align:top;font-family:Consolas,Menlo,monospace;font-size:16px;font-weight:bold;color:{INK};">'
        f'{_e(leg.price if leg.price is not None else "—")}</td></tr>'
        for leg in ticket.legs
    )
    usd = f" (${ticket.stake_usd})" if ticket.stake_usd is not None else ""
    stale = ("" if ticket.fresh else
             f'<tr><td colspan="2" style="padding:10px 20px;background:#FFF7ED;color:#9A3412;font-size:12px;">Prices as signed {ticket.age_minutes} minutes ago: '
             f're-check them at {_e(ticket.book_label)} before placing.</td></tr>')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>BetDoc Bet-Slip {_e(ticket.ticket_no)}</title></head>
<body style="margin:0;padding:24px;background:#F5F5F4;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;margin:0 auto;background:{IVORY};border:2px solid {GOLD};border-radius:16px;border-collapse:separate;overflow:hidden;">
<tr><td colspan="2" style="padding:8px 20px;background:rgba(200,155,60,0.10);border-bottom:2px dashed {GOLD};font-family:Consolas,Menlo,monospace;font-size:10px;letter-spacing:2px;color:#9A7424;">
BETDOC BET-SLIP · {_e(ticket.ticket_no)}</td></tr>
<tr><td style="padding:18px 20px;vertical-align:middle;"><div style="font-size:11px;font-weight:bold;letter-spacing:1px;color:{GOLD};">BETDOC QUANTITATIVE SEAL</div>
<div style="font-size:20px;font-weight:900;color:{INK};">{_e(ticket.title)}</div>
<div style="font-size:11px;color:{MUTED};">Signed {_e(ticket.signed_at_local)} · {_e(ticket.signed_by)}</div></td>
<td style="padding:18px 20px;text-align:right;vertical-align:middle;"><span style="display:inline-block;padding:8px 14px;border-radius:10px;border:1px solid {_e(fg)};background:{_e(bg)};color:{_e(fg)};font-family:Consolas,Menlo,monospace;font-weight:900;letter-spacing:1px;">{_e(ticket.book_label.upper())}</span></td></tr>
<tr><td colspan="2" style="padding:10px 20px;border-top:1px solid {RULE};border-bottom:1px solid {RULE};background:#FAFAF9;font-size:13px;">
<strong style="color:#9A7424;">Slip Score {ticket.slip_score:.0f}/100 {_e(ticket.tier)}</strong>
<span style="float:right;color:{INK};font-weight:bold;">{_e(ticket.kind.replace("_", " ").title())}{f" @ {_e(ticket.odds)}" if ticket.odds is not None else ""}</span></td></tr>
{stale}
<tr><td colspan="2" style="padding:0 8px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;">{rows}</table></td></tr>
<tr><td colspan="2" style="padding:6px 8px;background:#FAFAF9;border-top:2px solid {RULE};"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
{_figure("Kelly stake", f"{inr(ticket.stake_inr)}{usd}")}{_figure("Returns", inr(ticket.payout) if ticket.payout is not None else "—")}
{_figure("Take home after TDS", inr(ticket.take_home) if ticket.take_home is not None else "—", EMERALD)}{_figure("Stop-loss floor", inr(ticket.stop_floor), ROSE)}</tr></table>
<div style="padding:0 12px 10px;font-size:11px;color:{MUTED};">Section 115BBJ / 194BA: {ticket.tds_rate_pct or 30:.0f}% TDS{f" of {inr(ticket.tds)}" if ticket.tds is not None else ""} on the net winnings; 4% cess when the return is filed.</div></td></tr>
<tr><td colspan="2" style="padding:12px 20px;border-top:1px dashed {GOLD};font-size:12px;color:{INK};">Booking code: <strong style="font-family:Consolas,Menlo,monospace;">{_e(ticket.booking_code or "not recorded yet")}</strong></td></tr>
<tr><td colspan="2" style="padding:12px 20px;background:{INK};color:#A8A29E;font-size:10px;text-align:center;line-height:1.6;">
Seal (HMAC-SHA256) <span style="font-family:Consolas,Menlo,monospace;color:#E7E5E4;">{_e(ticket.seal)}</span><br>{_e(ticket.watermark)}<br>Nothing was placed by BetDoc: the ticket is placed by hand at the book.</td></tr>
</table></body></html>"""
