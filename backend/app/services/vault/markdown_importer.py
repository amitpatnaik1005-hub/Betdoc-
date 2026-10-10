"""Bulk credential importer: the user's markdown credentials file into the Vault (Group 70).

    python -m app.services.vault.markdown_importer --file "D:\\confidential\\API Keys & TARGET URL (Read me).md"
    python -m app.services.vault.markdown_importer --file <path> --dry-run     (parse and count, write nothing)

The parser is deliberately forgiving: people write credentials files by hand. It reads

* headings (``#``..``######``, or a line that is only bold text) as sections: a heading that names a
  bookmaker ("Parimatch", "1xBet account 2"), a data provider ("The Odds API", "Pinn API keys") or the
  sports ("Active sports") sets what the lines under it describe; any other sub-heading under a
  bookmaker ("Account 2 (main)") starts that bookmaker's next account and labels it;
* ``label: value`` / ``label = value`` / ``label - value`` lines, list items, block quotes and several
  pairs on one line (``user: a | pass: b``), a label on its own line with the value on the next;
* tables, either one account per row (a Bookmaker / Username / Password ... header) or two-column
  label | value tables;
* fenced code blocks of ``KEY=VALUE`` (``ODDS_API_KEY=...``, ``PINNACLE_USERNAME=...``) or JSON;
* a label that names its entity itself ("Odds API key 2: ...", "Betfair app key: ...") anywhere;
* sport keys (``soccer_epl``, ``cricket_ipl``) anywhere, and names ("EPL", "IPL", "NBA") in a sports section.

A second login (or a provider's second key) in the same section starts a new account. Placeholders
("xxx", "TBD", "your key here") are skipped. Warnings name line numbers and fields, never a value.

Writing: every secret is sealed with AES-256-GCM bound to its row and field; accounts are found again by
an HMAC of their login (``identity_digest``), so importing the same file twice changes nothing, and a
changed file updates the fields it carries without touching what the file does not say (active
status, priority, reserved stakes, verification). One transaction: an import lands whole or not at all.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security_vault import VaultCrypto
from app.models.omni_vault import VaultBookmakerAccount, VaultImportRun, VaultProviderCredential, VerificationStatus
from app.services.vault import catalog
from app.services.vault.catalog import Entity, FieldName

logger = logging.getLogger("betdoc.vault.import")

MAX_WARNINGS = 200
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\s*(?:\*\*|__)([^*_:]{2,80}?)(?:\*\*|__)\s*:?\s*$")
_RULE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,}|={3,})\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_MARK = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_URL = re.compile(r"https?://[^\s<>|)\]`'\"]+", re.I)
_PAIR_SPLIT = re.compile(r"\s+[|;,]\s+(?=[A-Za-z0-9][A-Za-z0-9 _#.-]{0,30}\s*[:=])|\s+\|\s+")
_SEPARATORS = (":", "=", " - ", " – ", " — ", " -> ", " → ", "\t")
_PLACEHOLDER = re.compile(
    r"^(?:|-+|—|–|n/?a|none|null|nil|tbd|tba|todo|\?+|x{2,}|\*{2,}|\.{2,}|<[^>]*>|\[[^\]]*\]|"
    r"(?:your|enter|insert|put|add)[ _-].*(?:here|key|password|token|username)?|redacted|hidden|secret|changeme|placeholder)$",
    re.I,
)
_BASE32 = re.compile(r"^[A-Z2-7]+=*$")
_CURRENCY = re.compile(r"^[A-Z]{3,5}$")
ENTITY_COLUMNS = ("bookmaker", "bookie", "book", "site", "provider", "platform", "service", "exchange", "sportsbook", "brand", "company", "source")


# ---------------------------------------------------------------------------------- parse model
@dataclass(slots=True)
class ParsedAccount:
    bookmaker_id: str
    line: int
    section: str
    label: str | None = None
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    token: str | None = None
    totp_seed: str | None = None
    notes: str | None = None
    url: str | None = None
    currency: str | None = None
    balance: Decimal | None = None
    stake_cap: Decimal | None = None

    @property
    def identity(self) -> str | None:
        """What makes the account itself: the login, else its key or token."""
        if self.username:
            return "u:" + re.sub(r"[\s-]+", "", self.username.strip().casefold()) if _looks_like_phone(self.username) else "u:" + self.username.strip().casefold()
        if self.api_key:
            return "k:" + self.api_key.strip()
        if self.token:
            return "t:" + self.token.strip()
        return None

    def secret_items(self) -> dict[str, str]:
        return {k: v for k, v in (("username", self.username), ("password", self.password), ("api_key", self.api_key), ("token", self.token),
                                   ("totp_seed", self.totp_seed), ("notes", self.notes), ("url", self.url)) if v}


@dataclass(slots=True)
class ParsedProvider:
    provider_id: str
    line: int
    section: str
    label: str | None = None
    api_key: str | None = None
    secret: str | None = None
    url: str | None = None


@dataclass(slots=True)
class ParseResult:
    accounts: list[ParsedAccount] = field(default_factory=list)
    providers: list[ParsedProvider] = field(default_factory=list)
    sports: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    lines: int = 0

    def warn(self, message: str) -> None:
        if len(self.warnings) < MAX_WARNINGS and message not in self.warnings:
            self.warnings.append(message)

    def summary(self) -> dict[str, Any]:
        return {
            "accounts_found": len(self.accounts),
            "providers_found": len(self.providers),
            "sports_found": len(self.sports),
            "syntax_warnings": list(self.warnings),
        }


@dataclass(slots=True)
class _Record:
    entity: Entity
    line: int
    section: str
    label: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    lines: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------------- value helpers
def _looks_like_phone(value: str) -> bool:
    return bool(re.fullmatch(r"\+?[\d\s()-]{8,20}", value.strip()))


def clean_value(raw: str) -> str:
    value = raw.strip()
    for mark in ("**", "__"):  # "**Label:** value" leaves the label's closing marks on the value
        if value.startswith(mark) and not value.endswith(mark):
            value = value[len(mark):].strip()
        elif value.endswith(mark) and not value.startswith(mark):
            value = value[: -len(mark)].strip()
    if value in ("**", "__"):
        return ""
    for _ in range(3):  # `x`, **x**, "x", <x>, nested once or twice
        stripped = value
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "`\"'":
            stripped = stripped[1:-1].strip()
        for wrap in ("**", "__", "``"):
            if len(stripped) > 2 * len(wrap) and stripped.startswith(wrap) and stripped.endswith(wrap):
                stripped = stripped[len(wrap):-len(wrap)].strip()
        if stripped.startswith("<") and stripped.endswith(">") and _URL.fullmatch(stripped[1:-1]):
            stripped = stripped[1:-1]
        if stripped == value:
            break
        value = stripped
    inline = re.fullmatch(r"`([^`]+)`\s*(?:\(.*\))?", value)  # `key` (main) -> key
    if inline:
        value = inline.group(1).strip()
    return value.rstrip(",;").strip()


def is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.fullmatch(value.strip()))


def _single_token(value: str) -> str:
    """Keys and tokens have no spaces: ``abc123 (main key)`` -> ``abc123``."""
    return value.split()[0] if value.split() else value


def _money(value: str) -> Decimal | None:
    digits = re.sub(r"[^\d.]", "", value.replace(",", ""))
    try:
        amount = Decimal(digits) if digits else None
    except InvalidOperation:
        return None
    return amount if amount is not None and amount >= 0 else None


def mask_identity(value: str) -> str:
    """``"parimatch_user_42"`` -> ``"pa***42"``; ``"amit@gmail.com"`` -> ``"am***@g***"``; short -> ``"a***"``."""
    value = value.strip()
    if "@" in value:
        name, _, domain = value.partition("@")
        return f"{name[:2]}***@{domain[:1]}***"
    if len(value) >= 6:
        return f"{value[:2]}***{value[-2:]}"
    return f"{value[:1]}***"


def key_hint(value: str, visible: int = 4) -> str:
    """``"abc…wxyz"``: three leading and ``visible`` trailing characters, only for keys long enough to spare them."""
    value = value.strip()
    if len(value) >= 16:
        return f"{value[:3]}…{value[-visible:]}"
    if len(value) >= 8:
        return f"…{value[-2:]}"
    return "…"


# ---------------------------------------------------------------------------------- the parser
class MarkdownCredentialParser:
    def __init__(self) -> None:
        self.result = ParseResult()
        self._sections: list[tuple[int, str, Entity | None]] = []
        self._record: _Record | None = None
        self._pending: tuple[Entity | None, FieldName, int] | None = None  # a label whose value is on the next line
        self._counter: dict[str, int] = {}

    # ---------------------------------------------------------------- context
    @property
    def _section_title(self) -> str:
        return " / ".join(title for _, title, _ in self._sections) or "(top of file)"

    @property
    def _entity(self) -> Entity | None:
        for _, _, entity in reversed(self._sections):
            if entity is not None:
                return entity
        return None

    def _heading(self, level: int, title: str, line: int) -> None:
        self._flush()
        self._pending = None
        while self._sections and self._sections[-1][0] >= level:
            self._sections.pop()
        entity = catalog.match_entity(title)
        parent = self._entity
        clean = re.sub(r"\s+", " ", re.sub(r"[*_`#]", "", title)).strip()[:128]
        label = None
        if entity is None and parent is not None and parent.kind != "sports":
            label = clean or None  # "Account 2 (main)" under "1xBet"
        elif entity is not None and entity.kind == "bookmaker" and not catalog.is_bare_name(clean):
            label = clean  # "Parimatch Account 2", "1xBet (backup)": more than the name itself
        self._sections.append((level, title.strip()[:80], entity))
        if label:
            self._record = _Record(entity or parent, line, self._section_title, label=label)  # type: ignore[arg-type]
        if entity is None and parent is None and re.search(r"sport|league", title, re.I):
            self._sections[-1] = (level, title.strip()[:80], Entity("sports", "sports"))

    # ---------------------------------------------------------------- records
    def _flush(self) -> None:
        record, self._record = self._record, None
        if record is None or not record.fields:
            return
        if record.entity.kind == "bookmaker":
            self._emit_account(record)
        elif record.entity.kind == "provider":
            self._emit_provider(record)

    def _emit_account(self, record: _Record) -> None:
        f = record.fields
        book = record.entity.key
        account = ParsedAccount(book, record.line, record.section, label=f.get("label") or record.label)
        account.username = f.get("username")
        account.password = f.get("password")
        account.api_key = _single_token(f["api_key"]) if f.get("api_key") else None
        account.token = _single_token(f["token"]) if f.get("token") else None
        account.notes = f.get("notes")
        account.url = f.get("url")
        if f.get("secret") and not account.api_key:
            account.api_key = _single_token(f["secret"])
        elif f.get("secret"):
            account.token = account.token or _single_token(f["secret"])
        if f.get("totp_seed"):
            seed = re.sub(r"\s+", "", f["totp_seed"]).upper()
            if not _BASE32.fullmatch(seed):
                self.result.warn(f"line {record.lines.get('totp_seed', record.line)}: the {catalog.bookmaker_display(book)} 2FA seed is not base32; stored as written")
                seed = f["totp_seed"].strip()
            account.totp_seed = seed
        if f.get("currency"):
            code = re.sub(r"[^A-Za-z]", "", f["currency"]).upper()
            if _CURRENCY.fullmatch(code):
                account.currency = code
            else:
                self.result.warn(f"line {record.lines.get('currency', record.line)}: currency for {catalog.bookmaker_display(book)} is not a code like INR; using {catalog.currency_for(book)}")
        for name in ("balance", "stake_cap"):
            if f.get(name):
                amount = _money(f[name])
                if amount is None or (name == "stake_cap" and amount == 0):
                    self.result.warn(f"line {record.lines.get(name, record.line)}: {name.replace('_', ' ')} for {catalog.bookmaker_display(book)} is not an amount; ignored")
                else:
                    setattr(account, name, amount)
        if account.url and not _URL.fullmatch(account.url):
            urls = _URL.findall(account.url)
            account.url = urls[0] if urls else (f"https://{account.url}" if re.fullmatch(r"[\w.-]+\.[a-z]{2,}(/\S*)?", account.url, re.I) else None)
            if account.url is None:
                self.result.warn(f"line {record.lines.get('url', record.line)}: the {catalog.bookmaker_display(book)} URL is not a web address; ignored")
        if account.identity is None:
            what = "a password or 2FA seed but no login or key" if (account.password or account.totp_seed) else "no login, key or token"
            self.result.warn(f"line {record.line}: {catalog.bookmaker_display(book)} entry in '{record.section}' has {what}: skipped")
            return
        if account.password is None and account.api_key is None and account.token is None:
            self.result.warn(f"line {record.line}: {catalog.bookmaker_display(book)} account has a login but no password, key or token")
        self.result.accounts.append(account)  # no label in the file: one is given at creation, never overwritten by a re-import

    def _emit_provider(self, record: _Record) -> None:
        f = record.fields
        provider = record.entity.key
        key = f.get("api_key") or f.get("token")
        if not key:
            if any(name in f for name in ("username", "password")):
                self.result.warn(f"line {record.line}: {catalog.provider_display(provider)} entry has a login but no API key: data providers need a key; skipped")
            elif f:
                self.result.warn(f"line {record.line}: {catalog.provider_display(provider)} entry has no API key: skipped")
            return
        self._counter[provider] = self._counter.get(provider, 0) + 1
        n = self._counter[provider]
        url = f.get("url")
        if url and not _URL.fullmatch(url):
            urls = _URL.findall(url)
            url = urls[0] if urls else None
        self.result.providers.append(ParsedProvider(
            provider, record.line, record.section,
            label=f.get("label") or record.label or catalog.provider_display(provider) + (f" #{n}" if n > 1 else ""),
            api_key=_single_token(key), secret=_single_token(f["secret"]) if f.get("secret") else None, url=url,
        ))

    def _set(self, entity: Entity | None, name: FieldName, value: str, line: int) -> None:
        entity = entity or self._entity
        if name == "sports":
            self._sports(value, line, friendly=True)
            return
        if entity is None or entity.kind == "sports":
            if name in ("username", "password", "api_key", "token", "secret", "totp_seed"):
                self.result.warn(f"line {line}: a {name.replace('_', ' ')} under '{self._section_title}', which names no bookmaker or provider: skipped")
            return
        value = clean_value(value)
        if is_placeholder(value):
            self.result.warn(f"line {line}: {name.replace('_', ' ')} is a placeholder: skipped")
            return
        record = self._record
        identity_fields = ("username",) if entity.kind == "bookmaker" else ("api_key", "token")
        if record is not None and record.entity != entity:
            self._flush()
            record = None
        if record is not None and name in record.fields:
            if name in identity_fields or name in ("password", "api_key", "token"):
                self._flush()  # a second login (or key): the next account
                record = None
            elif name == "notes":
                record.fields["notes"] += "\n" + value
                return
            else:
                self.result.warn(f"line {line}: a second {name.replace('_', ' ')} for the same {entity.kind}; kept the first")
                return
        if record is None:
            record = self._record = _Record(entity, line, self._section_title)
        record.fields[name] = value
        record.lines[name] = line

    def _sports(self, text: str, line: int, *, friendly: bool) -> None:
        keys, notes = catalog.sport_keys_in(text, friendly=friendly)
        for key in keys:
            if key not in self.result.sports:
                self.result.sports.append(key)
        for note in notes:
            self.result.warn(f"line {line}: {note}")

    # ---------------------------------------------------------------- one line
    def _pairs(self, text: str) -> list[str]:
        parts = [p for p in _PAIR_SPLIT.split(text) if p.strip()]
        return parts if len(parts) > 1 else [text]

    def _split_label(self, text: str) -> tuple[str, str] | None:
        """The earliest separator whose left side reads as a label (a field or an entity)."""
        candidates = sorted((text.find(sep), sep) for sep in _SEPARATORS if text.find(sep) > 0)
        for position, sep in candidates:
            label, value = text[:position], text[position + len(sep):]
            if len(label) > 60 or _URL.match(text[max(0, position - 5):]) and sep == ":" and label.lower().endswith(("http", "https")):
                continue
            if catalog.match_field(label) or catalog.split_entity_label(label) or catalog.match_entity(label):
                return label, value
        return None

    def _kv(self, text: str, line: int) -> bool:
        """``label: value`` (or env ``KEY=VALUE``). True when the line was understood."""
        split = self._split_label(text)
        if split is None:
            return False
        label, value = split
        value = value.strip()
        field_name = catalog.match_field(label)
        entity: Entity | None = None
        if field_name is None:
            pair = catalog.split_entity_label(label)
            if pair is not None:
                entity, field_name = pair
            else:
                entity = catalog.match_entity(label)
        if field_name is None and entity is not None:
            return self._entity_value(entity, value, line)
        if field_name is None:
            return False
        if not clean_value(value):
            self._pending = (entity, field_name, line)  # the value is on the next line
            return True
        self._set(entity, field_name, value, line)
        return True

    def _entity_value(self, entity: Entity, value: str, line: int) -> bool:
        """``Parimatch: https://...``, ``1xBet: user / pass``, ``Odds API: <key>``, ``Sports: EPL, IPL``."""
        value = clean_value(value)
        if entity.kind == "sports":
            self._sports(value, line, friendly=True)
            return True
        if not value:
            self._flush()
            self._sections.append((7, entity.key, entity))  # a label line acting as a heading
            return True
        if _URL.fullmatch(value):
            self._set(entity, "url", value, line)
            return True
        inner = self._split_label(value)
        if inner is not None and catalog.match_field(inner[0]):  # "Betfair: user: x | pass: y": the entity, then its fields
            self._flush()
            self._sections.append((7, entity.key, entity))
            return self._kv(value, line)
        if entity.kind == "provider":
            self._set(entity, "api_key", value, line)
            return True
        parts = [p.strip() for p in re.split(r"\s+/\s+|\s*\|\s*", value) if p.strip()]
        if len(parts) == 2 and not any(_URL.match(p) for p in parts):
            self._flush()
            self._set(entity, "username", parts[0], line)
            self._set(entity, "password", parts[1], line)
            self.result.warn(f"line {line}: read '{catalog.bookmaker_display(entity.key)}: a / b' as login / password")
            return True
        self.result.warn(f"line {line}: '{catalog.bookmaker_display(entity.key)}' has a value with no label (login? password?): skipped")
        return True

    def _line(self, raw: str, line: int) -> None:
        text = _LIST_MARK.sub("", re.sub(r"^\s*>+\s?", "", raw)).strip()
        if not text:
            return
        if self._pending is not None:
            entity, name, at = self._pending
            self._pending = None
            if self._split_label(text) is None:
                self._set(entity, name, text, line)
                return
            self.result.warn(f"line {at}: '{name.replace('_', ' ')}' has no value")
        bold = _BOLD_HEADING.match(raw)
        if bold and not catalog.match_field(bold.group(1)):
            self._heading(7, bold.group(1), line)
            return
        entity = self._entity
        if entity is not None and entity.kind == "sports" and self._split_label(text) is None:
            self._sports(text, line, friendly=True)
            return
        understood = False
        for part in self._pairs(text):
            understood = self._kv(part.strip(), line) or understood
        if understood:
            return
        if _URL.fullmatch(clean_value(text)) and entity is not None and entity.kind != "sports":
            self._set(entity, "url", clean_value(text), line)
            return
        keys, _ = catalog.sport_keys_in(text, friendly=False)
        if keys:
            self._sports(text, line, friendly=False)
            return
        if entity is not None and entity.kind != "sports" and re.search(r"[:=]", text):
            self.result.warn(f"line {line}: '{_label_preview(text)}' is not a field the importer knows: skipped")

    # ---------------------------------------------------------------- blocks
    def _table(self, rows: list[tuple[int, list[str]]]) -> None:
        self._flush()
        if not rows:
            return
        header_line, header = rows[0]
        body = rows[1:]
        columns: list[tuple[str, FieldName | None]] = []
        for cell in header:
            name = catalog.normalise(cell)
            columns.append(("entity", None) if name in ENTITY_COLUMNS else ("field", catalog.match_field(cell)))
        mapped = sum(1 for kind, f in columns if kind == "entity" or f is not None)
        if len(header) == 2 and mapped < 2:  # a label | value table (its first row may be data too)
            for line, cells in rows:
                if len(cells) >= 2 and not _TABLE_SEP.match("|".join(cells)):
                    self._kv(f"{cells[0]}: {cells[1]}", line)
            self._flush()
            return
        if mapped < 2:
            self.result.warn(f"line {header_line}: a table whose columns name no credential fields: skipped")
            return
        for line, cells in body:
            entity = self._entity
            fields: list[tuple[FieldName, str]] = []
            for (kind, name), cell in zip(columns, cells, strict=False):
                if kind == "entity":
                    entity = catalog.match_entity(cell) or (Entity("bookmaker", catalog.slug(cell)) if clean_value(cell) else entity)
                elif name is not None and clean_value(cell):
                    fields.append((name, cell))
            if entity is None:
                if fields:
                    self.result.warn(f"line {line}: a table row with no bookmaker or provider: skipped")
                continue
            self._flush()
            for name, cell in fields:
                self._set(entity, name, cell, line)
            self._flush()

    def _fence(self, block: list[tuple[int, str]]) -> None:
        self._flush()
        text = "\n".join(t for _, t in block).strip()
        if text.startswith(("{", "[")):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if data is not None:
                line = block[0][0]
                for label, value in _flatten(data):
                    if isinstance(value, (str, int, float)) and str(value).strip():
                        self._kv(f"{label}: {value}", line)
                self._flush()
                return
        for line, raw in block:
            stripped = raw.strip()
            if stripped and not stripped.startswith(("#", "//")):
                self._line(re.sub(r"^(?:export|set)\s+", "", stripped, flags=re.I), line)
        self._flush()

    # ---------------------------------------------------------------- driver
    def parse(self, text: str) -> ParseResult:
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.result.lines = len(lines)
        i = 0
        while i < len(lines):
            raw, number = lines[i], i + 1
            if _FENCE.match(raw):
                block, i = [], i + 1
                while i < len(lines) and not _FENCE.match(lines[i]):
                    block.append((i + 1, lines[i]))
                    i += 1
                self._fence(block)
                i += 1
                continue
            if raw.lstrip().startswith("|") and raw.count("|") >= 2:
                rows: list[tuple[int, list[str]]] = []
                while i < len(lines) and lines[i].lstrip().startswith("|"):
                    if not _TABLE_SEP.match(lines[i]):
                        rows.append((i + 1, [c.strip() for c in lines[i].strip().strip("|").split("|")]))
                    i += 1
                self._table(rows)
                continue
            heading = _HEADING.match(raw)
            if heading:
                self._heading(len(heading.group(1)), heading.group(2), number)
            elif _RULE.match(raw):
                self._flush()
            else:
                self._line(raw, number)
            i += 1
        if self._pending is not None:
            self.result.warn(f"line {self._pending[2]}: '{self._pending[1].replace('_', ' ')}' has no value")
        self._flush()
        return self.result


def _label_preview(text: str) -> str:
    label = re.split(r"[:=]", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", label).strip()[:40]


def _flatten(data: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(data, dict):
        for key, value in data.items():
            yield from _flatten(value, f"{prefix} {key}".strip())
    elif isinstance(data, list):
        for item in data:
            yield from _flatten(item, prefix)
    else:
        yield prefix, data


def parse_markdown(text: str) -> ParseResult:
    result = MarkdownCredentialParser().parse(text)
    _merge_duplicates(result)
    return result


def _merge_duplicates(result: ParseResult) -> None:
    """The same account twice in one file (same book and login): one account, the later values winning."""
    seen: dict[tuple[str, str], ParsedAccount] = {}
    merged: list[ParsedAccount] = []
    for account in result.accounts:
        key = (account.bookmaker_id, account.identity or "")
        first = seen.get(key)
        if first is None:
            seen[key] = account
            merged.append(account)
            continue
        for name in ("password", "api_key", "token", "totp_seed", "notes", "url", "currency", "balance", "stake_cap"):
            value = getattr(account, name)
            if value is not None:
                setattr(first, name, value)
        result.warn(f"line {account.line}: the {catalog.bookmaker_display(account.bookmaker_id)} account from line {first.line} again: merged")
    result.accounts = merged
    providers: dict[tuple[str, str], ParsedProvider] = {}
    for provider in result.providers:
        key = (provider.provider_id, provider.api_key or "")
        if key in providers:
            result.warn(f"line {provider.line}: the same {catalog.provider_display(provider.provider_id)} key as line {providers[key].line}: merged")
            continue
        providers[key] = provider
    result.providers = list(providers.values())


# ---------------------------------------------------------------------------------- reading input
class ImportSourceError(ValueError):
    """The file cannot be read: outside the allowed directories, too large, not text."""


def _allowed(path: Path, settings: Settings) -> bool:
    resolved = os.path.normcase(str(path.resolve()))
    for root in settings.VAULT_IMPORT_ALLOWED_DIRS:
        base = os.path.normcase(str(Path(root).expanduser().resolve()))
        if resolved == base or resolved.startswith(base.rstrip("\\/") + os.sep):
            return True
    return False


def decode_upload(data: bytes, settings: Settings) -> str:
    if len(data) > settings.VAULT_IMPORT_MAX_BYTES:
        raise ImportSourceError(f"the file is over {settings.VAULT_IMPORT_MAX_BYTES:,} bytes")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 with a byte-order mark (Windows Notepad's "Unicode")
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise ImportSourceError("the file claims UTF-16 but does not decode as it") from exc
        if "\x00" in text:
            raise ImportSourceError("the file is not text (markdown or .txt)")
        return text
    if b"\x00" in data[:4096]:
        raise ImportSourceError("the file is not text (markdown or .txt)")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportSourceError("the file's text encoding is not UTF-8, UTF-16 or Windows-1252")


def read_path(raw_path: str, settings: Settings, *, enforce_allowlist: bool = True) -> str:
    """A server-side file, only from ``VAULT_IMPORT_ALLOWED_DIRS`` (the CLI, run by the machine's owner, may skip it)."""
    path = Path(raw_path.strip().strip('"')).expanduser()
    if path.suffix.lower() not in (".md", ".markdown", ".txt"):
        raise ImportSourceError("only .md, .markdown or .txt files can be imported")
    if enforce_allowlist and not settings.VAULT_IMPORT_ALLOWED_DIRS:
        raise ImportSourceError("path imports are off: set VAULT_IMPORT_ALLOWED_DIRS to the folder that holds the file, or upload it")
    if enforce_allowlist and not _allowed(path, settings):
        raise ImportSourceError("that path is outside VAULT_IMPORT_ALLOWED_DIRS")
    try:
        if not path.is_file():
            raise ImportSourceError("no such file")
        size = path.stat().st_size
    except OSError as exc:
        raise ImportSourceError(f"the file cannot be read ({type(exc).__name__})") from exc
    if size > settings.VAULT_IMPORT_MAX_BYTES:
        raise ImportSourceError(f"the file is over {settings.VAULT_IMPORT_MAX_BYTES:,} bytes")
    try:
        return decode_upload(path.read_bytes(), settings)
    except OSError as exc:
        raise ImportSourceError(f"the file cannot be read ({type(exc).__name__})") from exc


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------- writing
def account_context(account_id: uuid.UUID, name: str) -> str:
    return f"vault-account:{account_id}:{name}"


def provider_context(provider_id: uuid.UUID, name: str) -> str:
    return f"vault-provider:{provider_id}:{name}"


def identity_digests(vault: VaultCrypto, book: str, identity: str) -> list[str]:
    return vault.blind_indexes(f"identity|{book}|{identity}")


def account_fingerprint(vault: VaultCrypto, account: ParsedAccount) -> str:
    view = account.secret_items() | {
        "label": account.label or "", "currency": account.currency or "",
        "balance": str(account.balance or ""), "stake_cap": str(account.stake_cap or ""),
    }
    return vault.blind_index("fingerprint|" + json.dumps(view, sort_keys=True))


@dataclass(slots=True)
class ImportReport:
    accounts_created: int = 0
    accounts_updated: int = 0
    accounts_unchanged: int = 0
    providers_created: int = 0
    providers_updated: int = 0
    providers_unchanged: int = 0
    providers_linked: list[str] = field(default_factory=list)
    sports_added: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    run_id: str | None = None
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}  # type: ignore[attr-defined]


def _seal_account(vault: VaultCrypto, row: VaultBookmakerAccount, account: ParsedAccount) -> None:
    for name, value in account.secret_items().items():
        column = "encrypted_target_url" if name == "url" else f"encrypted_{name}"
        setattr(row, column, vault.encrypt_key(value, context=account_context(row.id, name)))
    if account.username:
        row.username_hint = mask_identity(account.username)
    if account.url:
        row.target_host = (urlsplit(account.url).hostname or "")[:255] or None


async def _find_account(session: AsyncSession, vault: VaultCrypto, account: ParsedAccount) -> VaultBookmakerAccount | None:
    digests = identity_digests(vault, account.bookmaker_id, account.identity or "")
    return (await session.execute(
        select(VaultBookmakerAccount).where(VaultBookmakerAccount.bookmaker_id == account.bookmaker_id, VaultBookmakerAccount.identity_digest.in_(digests))
    )).scalars().first()


def _differs(vault: VaultCrypto, row: VaultBookmakerAccount, account: ParsedAccount) -> bool:
    """Does the file say anything this row does not already hold? Only what the file carries is compared:
    a field the file leaves out never counts as a change (and is never cleared)."""
    if account.label and account.label[:128] != row.label:
        return True
    if account.currency and account.currency != (row.currency or "").upper():
        return True
    for name, value in (("balance", account.balance), ("stake_cap", account.stake_cap)):
        current = getattr(row, name)
        if value is not None and (current is None or Decimal(current) != value):
            return True
    from app.services.vault.registry import account_secrets  # noqa: PLC0415 - registry imports this module

    with account_secrets(vault, row) as current_secrets:
        return any(current_secrets.get(name) != value for name, value in account.secret_items().items())


async def _upsert_account(session: AsyncSession, vault: VaultCrypto, account: ParsedAccount, origin: str, report: ImportReport, priorities: dict[str, int], *, dry_run: bool) -> None:
    existing = await _find_account(session, vault, account)
    if existing is not None and not _differs(vault, existing, account):
        report.accounts_unchanged += 1
        if not dry_run and existing.identity_digest != (digest := identity_digests(vault, account.bookmaker_id, account.identity or "")[0]):
            existing.identity_digest = digest  # found under a previous key: re-index under the current one
        return
    if dry_run:
        if existing is None:
            report.accounts_created += 1
        else:
            report.accounts_updated += 1
        return
    fingerprint = account_fingerprint(vault, account)
    if existing is None:
        if account.bookmaker_id not in priorities:
            count = (await session.execute(select(VaultBookmakerAccount.id).where(VaultBookmakerAccount.bookmaker_id == account.bookmaker_id))).all()
            priorities[account.bookmaker_id] = len(count)
        priorities[account.bookmaker_id] += 1
        n = priorities[account.bookmaker_id]
        default_label = catalog.bookmaker_display(account.bookmaker_id) + (f" {n}" if n > 1 else "")
        row = VaultBookmakerAccount(
            id=uuid.uuid4(), bookmaker_id=account.bookmaker_id, label=(account.label or default_label)[:128],
            identity_digest=identity_digests(vault, account.bookmaker_id, account.identity or "")[0],
            currency=account.currency or catalog.currency_for(account.bookmaker_id), adapter_key=catalog.adapter_key(account.bookmaker_id),
            is_active=True, priority=priorities[account.bookmaker_id], balance=account.balance, stake_cap=account.stake_cap,
            reserved=Decimal(0), source=origin, verification_status=VerificationStatus.UNVERIFIED.value, secrets_fingerprint=fingerprint,
        )
        _seal_account(vault, row, account)
        session.add(row)
        report.accounts_created += 1
        return
    secrets_changed = any(name in account.secret_items() for name in ("password", "api_key", "token", "totp_seed"))
    _seal_account(vault, existing, account)
    if account.label:
        existing.label = account.label[:128]
    if account.currency:
        existing.currency = account.currency
    if account.balance is not None:
        existing.balance = account.balance
    if account.stake_cap is not None:
        existing.stake_cap = account.stake_cap
    existing.identity_digest = identity_digests(vault, account.bookmaker_id, account.identity or "")[0]
    existing.secrets_fingerprint = fingerprint
    existing.source = origin
    if secrets_changed:
        existing.verification_status, existing.verification_detail = VerificationStatus.UNVERIFIED.value, "credentials changed by an import"
    report.accounts_updated += 1


async def _upsert_provider(session: AsyncSession, vault: VaultCrypto, provider: ParsedProvider, origin: str, report: ImportReport, settings: Settings, *, dry_run: bool) -> VaultProviderCredential | None:
    digests = vault.blind_indexes(f"provider|{provider.provider_id}|{provider.api_key}")
    existing = (await session.execute(
        select(VaultProviderCredential).where(VaultProviderCredential.provider_id == provider.provider_id, VaultProviderCredential.key_digest.in_(digests))
    )).scalars().first()
    if existing is not None:
        same_secret = existing.encrypted_secret is None if provider.secret is None else (
            existing.encrypted_secret is not None and vault.decrypt_key(existing.encrypted_secret, context=provider_context(existing.id, "secret")) == provider.secret
        )
        changed = (provider.url is not None and provider.url != existing.base_url) or not same_secret
        if not changed:
            report.providers_unchanged += 1
            return existing
        if dry_run:
            report.providers_updated += 1
            return existing
        if provider.url is not None:
            existing.base_url = provider.url
        if provider.secret is not None:
            existing.encrypted_secret = vault.encrypt_key(provider.secret, context=provider_context(existing.id, "secret"))
        existing.key_digest, existing.source = digests[0], origin
        report.providers_updated += 1
        return existing
    report.providers_created += 1
    if dry_run:
        return None
    row = VaultProviderCredential(
        id=uuid.uuid4(), provider_id=provider.provider_id, label=(provider.label or catalog.provider_display(provider.provider_id))[:128],
        key_digest=digests[0], api_key_hint=key_hint(provider.api_key or "", settings.mask_visible_chars), base_url=provider.url,
        is_active=True, source=origin, verification_status=VerificationStatus.UNVERIFIED.value,
    )
    row.encrypted_api_key = vault.encrypt_key(provider.api_key or "", context=provider_context(row.id, "api_key"))
    if provider.secret:
        row.encrypted_secret = vault.encrypt_key(provider.secret, context=provider_context(row.id, "secret"))
    session.add(row)
    return row


async def link_to_fleet(session: AsyncSession, vault: VaultCrypto, row: VaultProviderCredential, settings: Settings, redis: Redis | None, *, force: bool = False) -> str:
    """Give the provider's Fleet Command source this key. A source that already runs on a different key keeps
    it unless ``force`` (the admin promoting this one): an import never swaps a working key silently.
    Returns linked | already | kept_existing | no_source."""
    from app.models.omni_vault import OmniFleetSource  # noqa: PLC0415
    from app.services.omni_fleet import publish_schedule  # noqa: PLC0415 - heavy module
    from app.services.omni_normalizer import default_alias_dictionary  # noqa: PLC0415
    from app.services.omni_router import load_registry  # noqa: PLC0415

    source_id = catalog.PROVIDER_FLEET_SOURCE.get(row.provider_id)
    if source_id is None and await session.get(OmniFleetSource, row.provider_id) is not None:
        source_id = row.provider_id  # a config-driven source registered under the provider's own id
    if source_id is None:
        return "no_source"
    plain = vault.decrypt_key(row.encrypted_api_key, context=provider_context(row.id, "api_key"))
    try:
        source = await session.get(OmniFleetSource, source_id)
        if source is None:  # created inside the import's own transaction (Fleet Command's helper would commit it)
            source = OmniFleetSource(source_id=source_id, is_enabled=True, consecutive_failures=0)
            session.add(source)
            await session.flush()
        if source.encrypted_api_key:
            try:
                current = vault.decrypt_key(source.encrypted_api_key)
            except Exception:  # noqa: BLE001 - an unreadable key is replaced, never trusted
                current = None
            if current == plain:
                row.linked_source_id = source_id
                return "already"
            if not force:
                return "kept_existing"
        source.encrypted_api_key = vault.encrypt_key(plain)
        source.api_key_hint = key_hint(plain, settings.mask_visible_chars)
        source.paused_at, source.consecutive_failures, source.last_error = None, 0, None
        row.linked_source_id = source_id
        for other in (await session.execute(select(VaultProviderCredential).where(VaultProviderCredential.linked_source_id == source_id, VaultProviderCredential.id != row.id))).scalars():
            other.linked_source_id = None
        if redis is not None:
            registry, _ = await load_registry(session, settings, default_alias_dictionary())
            if source_id in registry:
                try:
                    await publish_schedule(redis, source, registry[source_id], settings)
                except Exception:  # noqa: BLE001 - the next fleet run republishes it
                    logger.warning("Vault: schedule for %s not mirrored to Redis", source_id)
        return "linked"
    finally:
        plain = ""


async def import_parsed(
    session: AsyncSession, vault: VaultCrypto, parsed: ParseResult, settings: Settings, *, origin: str, content_sha256: str,
    actor_id: uuid.UUID | None = None, redis: Redis | None = None, dry_run: bool = False,
) -> ImportReport:
    """Encrypt and upsert everything parsed, in the caller's transaction (it commits). ``dry_run``: classify only."""
    from app.services.vault import fleet_config  # noqa: PLC0415 - import cycle through the overlay publisher

    report = ImportReport(warnings=list(parsed.warnings), dry_run=dry_run)
    priorities: dict[str, int] = {}
    for account in parsed.accounts:
        await _upsert_account(session, vault, account, origin, report, priorities, dry_run=dry_run)
    for provider in parsed.providers:
        row = await _upsert_provider(session, vault, provider, origin, report, settings, dry_run=dry_run)
        if row is not None and not dry_run and row.linked_source_id is None:
            await session.flush()
            outcome = await link_to_fleet(session, vault, row, settings, redis)
            if outcome == "linked":
                report.providers_linked.append(row.provider_id)
            elif outcome == "kept_existing":
                report.warnings.append(f"{catalog.provider_display(row.provider_id)}: Fleet Command already runs on another key; kept it. Promote '{row.label}' from the Vault tab to switch")
    config = await fleet_config.get_config(session)
    new_sports = [s for s in parsed.sports if s not in (config.sports or []) and s not in settings.odds_sport_keys]
    report.sports_added = new_sports
    if dry_run:
        return report
    if new_sports:
        config.sports = [*(config.sports or []), *new_sports]
    run = VaultImportRun(
        origin=origin, content_sha256=content_sha256, accounts_created=report.accounts_created, accounts_updated=report.accounts_updated,
        accounts_unchanged=report.accounts_unchanged, providers_created=report.providers_created, providers_updated=report.providers_updated,
        providers_unchanged=report.providers_unchanged, sports_added=new_sports, warnings=report.warnings[:MAX_WARNINGS], actor_id=actor_id,
    )
    session.add(run)
    await session.flush()
    report.run_id = str(run.id)
    await fleet_config.bump(session, actor_id)
    return report


def preview_payload(parsed: ParseResult, report: ImportReport | None = None) -> dict[str, Any]:
    """The dry-run answer: counts, warnings and a masked view of what was found (no value, ever)."""
    out = parsed.summary() | {
        "accounts": [
            {
                "line": a.line, "bookmaker": a.bookmaker_id, "bookmaker_name": catalog.bookmaker_display(a.bookmaker_id),
                "label": a.label or catalog.bookmaker_display(a.bookmaker_id),
                "username_hint": mask_identity(a.username) if a.username else None, "currency": a.currency or catalog.currency_for(a.bookmaker_id),
                "has_password": bool(a.password), "has_api_key": bool(a.api_key), "has_token": bool(a.token), "has_2fa": bool(a.totp_seed),
                "target_host": urlsplit(a.url).hostname if a.url else None, "adapter": catalog.adapter_key(a.bookmaker_id),
            }
            for a in parsed.accounts
        ],
        "providers": [
            {"line": p.line, "provider": p.provider_id, "provider_name": catalog.provider_display(p.provider_id), "label": p.label,
             "key_hint": key_hint(p.api_key or ""), "has_secret": bool(p.secret), "fleet_source": catalog.PROVIDER_FLEET_SOURCE.get(p.provider_id)}
            for p in parsed.providers
        ],
        "sports": list(parsed.sports),
        "lines": parsed.lines,
    }
    if report is not None:
        out["changes"] = {k: v for k, v in report.as_dict().items() if k not in ("warnings", "run_id", "dry_run")}
    return out


# ---------------------------------------------------------------------------------- CLI
async def _cli(path: str, dry_run: bool) -> int:
    from app.core.config import get_settings  # noqa: PLC0415
    from app.core.database import AsyncSessionLocal  # noqa: PLC0415

    settings = get_settings()
    try:
        text = read_path(path, settings, enforce_allowlist=False)  # the machine's owner, at its own terminal
    except ImportSourceError as exc:
        print(f"Cannot import: {exc}", file=sys.stderr)
        return 2
    parsed = parse_markdown(text)
    try:
        vault = VaultCrypto.from_settings(settings)
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot import: the vault is not configured ({type(exc).__name__}: MASTER_VAULT_KEY)", file=sys.stderr)
        return 2
    redis = Redis.from_url(settings.REDIS_URL.get_secret_value(), decode_responses=True)
    try:
        async with AsyncSessionLocal() as session:
            report = await import_parsed(session, vault, parsed, settings, origin="cli", content_sha256=content_digest(text), redis=None if dry_run else redis, dry_run=dry_run)
            if dry_run:
                await session.rollback()
            else:
                await session.commit()
                from app.services.vault import fleet_config  # noqa: PLC0415

                async with AsyncSessionLocal() as fresh:
                    await fleet_config.publish(fresh, redis, settings)
    finally:
        await redis.aclose()
    summary = parsed.summary()
    print(f"{'Dry run' if dry_run else 'Imported'}: {summary['accounts_found']} accounts, {summary['providers_found']} provider keys, {summary['sports_found']} sports")
    print(f"  accounts  created {report.accounts_created}, updated {report.accounts_updated}, unchanged {report.accounts_unchanged}")
    print(f"  providers created {report.providers_created}, updated {report.providers_updated}, unchanged {report.providers_unchanged}"
          + (f"; linked to Fleet Command: {', '.join(report.providers_linked)}" if report.providers_linked else ""))
    if report.sports_added:
        print(f"  sports activated: {', '.join(report.sports_added)}")
    for warning in report.warnings:
        print(f"  warning: {warning}")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.services.vault.markdown_importer", description="Import a markdown credentials file into the BetDoc Vault (AES-256-GCM).")
    parser.add_argument("--file", required=True, help="the .md / .txt file")
    parser.add_argument("--dry-run", action="store_true", help="parse and classify, write nothing")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.WARNING)
    return asyncio.run(_cli(args.file, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
