"""The Bet-Slip ticket as an editable Word document (Group 106, point b.12, channel 3). Developed for Amit Ashok Kumar
Patnaik.

A complete WordprocessingML package written directly (no third-party library): content types, the package and
document relationships, core and app properties, a style sheet (Normal, Title, headings, Table Grid), a page footer
carrying the watermark and the ticket number on every page, and the document: the ticket (verdict, money, selections in the book's own wording), the Consultant's diagnostic report as
signed (the Doctor, the Lawyer, the Consultant, TipMaster), each leg's rationale (fair chance against the price),
the seal and how to verify it. Element order follows the ECMA-376 schema so Word opens it
without repair; every text node is escaped and stripped of characters XML cannot carry.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Any
from xml.sax.saxutils import escape

from app.domain.oracle.slip_formatter import inr
from app.domain.slip_ornament.ticket import Ticket

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
GOLD, EMERALD, INK, MUTED, ROSE, HEADER_FILL = "C89B3C", "059669", "1C1917", "78716C", "BE123C", "F3EBD7"
PAGE_WIDTH = 9638  # A4 (11906 twips) less two 1134-twip margins


def _text(value: object) -> str:
    return escape(_INVALID.sub("", "" if value is None else str(value)))


def run(text: object, *, bold: bool = False, italic: bool = False, colour: str | None = None, size: float | None = None, mono: bool = False) -> str:
    props = []
    if mono:
        props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>')
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if colour:
        props.append(f'<w:color w:val="{colour}"/>')
    if size:
        props.append(f'<w:sz w:val="{round(size * 2)}"/><w:szCs w:val="{round(size * 2)}"/>')
    content = "<w:br/>".join(f'<w:t xml:space="preserve">{_text(line)}</w:t>' for line in str(text).split("\n"))
    return f"<w:r>{'<w:rPr>' + ''.join(props) + '</w:rPr>' if props else ''}{content}</w:r>"


def para(*runs: str, style: str | None = None, align: str | None = None, after: int | None = None, shade: str | None = None) -> str:
    props = []
    if style:
        props.append(f'<w:pStyle w:val="{style}"/>')
    if shade:
        props.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>')
    if after is not None:
        props.append(f'<w:spacing w:after="{after}"/>')
    if align:
        props.append(f'<w:jc w:val="{align}"/>')
    return f"<w:p>{'<w:pPr>' + ''.join(props) + '</w:pPr>' if props else ''}{''.join(runs)}</w:p>"


def heading(text: str, level: int = 1) -> str:
    return para(run(text), style=f"Heading{level}")


def table(rows: Sequence[Sequence[str]], widths: Sequence[int]) -> str:
    """``rows``: cell paragraphs (the first row is the header, repeated on each page)."""
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    out = []
    header_fill = f'<w:shd w:val="clear" w:color="auto" w:fill="{HEADER_FILL}"/>'
    for i, cells in enumerate(rows):
        shade = header_fill if i == 0 else ""
        tcs = "".join(f'<w:tc><w:tcPr><w:tcW w:w="{widths[j]}" w:type="dxa"/>{shade}</w:tcPr>{cell or para()}</w:tc>' for j, cell in enumerate(cells))
        out.append(f"<w:tr>{'<w:trPr><w:tblHeader/></w:trPr>' if i == 0 else ''}{tcs}</w:tr>")
    return (f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblLook w:val="04A0"/></w:tblPr>'
            f"<w:tblGrid>{grid}</w:tblGrid>{''.join(out)}</w:tbl>")


def _cell(text: object, **kwargs: Any) -> str:
    return para(run(text, size=kwargs.pop("size", 9), **kwargs), after=0)


def _pct(value: Any, digits: int = 1, signed: bool = False) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:{'+' if signed else ''}.{digits}f}%"


# ================================================================ the document
def _ticket_section(t: Ticket) -> list[str]:
    usd = f" (${t.stake_usd})" if t.stake_usd is not None else ""
    out = [
        para(run("BETDOC BET-SLIP", bold=True, colour=GOLD, size=20), style="Title", align="center"),
        para(run(f"{t.ticket_no} · {t.book_label} · signed {t.signed_at_local}", italic=True, colour=MUTED, size=9), align="center"),
        para(run(t.title, bold=True, colour=EMERALD, size=13), run(f"     Slip Score {t.slip_score:.0f}/100 {t.tier}", bold=True, colour="9A7424", size=13)),
        para(run(t.summary, colour="44403C", size=9)),
    ]
    if not t.fresh:
        out.append(para(run(f"Prices as signed {t.age_minutes} minutes ago: re-check them at {t.book_label} before placing.", bold=True, colour="9A3412", size=9), shade="FFF7ED"))
    head = [_cell(h, bold=True) for h in ("Total odds", "Kelly stake", "Returns", "Take home after TDS")]
    vals = [_cell(t.odds if t.odds is not None else "—", bold=True, mono=True, size=11), _cell(f"{inr(t.stake_inr)}{usd}", bold=True, size=10),
            _cell(inr(t.payout) if t.payout is not None else "—", bold=True, mono=True, size=11),
            _cell(inr(t.take_home) if t.take_home is not None else "—", bold=True, mono=True, size=11, colour=EMERALD)]
    head2 = [_cell(h, bold=True) for h in ("TDS (Section 115BBJ / 194BA)", "Stop-loss floor", "Win chance", "Joint EV")]
    vals2 = [_cell(f"{inr(t.tds)} ({t.tds_rate_pct or 0:.0f}% of {inr(t.net_winnings)})" if t.tds is not None and t.net_winnings is not None else "—"),
             _cell(f"{inr(t.stop_floor)} (cash out {t.stop_pct * 100:.0f}% under the stake)", colour=ROSE),
             _cell(_pct(t.joint_probability)), _cell(_pct(t.joint_ev, signed=True))]
    out += [table([head, vals], [2400, 2638, 2300, 2300]), para(after=60), table([head2, vals2], [2900, 2738, 2000, 2000])]
    if t.stake_usd is None:
        out.append(para(run("No fresh USD rate at signing: the ticket shows no dollar figure.", italic=True, colour=MUTED, size=8)))
    out.append(heading("Selections, as the book shows them"))
    rows = [[_cell(h, bold=True) for h in ("#", "Match", "League · kick-off", "Market and selection", "Code", "Odds")]]
    for leg in t.legs:
        rows.append([_cell(leg.number, mono=True), _cell(leg.fixture, bold=True), _cell(" · ".join(p for p in (leg.sport, leg.league, leg.kickoff) if p) or "—"),
                     _cell(leg.market, bold=True, colour=EMERALD), _cell(leg.code, mono=True), _cell(leg.price if leg.price is not None else "—", bold=True, mono=True)])
    out.append(table(rows, [400, 2300, 2400, 2638, 900, 1000]))
    return out


def _report_section(t: Ticket, report: Mapping[str, Any]) -> list[str]:
    doctor, lawyer = report.get("doctor") or {}, report.get("lawyer") or {}
    capital, tip = report.get("consultant") or {}, report.get("tipmaster") or {}
    out = [heading("Consultant diagnostic report"), heading("The Doctor", 2)]
    pathologies = doctor.get("pathologies") or []
    if not pathologies:
        out.append(para(run("No pathology found: no failed pillar, no leg priced under its fair value, no heavy margin.", colour=EMERALD, size=9)))
    for p in pathologies:
        out.append(para(run(f"[{p.get('severity')}] {p.get('title')}: ", bold=True, size=9), run(p.get("diagnosis"), size=9)))
    unexamined = doctor.get("unexamined") or []
    if unexamined:
        out.append(para(run("Not examined (no evidence): ", bold=True, colour=MUTED, size=9),
                        run("; ".join(f"{u.get('number')}. {u.get('title')}" for u in unexamined), colour=MUTED, size=9)))
    out.append(heading("The Lawyer", 2))
    for c in lawyer.get("clauses") or []:
        reference = c.get("reference") or f"not on file: confirm it in {t.book_label}'s own rules"
        out.append(para(run(f"{c.get('title')} ({str(c.get('risk', '')).lower()} risk): ", bold=True, size=9), run(c.get("finding"), size=9),
                        run(f"\nHedge: {c.get('hedge')} Basis: {c.get('basis')}. Clause: {reference}.", colour=MUTED, size=8)))
    out.append(heading("The Consultant", 2))
    downside = capital.get("downside") or {}
    out.append(para(run(f"KUMBHA's stake: {capital.get('sizing_reason') or inr(t.stake_inr)}. Regime {str(capital.get('regime') or '—').lower().replace('_', ' ')}, "
                        f"rolling drawdown {_pct(capital.get('drawdown'))}. The slip loses everything {_pct(downside.get('p_total_loss'))} of the time.", size=9)))
    out.append(heading("TipMaster: the crowd against BetDoc", 2))
    out.append(para(run(f"{tip.get('label', '—')}: ", bold=True, size=9), run(tip.get("explanation"), size=9)))
    out.append(heading("Why each leg"))
    for leg in t.legs:
        implied = None if not leg.price else float(1 / leg.price)
        out.append(para(run(f"{leg.number}) {leg.fixture}, {leg.market}: ", bold=True, size=9),
                        run(f"{leg.price if leg.price is not None else '—'} at {t.book_label} implies {_pct(implied)}; BetDoc's fair chance is "
                            f"{_pct(leg.fair_probability)}, an EV of {_pct(leg.ev, signed=True)} on every rupee.", size=9)))
    return out


def _closing(t: Ticket) -> list[str]:
    return [
        heading("Booking code and seal"),
        para(run("Booking code: ", bold=True, size=9), run(t.booking_code or "not recorded yet", mono=True, size=10)),
        para(run("Seal (HMAC-SHA256 over the signed report): ", bold=True, size=9), run(t.seal, mono=True, size=8)),
        para(run(f"Verify it: GET /api/v1/consultant/verdicts/{t.report_id} re-checks the seal against the stored report.", colour=MUTED, size=8)),
        para(run("Nothing was placed by BetDoc: the ticket is placed by hand at the book.", italic=True, colour=MUTED, size=8)),
    ]


def footer_xml(t: Ticket) -> str:
    """The watermark and the ticket number at the foot of every page."""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'{para(run(f"{t.watermark} · {t.ticket_no}", italic=True, colour=MUTED, size=8), align="center", after=0)}</w:ftr>')


def document_xml(ticket: Ticket, report: Mapping[str, Any]) -> str:
    body = "".join([*_ticket_section(ticket), *_report_section(ticket, report), *_closing(ticket)])
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<w:body>{body}<w:sectPr><w:footerReference w:type="default" r:id="rId2"/><w:pgSz w:w="11906" w:h="16838"/>'
            '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="709" w:footer="709" w:gutter="0"/></w:sectPr></w:body></w:document>')


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
          '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:eastAsia="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
          '<w:sz w:val="20"/><w:szCs w:val="20"/><w:lang w:val="en-IN"/></w:rPr></w:rPrDefault>'
          '<w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
          '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
          '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
          f'<w:pPr><w:spacing w:after="40"/><w:jc w:val="center"/></w:pPr><w:rPr><w:b/><w:color w:val="{GOLD}"/><w:sz w:val="40"/><w:szCs w:val="40"/></w:rPr></w:style>'
          '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
          '<w:pPr><w:keepNext/><w:spacing w:before="280" w:after="80"/><w:outlineLvl w:val="0"/></w:pPr>'
          f'<w:rPr><w:b/><w:color w:val="{INK}"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr></w:style>'
          '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
          '<w:pPr><w:keepNext/><w:spacing w:before="160" w:after="60"/><w:outlineLvl w:val="1"/></w:pPr>'
          f'<w:rPr><w:b/><w:color w:val="9A7424"/><w:sz w:val="22"/><w:szCs w:val="22"/></w:rPr></w:style>'
          '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/><w:uiPriority w:val="99"/><w:semiHidden/>'
          '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="40" w:type="dxa"/><w:left w:w="108" w:type="dxa"/>'
          '<w:bottom w:w="40" w:type="dxa"/><w:right w:w="108" w:type="dxa"/></w:tblCellMar></w:tblPr></w:style>'
          '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:basedOn w:val="TableNormal"/><w:uiPriority w:val="59"/>'
          '<w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:space="0" w:color="D6D3D1"/><w:left w:val="single" w:sz="4" w:space="0" w:color="D6D3D1"/>'
          '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="D6D3D1"/><w:right w:val="single" w:sz="4" w:space="0" w:color="D6D3D1"/>'
          '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="D6D3D1"/><w:insideV w:val="single" w:sz="4" w:space="0" w:color="D6D3D1"/></w:tblBorders></w:tblPr></w:style>'
          '</w:styles>')

CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                 '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
                 '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                 '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                 '</Types>')

PACKAGE_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
                '</Relationships>')

DOCUMENT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
                 '</Relationships>')


def _core(t: Ticket) -> str:
    stamp = t.signed_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>BetDoc Bet-Slip {_text(t.ticket_no)}</dc:title><dc:subject>{_text(t.title)}</dc:subject><dc:creator>{_text(t.architect)}</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
            "</cp:coreProperties>")


APP = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>BetDoc Core Engine</Application></Properties>')


def docx_bytes(ticket: Ticket, report: Mapping[str, Any]) -> bytes:
    parts = [("[Content_Types].xml", CONTENT_TYPES), ("_rels/.rels", PACKAGE_RELS), ("docProps/core.xml", _core(ticket)), ("docProps/app.xml", APP),
             ("word/_rels/document.xml.rels", DOCUMENT_RELS), ("word/styles.xml", STYLES), ("word/footer1.xml", footer_xml(ticket)),
             ("word/document.xml", document_xml(ticket, report))]
    stamp = ticket.signed_at.astimezone(UTC).timetuple()[:6]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        for name, xml in parts:
            info = zipfile.ZipInfo(name, date_time=stamp)  # the signing time: the same verdict gives the same bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, xml.encode("utf-8"))
    return buffer.getvalue()
