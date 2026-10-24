"""OddsPapi (oddspapi.io): 1xBet and Pinnacle prices as The Odds API's failover (Group 128).

OddsPapi carries 300+ bookmakers (1xBet and Pinnacle among them; not Parimatch). One ``GET /odds-by-tournaments``
returns every fixture of up to five tournaments with one bookmaker's markets, so a run costs one request per configured
bookmaker per five tournaments. The free plan allows ``request_limit`` requests a month (250 at the time of writing), so
the source runs only when the router hands it a group (The Odds API unavailable or under its reserve) and at
``ODDSPAPI_INTERVAL_SECONDS``. ``GET /account`` costs nothing and reports the month's count: it is the probe, and it is
read after every run so the router sees the budget.

The response is translated into The Odds API's event shape (``bookmakers -> markets h2h / totals / spreads / btts``),
so the fleet's existing normaliser de-vigs it and Ashoka prices it like any feed. Team names come from
``GET /participants`` and market meanings from ``GET /markets``, each read once per sport and kept in the process for
``ODDSPAPI_CATALOG_TTL_SECONDS``.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, ClassVar

from app.adapters.ingestion.base import BaseDataIngestor, IngestionBatch, IngestionError, SourcePayload
from app.core.config import Settings

SOCCER = 10
MARKET_KINDS = {"1x2": "h2h", "totals": "totals", "spreads": "spreads", "bothteamsscore": "btts"}  # OddsPapi marketType -> event market key
CHUNK = 5  # tournaments per /odds-by-tournaments call
_Catalog = dict[str, tuple[str, float | None, dict[str, str]]]  # marketId -> (event market key, line, outcomeId -> outcome name)


class OddsPapiIngestor(BaseDataIngestor):
    source_id = "oddspapi"
    display_name = "OddsPapi"
    description = "1xBet and Pinnacle prices per tournament (oddspapi.io): The Odds API's failover, on a monthly request budget."
    requires_api_key = True
    docs_url = "https://oddspapi.io/en/docs"
    requests_per_minute = 20.0
    burst = 2

    # Per process: (time.monotonic() when read, value), so a catalogue costs one request per sport per TTL
    _participants: ClassVar[dict[int, tuple[float, dict[str, str]]]] = {}
    _markets: ClassVar[dict[int, tuple[float, _Catalog]]] = {}

    @classmethod
    def interval_seconds(cls, settings: Settings) -> float:
        return float(settings.ODDSPAPI_INTERVAL_SECONDS)

    @classmethod
    def coverage(cls, settings: Settings) -> dict[str, str]:
        """Canonical sport key -> OddsPapi tournament id, for the sports the fleet polls."""
        polled = set(settings.odds_sport_keys)
        return {sport: str(tid) for sport, tid in settings.ODDSPAPI_TOURNAMENTS.items() if sport in polled}

    # ---------------------------------------------------------------- requests
    def _url(self, path: str) -> str:
        return f"{self._settings.ODDSPAPI_BASE_URL.rstrip('/')}/{path.lstrip('/')}"

    async def _account(self) -> None:
        """``GET /account`` (not counted): the month's request count and limit, as the batch's quota."""
        try:
            body = await self._get_json(self._url("account"), params={"apiKey": self._api_key or ""})
        except IngestionError:
            return
        subs = [s for s in (body.get("subscriptions") or []) if isinstance(s, Mapping) and s.get("is_active")] if isinstance(body, Mapping) else []
        if not subs:
            return
        limit, used = subs[0].get("request_limit"), subs[0].get("request_count")
        if isinstance(limit, (int, float)) and isinstance(used, (int, float)):
            self._quota_used = float(used)
            self._quota_remaining = max(0.0, float(limit) - float(used))

    async def probe(self) -> IngestionBatch:
        await self._account()
        return self._batch([], {"probe": True})

    async def _catalogue(self, sport_id: int) -> tuple[dict[str, str], _Catalog]:
        ttl = float(self._settings.ODDSPAPI_CATALOG_TTL_SECONDS)
        cached = type(self)._participants.get(sport_id)
        if cached is None or time.monotonic() - cached[0] >= ttl:
            raw = await self._get_json(self._url("participants"), params={"apiKey": self._api_key or "", "sportId": str(sport_id)})
            names = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}
            cached = type(self)._participants[sport_id] = (time.monotonic(), names)
        known = type(self)._markets.get(sport_id)
        if known is None or time.monotonic() - known[0] >= ttl:
            raw = await self._get_json(self._url("markets"), params={"apiKey": self._api_key or "", "sportId": str(sport_id)})
            known = type(self)._markets[sport_id] = (time.monotonic(), parse_markets(raw))
        return cached[1], known[1]

    async def fetch(self, scope: Sequence[str] | None = None) -> IngestionBatch:
        covered = self.coverage(self._settings)
        sport_of = {tid: sport for sport, tid in covered.items()}
        tournaments = [t for t in (scope if scope is not None else covered.values()) if t in sport_of]
        if not tournaments:
            raise IngestionError("OddsPapi: no tournament in scope (ODDSPAPI_TOURNAMENTS for the polled sports)")
        names, markets = await self._catalogue(SOCCER)
        books = [b.strip() for b in self._settings.ODDSPAPI_BOOKMAKERS.split(",") if b.strip()]
        fixtures: dict[str, dict[str, Any]] = {}
        for i in range(0, len(tournaments), CHUNK):
            chunk = tournaments[i:i + CHUNK]
            for book in books:
                rows = await self._get_json(self._url("odds-by-tournaments"), params={"apiKey": self._api_key or "", "bookmaker": book, "tournamentIds": ",".join(chunk)})
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, Mapping) and row.get("fixtureId"):
                        merged = fixtures.setdefault(str(row["fixtureId"]), {**row, "bookmakerOdds": {}})
                        merged["bookmakerOdds"].update(row.get("bookmakerOdds") or {})
        await self._account()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        unnamed = 0
        for row in fixtures.values():
            sport = sport_of.get(str(row.get("tournamentId")))
            if sport is None:
                continue
            event = to_event(row, sport, names, markets)
            if event is None:
                unnamed += 1
                continue
            grouped[sport].append(event)
        return self._batch([SourcePayload(key=sport, data=events) for sport, events in grouped.items()],
                           {"tournaments": tournaments, "bookmakers": books, "fixtures": len(fixtures), "unnamed": unnamed})


# ================================================================ translation (pure)
def parse_markets(raw: Any) -> _Catalog:
    """``GET /markets`` -> the full-time 1X2, totals, Asian handicap and BTTS markets, by id."""
    out: _Catalog = {}
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, Mapping) or m.get("period") != "fulltime" or m.get("playerProp"):
            continue
        key = MARKET_KINDS.get(str(m.get("marketType")))
        if key is None or m.get("marketId") is None:
            continue
        outcomes = {str(o["outcomeId"]): str(o.get("outcomeName") or "") for o in m.get("outcomes") or [] if isinstance(o, Mapping) and o.get("outcomeId") is not None}
        line = m.get("handicap")
        out[str(m["marketId"])] = (key, float(line) if isinstance(line, (int, float)) else None, outcomes)
    return out


def _price(outcome: Mapping[str, Any]) -> tuple[float, str | None, bool] | None:
    """The (non-player) price of an outcome when it is open: (price, changedAt, the book's main line)."""
    player = (outcome.get("players") or {}).get("0")
    if not isinstance(player, Mapping) or not player.get("active"):
        return None
    price = player.get("price")
    if not isinstance(price, (int, float)) or price <= 1.0:
        return None
    return float(price), player.get("changedAt"), bool(player.get("mainLine"))


def h2h_size(markets: _Catalog) -> int:
    """How many outcomes the full-time 1X2 has (three in football)."""
    return next((len(outcomes) for key, _, outcomes in markets.values() if key == "h2h"), 3)


def main_handicap(lines: Mapping[float, Mapping[str, tuple[float, bool]]]) -> float | None:
    """The home line the book marks as its main one, else the most balanced line both sides are priced on."""
    both = {line: sides for line, sides in lines.items() if "1" in sides and "2" in sides}
    if not both:
        return None
    flagged = [line for line, sides in both.items() if sides["1"][1] or sides["2"][1]]
    if flagged:
        return min(flagged, key=abs)
    return min(both, key=lambda line: (abs(both[line]["1"][0] - both[line]["2"][0]), abs(line)))


def to_event(row: Mapping[str, Any], sport: str, names: Mapping[str, str], markets: _Catalog) -> dict[str, Any] | None:
    """One OddsPapi fixture as The Odds API's event: None without both team names (nothing to pair it with)."""
    home, away = names.get(str(row.get("participant1Id"))), names.get(str(row.get("participant2Id")))
    if not home or not away or not row.get("startTime"):
        return None
    bookmakers = []
    for book, data in (row.get("bookmakerOdds") or {}).items():
        if not isinstance(data, Mapping) or data.get("suspended") or data.get("bookmakerIsActive") is False:
            continue
        by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        handicaps: dict[float, dict[str, tuple[float, bool]]] = defaultdict(dict)  # home line -> side -> (price, main line)
        stamps: list[str] = []
        for market_id, market in (data.get("markets") or {}).items():
            meaning = markets.get(str(market_id))
            # marketActive is not a "closed" flag here (1xBet's open pre-match 1X2 arrives marketActive false with every outcome
            # active and freshly changed): each outcome's own active flag, and the book's suspended flag, decide
            if meaning is None or not isinstance(market, Mapping):
                continue
            key, line, outcome_names = meaning
            for outcome_id, outcome in (market.get("outcomes") or {}).items():
                priced = _price(outcome) if isinstance(outcome, Mapping) else None
                label = outcome_names.get(str(outcome_id))
                if priced is None or not label:
                    continue
                price, stamp, main = priced
                if stamp:
                    stamps.append(stamp)
                if key == "h2h":
                    name = home if label == "1" else away if label == "2" else "Draw" if label.upper() == "X" else None
                    if name:
                        by_key["h2h"].append({"name": name, "price": price})
                elif key == "totals" and line is not None and label in ("Over", "Under"):
                    by_key["totals"].append({"name": label, "price": price, "point": line})
                elif key == "spreads" and line is not None and label in ("1", "2"):
                    handicaps[line][label] = (price, main)
                elif key == "btts" and label.upper() in ("YES", "NO"):
                    by_key["btts"].append({"name": label.title(), "price": price})
        for key, size in (("h2h", h2h_size(markets)), ("btts", 2)):  # a 1X2 without its draw is not a two-way market: whole or nothing
            if key in by_key and len(by_key[key]) != size:
                del by_key[key]
        line = main_handicap(handicaps)
        if line is not None:  # one Asian handicap line per book, as The Odds API sends it: the book's main line
            by_key["spreads"] = [{"name": home, "price": handicaps[line]["1"][0], "point": line}, {"name": away, "price": handicaps[line]["2"][0], "point": -line}]
        if not by_key:
            continue
        stamp = max(stamps) if stamps else datetime.now(UTC).isoformat()
        bookmakers.append({"key": book, "title": book, "last_update": stamp,
                           "markets": [{"key": k, "last_update": stamp, "outcomes": v} for k, v in by_key.items()]})
    if not bookmakers:
        return None
    return {"id": f"oddspapi:{row['fixtureId']}", "sport_key": sport, "commence_time": row["startTime"], "home_team": home, "away_team": away,
            "bookmakers": bookmakers}
