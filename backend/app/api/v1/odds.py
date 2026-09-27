import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import DbSession, get_current_user
from app.core.config import settings
from app.models.odds import OddsSnapshot
from app.models import User
from app.schemas.odds import (
    NormalizedMatchOdds,
    OddsBookmaker,
    OddsMarket,
    OddsMovement,
    OddsSelection,
)

prithviraj = logging.getLogger("betdoc.prithviraj")  # steam detection

router = APIRouter(tags=["odds"])

SPORT_KEY_PATTERN: str = r"^[a-z0-9_]+$"


def _stale_after() -> timedelta:
    return timedelta(seconds=max(5 * settings.ODDS_POLLING_INTERVAL_SEC, 300))


def _rebuild(rows: list[OddsSnapshot]) -> list[NormalizedMatchOdds]:
    """Reassemble the nested API shape from flat snapshot rows (ordered by the query)."""
    meta: dict[str, OddsSnapshot] = {}
    books: dict[str, dict[str, dict[str, list[OddsSelection]]]] = {}
    books_ts: dict[str, dict[str, datetime]] = {}

    for r in rows:
        meta.setdefault(r.match_id, r)
        books_ts.setdefault(r.match_id, {}).setdefault(r.bookmaker, r.bookmaker_last_update or r.timestamp)
        (
            books.setdefault(r.match_id, {})
            .setdefault(r.bookmaker, {})
            .setdefault(r.market_type, [])
            .append(OddsSelection(name=r.selection, price=r.odds))
        )

    result: list[NormalizedMatchOdds] = []
    for match_id, m in meta.items():
        ts: datetime = m.timestamp
        bookmakers: list[OddsBookmaker] = [
            OddsBookmaker(
                key=bk,
                title=bk,  # title is not persisted; the key is the stable identifier
                last_update=books_ts[match_id][bk],
                markets=[
                    OddsMarket(key=mk, last_update=books_ts[match_id][bk], selections=sels)
                    for mk, sels in markets.items()
                ],
            )
            for bk, markets in books[match_id].items()
        ]
        result.append(
            NormalizedMatchOdds(
                id=match_id,
                sport_key=m.sport_key,
                commence_time=m.commence_time,
                home_team=m.home_team,
                away_team=m.away_team,
                bookmakers=bookmakers,
            )
        )
    return result


@router.get("/live", response_model=list[NormalizedMatchOdds])
async def get_live_odds(
    db: DbSession,
    current_user: User = Depends(get_current_user),
    sport: str | None = Query(default=None, pattern=SPORT_KEY_PATTERN),
) -> list[NormalizedMatchOdds]:
    configured: list[str] = settings.odds_sport_keys
    if sport is None and not configured:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No sport specified or configured")
    sport_key: str = sport or configured[0]

    latest_ts: datetime | None = (
        await db.execute(
            select(func.max(OddsSnapshot.timestamp)).where(OddsSnapshot.sport_key == sport_key)
        )
    ).scalar_one_or_none()

    if latest_ts is None:
        return []

    # Never serve silently stale prices (e.g. poller halted on quota).
    if datetime.now(timezone.utc) - latest_ts > _stale_after():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Odds snapshot for {sport_key} is stale (last update {latest_ts.isoformat()})",
        )

    rows: list[OddsSnapshot] = list(
        (
            await db.execute(
                select(OddsSnapshot)
                .where(
                    OddsSnapshot.sport_key == sport_key,
                    OddsSnapshot.timestamp == latest_ts,
                )
                # Deterministic order so clients can deep-compare successive responses.
                .order_by(
                    OddsSnapshot.commence_time,
                    OddsSnapshot.match_id,
                    OddsSnapshot.bookmaker,
                    OddsSnapshot.market_type,
                    OddsSnapshot.selection,
                )
            )
        ).scalars().all()
    )
    return _rebuild(rows)


@router.get("/movements", response_model=list[OddsMovement])
async def get_movements(
    db: DbSession,
    current_user: User = Depends(get_current_user),
    match_id: str = Query(..., min_length=1, max_length=128),
    bookmaker: str | None = Query(default=None, max_length=64),
    market_type: str = Query(default="Match Odds", max_length=32),
    since: datetime | None = Query(default=None),
    changes_only: bool = Query(default=True),
    limit: int = Query(default=5000, ge=1, le=20000),
) -> list[OddsMovement]:
    stmt = select(
        OddsSnapshot.bookmaker,
        OddsSnapshot.market_type,
        OddsSnapshot.selection,
        OddsSnapshot.odds,
        OddsSnapshot.timestamp,
    ).where(
        OddsSnapshot.match_id == match_id,
        OddsSnapshot.market_type == market_type,
    )
    if bookmaker is not None:
        stmt = stmt.where(OddsSnapshot.bookmaker == bookmaker)
    if since is not None:
        stmt = stmt.where(OddsSnapshot.timestamp >= since)

    # Newest first so `limit` truncates the OLDEST history, never the latest moves.
    stmt = stmt.order_by(OddsSnapshot.timestamp.desc()).limit(limit)
    rows = (await db.execute(stmt)).all()

    ordered = sorted(rows, key=lambda r: (r.bookmaker, r.selection, r.timestamp))
    points: list[OddsMovement] = []
    last_price: dict[tuple[str, str], float] = {}
    for r in ordered:
        key: tuple[str, str] = (r.bookmaker, r.selection)
        if changes_only and last_price.get(key) == r.odds:
            continue
        last_price[key] = r.odds
        points.append(
            OddsMovement(
                bookmaker=r.bookmaker,
                market_type=r.market_type,
                selection=r.selection,
                odds=r.odds,
                timestamp=r.timestamp,
            )
        )

    prithviraj.info(
        "PRITHVIRAJ: Steam detection query executed. user_id=%s match_id=%s bookmaker=%s "
        "rows=%d points=%d",
        current_user.id,
        match_id,
        bookmaker or "*",
        len(rows),
        len(points),
    )
    return points
