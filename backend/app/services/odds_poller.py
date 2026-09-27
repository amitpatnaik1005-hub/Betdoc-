import asyncio
import logging
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

from pydantic import ValidationError
from sqlalchemy import func, insert, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.odds import OddsSnapshot
from app.schemas.odds import NormalizedMatchOdds
from app.services.odds_client import (
    OddsAPIAuthError,
    OddsAPIClient,
    OddsAPIError,
    QuotaExceededError,
)

varahmihir = logging.getLogger("betdoc.varahmihir")  # snapshot storage (backtests)
panini = logging.getLogger("betdoc.panini")  # normalisation

# Postgres caps a statement at 65,535 bind params; 11 columns/row -> <= 5,957 rows.
INSERT_CHUNK_ROWS: int = 2000
# Cluster-wide lock so N uvicorn workers don't each poll (and each burn quota).
POLL_ADVISORY_LOCK_KEY: int = 0x0DD5_0001
_SPORT_KEY_RE = re.compile(r"^[a-z0-9_]+$")


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
                            # cross-section, which VARAHMIHIR's backtests rely on.
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


async def _poll_sport(sport: str) -> None:
    interval: int = settings.ODDS_POLLING_INTERVAL_SEC

    async with AsyncSessionLocal() as db:
        locked: bool = bool(
            (await db.execute(select(func.pg_try_advisory_xact_lock(POLL_ADVISORY_LOCK_KEY)))).scalar_one()
        )
        if not locked:
            return  # another worker is polling right now

        # Freshness check under the lock: skip if another worker polled recently.
        last_ts: datetime | None = (
            await db.execute(
                select(func.max(OddsSnapshot.timestamp)).where(OddsSnapshot.sport_key == sport)
            )
        ).scalar_one_or_none()
        now: datetime = datetime.now(timezone.utc)
        if last_ts is not None and (now - last_ts).total_seconds() < interval * 0.9:
            await db.rollback()
            return

        raw: list[dict[str, Any]] = await OddsAPIClient.fetch_odds(sport)
        rows: list[dict[str, Any]] = _flatten(raw, sport, now)

        if not rows:
            await db.rollback()
            varahmihir.info("VARAHMIHIR: No odds for sport=%s; nothing stored.", sport)
            return

        for chunk in _chunks(rows, INSERT_CHUNK_ROWS):
            await db.execute(insert(OddsSnapshot).values(chunk))
        await db.commit()  # also releases the advisory lock

    varahmihir.info(
        "VARAHMIHIR: Snapshot stored. sport=%s rows=%d quota_remaining=%s",
        sport,
        len(rows),
        OddsAPIClient.requests_remaining(),
    )


async def poll_and_store_odds() -> None:
    """Long-running poll loop. Never raises: failures are logged, never crash the app.

    asyncio.CancelledError is a BaseException, so it bypasses `except Exception`
    and shutdown cancellation still works.
    """
    try:
        if settings.ODDS_API_KEY is None:
            varahmihir.warning("VARAHMIHIR: ODDS_API_KEY not set; odds poller disabled.")
            return

        sports: list[str] = [s for s in settings.odds_sport_keys if _SPORT_KEY_RE.match(s)]
        if not sports:
            varahmihir.error("VARAHMIHIR: No valid ODDS_SPORT_KEYS configured; poller disabled.")
            return

        interval: int = max(10, settings.ODDS_POLLING_INTERVAL_SEC)
        loop = asyncio.get_running_loop()
        varahmihir.info("VARAHMIHIR: Poller online. sports=%s interval=%ss", sports, interval)

        while True:
            started: float = loop.time()
            for sport in sports:
                try:
                    await _poll_sport(sport)
                except QuotaExceededError as exc:
                    varahmihir.critical("VARAHMIHIR: %s. Poller halted.", exc)
                    return
                except OddsAPIAuthError as exc:
                    varahmihir.critical("VARAHMIHIR: %s. Poller halted.", exc)
                    return
                except OddsAPIError as exc:
                    varahmihir.error("VARAHMIHIR: Poll failed: %s", exc)  # sanitised message
                except Exception:
                    varahmihir.exception("VARAHMIHIR: Unexpected poll failure for sport=%s", sport)

            elapsed: float = loop.time() - started
            await asyncio.sleep(max(1.0, interval - elapsed))

    except Exception:
        varahmihir.exception("VARAHMIHIR: Poller crashed; odds ingestion stopped.")
