"""Odds API responses -> odds_snapshots rows. Fetching lives in the ingestion fleet
(app.adapters.ingestion.odds_api_adapter, scheduled by app.services.omni_fleet)."""

import logging
import math
import uuid
from datetime import datetime
from typing import Any, Iterator

from pydantic import ValidationError
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.odds import OddsSnapshot
from app.schemas.odds import NormalizedMatchOdds

drona = logging.getLogger("betdoc.drona")  # snapshot storage (backtests)
panini = logging.getLogger("betdoc.panini")  # normalisation

# Postgres caps a statement at 65,535 bind params; 11 columns/row -> <= 5,957 rows.
INSERT_CHUNK_ROWS: int = 2000


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _flatten(raw: list[dict[str, Any]], sport: str, snapshot_ts: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    bad_events: int = 0
    bad_prices: int = 0

    for item in raw:
        try:
            event: NormalizedMatchOdds = NormalizedMatchOdds.model_validate(item)
        except ValidationError:
            bad_events += 1
            continue

        for bookmaker in event.bookmakers:
            for market in bookmaker.markets:
                # Normalise market type
                market_type = "Match Odds" if market.key == "h2h" else market.key
                
                for selection in market.selections:
                    if not math.isfinite(selection.price) or selection.price <= 1.0:
                        bad_prices += 1
                        continue
                        
                    # Normalise selection names for PANINI
                    selection_name = selection.name
                    if selection_name == event.home_team:
                        selection_name = "HOME"
                    elif selection_name == event.away_team:
                        selection_name = "AWAY"
                    elif selection_name.lower() == "draw":
                        selection_name = "DRAW"
                        
                    rows.append(
                        {
                            "id": uuid.uuid4(),
                            "match_id": event.id,
                            "sport_key": event.sport_key or sport,
                            "commence_time": event.commence_time,
                            "home_team": event.home_team,
                            "away_team": event.away_team,
                            "bookmaker": bookmaker.key,
                            "market_type": market_type,
                            "selection": selection_name,
                            "odds": selection.price,
                            # One timestamp per poll: the snapshot is a consistent
                            # cross-section, which DRONA's backtests rely on.
                            "timestamp": snapshot_ts,
                            "bookmaker_last_update": bookmaker.last_update
                        }
                    )

    panini.info(
        "PANINI: Normalised sport=%s events=%d rows=%d (rejected events=%d, prices=%d)",
        sport,
        len(raw),
        len(rows),
        bad_events,
        bad_prices,
    )
    return rows


async def store_snapshots(db: AsyncSession, raw: list[dict[str, Any]], sport: str, snapshot_ts: datetime) -> int:
    """Flatten one Odds API response into odds_snapshots rows (DRONA's backtests, the Arena board,
    steam detection). The caller owns the transaction and the scheduling: the ingestion fleet's
    per-source distributed lock replaces the advisory lock this module used to take."""
    rows: list[dict[str, Any]] = _flatten(raw, sport, snapshot_ts)
    for chunk in _chunks(rows, INSERT_CHUNK_ROWS):
        await db.execute(insert(OddsSnapshot).values(chunk))
    drona.info("DRONA: Snapshot staged. sport=%s rows=%d", sport, len(rows))
    return len(rows)
