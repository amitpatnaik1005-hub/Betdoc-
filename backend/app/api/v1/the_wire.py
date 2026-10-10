"""The Wire under ``/api/v1/the-wire`` (Group 39; Group 78 made it the fortress's feed). Developed for Amit Ashok Kumar Patnaik.

    GET   /the-wire/dashboard?match_ids=a,b         news (scored), scores (ESPN's clock where paired), venue weather, catalysts
    GET   /the-wire/fixtures                        the tracked fixtures and what the Wire knows of each
    GET   /the-wire/news?impact=&fixture_id=        stored articles, newest first
    GET   /the-wire/catalysts?hours=24              news the market followed
    GET   /the-wire/weather/{fixture_id}            the latest snapshot and the ones before it
    GET   /the-wire/venues                          venues learnt or entered, and the seed's size
    POST  /the-wire/venues                          enter a venue (admin): it wins over the seed and ESPN
    GET   /the-wire/absences/{fixture_id}           absences, ratings, the lineup delta and the adjusted probabilities
    POST  /the-wire/absences                        add an absence (admin)
    PATCH /the-wire/absences/{absence_id}           rate it (admin); the rating carries to the player's later absences
    POST  /the-wire/absences/{fixture_id}/publish   write the injury section now (admin; every absence rated)
    GET   /the-wire/referees?league=                profiles
    GET   /the-wire/referees/{name}?league=         one referee: profile and records
    POST  /the-wire/referees/assign                 name a fixture's referee (admin)
    POST  /the-wire/referees/records                import past officiating records with their source (admin)
    POST  /the-wire/scan?parts=news,espn,weather    run the scans now (admin)

Live frames: ``/api/v1/ws/the-wire`` (``app/api/v1/wire_ws.py``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentAdmin, CurrentUser
from app.api.v1.cfo_execution import get_session_factory
from app.core.config import Settings, get_settings
from app.domain.the_wire import WireAggregatorService
from app.domain.the_wire import venue_geocoder as geo
from app.domain.the_wire.live_providers import OddsApiScoreProvider, RssNewsProvider
from app.models.the_wire import InjuryRosterReport, NewsArticleSentiment, OfficiatingRecord, RefereeProfile, VenueLocation, WeatherSnapshot
from app.schemas.the_wire import AbsenceIn, AbsenceRating, OfficiatingImport, RefereeAssignment, VenueIn, WireDashboard
from app.services.the_wire import espn_sync, lineups, news, referees, weather
from app.services.the_wire.fixtures import Fixture, tracked
from app.services.the_wire.providers import EspnScoreProvider, SnapshotWeatherProvider, StoredNewsProvider
from app.services.twin.intel import read_intel
from app.services.twin.vetting import developer_credit

MAX_MATCH_IDS: Final[int] = 50
MAX_MATCH_ID_LENGTH: Final[int] = 64

router = APIRouter(tags=["the_wire"])

AppSettings = Annotated[Settings, Depends(get_settings)]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def _redis(request: Request) -> Redis | None:
    return getattr(request.app.state, "redis", None)


def _odds(settings: Settings) -> OddsApiScoreProvider:
    return OddsApiScoreProvider(settings.ODDS_API_KEY.get_secret_value() if settings.ODDS_API_KEY else None, settings.ODDS_API_BASE_URL, settings.odds_sport_keys)


async def _fixture(request: Request, settings: Settings, fixture_id: str) -> Fixture:
    for f in await tracked(_redis(request), settings, datetime.now(UTC)):
        if f.fixture_id == fixture_id:
            return f
    raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "NOT_TRACKED", "message": "The live board does not carry this fixture (in play or within the horizon)"})


async def _credit(sessions: async_sessionmaker[AsyncSession]) -> str:
    async with sessions() as session:
        return await developer_credit(session)


# ================================================================ the dashboard
@router.get("/dashboard", response_model=WireDashboard)
async def get_wire_dashboard(
    request: Request, current_user: CurrentUser, sessions: Sessions, settings: AppSettings,  # noqa: ARG001 - current_user enforces authentication
    match_ids: Annotated[str, Query(description="Comma-separated match IDs", max_length=4096)] = "",
) -> WireDashboard:
    ids = list(dict.fromkeys(m.strip() for m in match_ids.split(",") if m.strip()))
    if len(ids) > MAX_MATCH_IDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"At most {MAX_MATCH_IDS} match IDs are allowed per request")
    if any(len(m) > MAX_MATCH_ID_LENGTH for m in ids):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Match IDs must be at most {MAX_MATCH_ID_LENGTH} characters")
    service = WireAggregatorService(
        news_provider=StoredNewsProvider(sessions, settings, RssNewsProvider(settings.WIRE_NEWS_FEEDS)),
        score_provider=EspnScoreProvider(_odds(settings), _redis(request), settings),
        weather_provider=SnapshotWeatherProvider(sessions),
    )
    board = await service.fetch_dashboard(ids)
    since = datetime.now(UTC) - timedelta(hours=24)
    async with sessions() as session:
        rows = (await session.execute(select(NewsArticleSentiment).where(NewsArticleSentiment.catalyst.is_not(None), NewsArticleSentiment.published_at >= since)
                                      .order_by(NewsArticleSentiment.published_at.desc()).limit(20))).scalars()
        found = [c for r in rows if (c := news.catalyst_alert(r)) is not None]
        credit = await developer_credit(session)
    return board.model_copy(update={"catalysts": found, "developer_credit": credit})


@router.get("/fixtures")
async def get_fixtures(request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    redis = _redis(request)
    fixtures = await tracked(redis, settings, datetime.now(UTC))
    ids = [f.fixture_id for f in fixtures]
    links = await espn_sync.read_links(redis, settings, ids)
    intel = (await read_intel(redis, settings, ids)) if redis is not None and ids else {}
    async with sessions() as session:
        snaps = await weather.latest(session, ids)
        absences = dict((await session.execute(select(InjuryRosterReport.fixture_id, func.count()).where(
            InjuryRosterReport.fixture_id.in_(ids), InjuryRosterReport.is_active.is_(True)).group_by(InjuryRosterReport.fixture_id))).all()) if ids else {}
        refs = {r.fixture_id: r.referee_name for r in (await session.execute(select(OfficiatingRecord).where(OfficiatingRecord.fixture_id.in_(ids)))).scalars()} if ids else {}
        credit = await developer_credit(session)
    rows = []
    for f in fixtures:
        link, snap, ev = links.get(f.fixture_id), snaps.get(f.fixture_id), intel.get(f.fixture_id)
        rows.append({
            "fixture_id": f.fixture_id, "home": f.home, "away": f.away, "sport_key": f.sport_key, "kickoff": f.kickoff.isoformat(),
            "espn": None if link is None else {k: link.get(k) for k in ("event_id", "league", "status", "home_score", "away_score", "clock", "detail", "venue")},
            "weather": None if snap is None else weather.report_of(snap).model_dump(mode="json"),
            "absences": int(absences.get(f.fixture_id, 0)), "referee": refs.get(f.fixture_id),
            "intel_sections": [] if ev is None else sorted(ev.model_dump(exclude_none=True)),
        })
    return {"fixtures": rows, "developer_credit": credit}


@router.get("/news")
async def get_news(user: CurrentUser, sessions: Sessions, settings: AppSettings,  # noqa: ARG001
                   impact: Annotated[str | None, Query(pattern="^(CRITICAL|HIGH|MEDIUM|LOW)$")] = None,
                   fixture_id: Annotated[str | None, Query(max_length=128)] = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, Any]:
    stmt = select(NewsArticleSentiment).order_by(NewsArticleSentiment.published_at.desc())
    if impact:
        stmt = stmt.where(NewsArticleSentiment.tactical_impact == impact)
    async with sessions() as session:
        rows = list((await session.execute(stmt.limit(limit if fixture_id is None else 2000))).scalars())
    if fixture_id:
        rows = [r for r in rows if any(f.get("fixture_id") == fixture_id for f in r.fixtures or [])][:limit]
    return {"articles": [news.article_dict(r) for r in rows]}


@router.get("/catalysts")
async def get_catalysts(user: CurrentUser, sessions: Sessions, hours: Annotated[float, Query(gt=0, le=24 * 30)] = 24.0) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        rows = (await session.execute(select(NewsArticleSentiment).where(NewsArticleSentiment.catalyst.is_not(None),
                                                                         NewsArticleSentiment.published_at >= datetime.now(UTC) - timedelta(hours=hours))
                                      .order_by(NewsArticleSentiment.published_at.desc()))).scalars()
        return {"catalysts": [c.model_dump(mode="json") for r in rows if (c := news.catalyst_alert(r)) is not None]}


# ================================================================ weather and venues
@router.get("/weather/{fixture_id}")
async def get_weather(fixture_id: str, user: CurrentUser, sessions: Sessions) -> dict[str, Any]:  # noqa: ARG001
    async with sessions() as session:
        rows = list((await session.execute(select(WeatherSnapshot).where(WeatherSnapshot.fixture_id == fixture_id)
                                           .order_by(WeatherSnapshot.fetched_at.desc()).limit(12))).scalars())
        credit = await developer_credit(session)
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "NO_SNAPSHOT", "message": "No forecast has been taken for this fixture (no venue, or not yet scanned)"})
    return {"latest": weather.snapshot_dict(rows[0]), "history": [weather.snapshot_dict(r) for r in rows[1:]], "developer_credit": credit}


def _venue_dict(v: VenueLocation) -> dict[str, Any]:
    return {"id": str(v.id), "sport": v.sport, "team": v.team_name, "venue": v.venue_name, "city": v.city, "country": v.country, "latitude": v.latitude,
            "longitude": v.longitude, "elevation_m": v.elevation_m, "roof_type": v.roof_type, "surface_type": v.surface_type, "source": v.source,
            "updated_at": v.updated_at.isoformat() if v.updated_at else None}


@router.get("/venues")
async def get_venues(user: CurrentUser, sessions: Sessions, sport: Annotated[str | None, Query(max_length=32)] = None) -> dict[str, Any]:  # noqa: ARG001
    stmt = select(VenueLocation).order_by(VenueLocation.sport, VenueLocation.team_name)
    if sport:
        stmt = stmt.where(VenueLocation.sport == sport)
    async with sessions() as session:
        rows = list((await session.execute(stmt)).scalars())
    seed = [v for v in geo.SEED_VENUES if not sport or v.sport == sport]
    return {"stored": [_venue_dict(v) for v in rows], "seed": [{**v.as_dict(), "teams": list(v.teams)} for v in seed]}


@router.post("/venues", status_code=status.HTTP_201_CREATED)
async def post_venue(body: VenueIn, admin: CurrentAdmin, sessions: Sessions) -> dict[str, Any]:
    now = datetime.now(UTC)
    family, alias = body.sport.casefold(), geo.normalize(body.team_name)
    async with sessions() as session:
        row = (await session.execute(select(VenueLocation).where(VenueLocation.sport == family, VenueLocation.alias == alias))).scalars().first()
        if row is None:
            row = VenueLocation(id=uuid.uuid4(), sport=family, alias=alias, created_at=now)
            session.add(row)
        row.team_name, row.venue_name, row.city, row.country = body.team_name, body.venue_name, body.city, body.country
        row.latitude, row.longitude, row.elevation_m, row.roof_type, row.surface_type = body.latitude, body.longitude, body.elevation_m, body.roof_type, body.surface_type
        row.source, row.espn_venue_id, row.updated_at = f"admin:{admin.username}"[:64], None, now
        await session.commit()
        return {"venue": _venue_dict(row), "developer_credit": await developer_credit(session)}


# ================================================================ absences
async def _probabilities(request: Request, settings: Settings, fixture_id: str) -> dict[str, float] | None:
    return (await news.consensus(_redis(request), settings, [fixture_id], datetime.now(UTC))).get(fixture_id)


@router.get("/absences/{fixture_id}")
async def get_absences(fixture_id: str, request: Request, user: CurrentUser, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:  # noqa: ARG001
    fixture = await _fixture(request, settings, fixture_id)
    async with sessions() as session:
        rows = await lineups.rows_for(session, fixture_id)
    return lineups.view(rows, fixture, settings, await _probabilities(request, settings, fixture_id))


@router.post("/absences", status_code=status.HTTP_201_CREATED)
async def post_absence(body: AbsenceIn, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    if body.rating is not None and body.rating > settings.WIRE_RATING_SCALE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "RATING_RANGE", "message": f"ratings run 0 .. {settings.WIRE_RATING_SCALE:g}"})
    fixture = await _fixture(request, settings, body.fixture_id)
    now = datetime.now(UTC)
    async with sessions() as session:
        row = await lineups.add_manual(session, fixture, body, admin.username, admin.id, now)
        published = await lineups.publish_intel(session, _redis(request), settings, fixture, now, source=f"admin:{admin.username}")
        await session.commit()
        return {"absence": lineups.row_dict(row), "injury_section": published, "developer_credit": await developer_credit(session)}


@router.patch("/absences/{absence_id}")
async def rate_absence(absence_id: uuid.UUID, body: AbsenceRating, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    if body.rating > settings.WIRE_RATING_SCALE:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": "RATING_RANGE", "message": f"ratings run 0 .. {settings.WIRE_RATING_SCALE:g}"})
    now = datetime.now(UTC)
    async with sessions() as session:
        row = await session.get(InjuryRosterReport, absence_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such absence")
        row.rating, row.rated_by, row.updated_at = body.rating, admin.id, now
        if body.replacement_quality is not None:
            row.replacement_quality = body.replacement_quality
        await session.flush()
        published: dict[str, Any] = {"written": False, "reason": "NOT_TRACKED"}
        fixture = next((f for f in await tracked(_redis(request), settings, now) if f.fixture_id == row.fixture_id), None)
        if fixture is not None:
            published = await lineups.publish_intel(session, _redis(request), settings, fixture, now, source=f"wire-ratings:{admin.username}")
        await session.commit()
        return {"absence": lineups.row_dict(row), "injury_section": published}


@router.post("/absences/{fixture_id}/publish")
async def publish_absences(fixture_id: str, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    fixture = await _fixture(request, settings, fixture_id)
    async with sessions() as session:
        result = await lineups.publish_intel(session, _redis(request), settings, fixture, datetime.now(UTC), source=f"admin:{admin.username}")
        await session.commit()
    if not result["written"]:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": "The injury section was not written", **result})
    return result


# ================================================================ referees
def _profile_dict(p: RefereeProfile) -> dict[str, Any]:
    return {"referee": p.referee_name, "league": p.league, "sport": p.sport, "matches": p.matches_officiated, "avg_yellow_cards": p.avg_yellow_cards,
            "avg_red_cards": p.avg_red_cards, "cards_per_game": p.cards_per_game, "penalties_per_90": p.penalties_per_90, "home_bias_ratio": p.home_bias_ratio,
            "over_totals_pct": p.over_totals_pct, "baseline_matches": p.baseline_matches, "updated_at": p.updated_at.isoformat() if p.updated_at else None}


@router.get("/referees")
async def get_referees(user: CurrentUser, sessions: Sessions, settings: AppSettings, league: Annotated[str | None, Query(max_length=64)] = None) -> dict[str, Any]:  # noqa: ARG001
    stmt = select(RefereeProfile).order_by(RefereeProfile.league, RefereeProfile.matches_officiated.desc())
    if league:
        stmt = stmt.where(RefereeProfile.league == league)
    async with sessions() as session:
        rows = list((await session.execute(stmt)).scalars())
        leagues = dict((await session.execute(select(OfficiatingRecord.league, func.count()).where(OfficiatingRecord.status == "FINAL").group_by(OfficiatingRecord.league))).all())
    return {"profiles": [{**_profile_dict(r), "fortress_ready": r.matches_officiated >= settings.WIRE_REFEREE_MIN_MATCHES} for r in rows],
            "records_per_league": leagues, "min_matches": settings.WIRE_REFEREE_MIN_MATCHES}


@router.get("/referees/{name}")
async def get_referee(name: str, user: CurrentUser, sessions: Sessions, settings: AppSettings, league: Annotated[str | None, Query(max_length=64)] = None) -> dict[str, Any]:  # noqa: ARG001
    key = " ".join(name.split()).casefold()
    async with sessions() as session:
        recs = [r for r in (await session.execute(select(OfficiatingRecord).where(func.lower(OfficiatingRecord.referee_name) == key)
                                                   .order_by(OfficiatingRecord.played_at.desc()))).scalars() if league is None or r.league == league]
        if not recs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, {"reason": "NO_RECORDS", "message": "No officiating record names this referee"})
        profiles = []
        for lg in sorted({r.league for r in recs}):
            prof = referees.compute(await referees.league_records(session, lg), recs[0].referee_name or name, lg, settings)
            if prof is not None:
                profiles.append(prof.as_dict())
        credit = await developer_credit(session)
    return {"referee": recs[0].referee_name, "profiles": profiles, "records": [referees.record_dict(r) for r in recs[:100]], "developer_credit": credit}


@router.post("/referees/assign")
async def assign_referee(body: RefereeAssignment, request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    fixture = await _fixture(request, settings, body.fixture_id)
    now = datetime.now(UTC)
    async with sessions() as session:
        row = await referees.assign(session, settings, fixture, body.referee_name, admin.username, admin.id, now)
        written = await referees.write_intel_for(session, _redis(request), settings, [fixture], now)
        await session.commit()
        prof = referees.compute(await referees.league_records(session, row.league), row.referee_name or "", row.league, settings)
    return {"record": referees.record_dict(row), "profile": None if prof is None else prof.as_dict(), "referee_section_written": bool(written)}


@router.post("/referees/records", status_code=status.HTTP_201_CREATED)
async def import_referee_records(body: OfficiatingImport, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with sessions() as session:
        count = await referees.import_records(session, body, admin.username, admin.id, now)
        profiles = []
        for league in sorted({r.league for r in body.records}):
            profiles += [p.as_dict() for p in await referees.refresh(session, settings, league, now)]
        await session.commit()
        return {"imported": count, "profiles": profiles, "developer_credit": await developer_credit(session)}


# ================================================================ scans on demand
@router.post("/scan")
async def scan_now(request: Request, admin: CurrentAdmin, sessions: Sessions, settings: AppSettings,  # noqa: ARG001
                   parts: Annotated[str, Query(pattern="^(news|espn|weather)(,(news|espn|weather))*$")] = "news,espn,weather") -> dict[str, Any]:
    redis, now, out = _redis(request), datetime.now(UTC), {}
    wanted = parts.split(",")
    if "espn" in wanted:
        out["espn"] = await espn_sync.sync(sessions, redis, settings, now)
    if "weather" in wanted:
        out["weather"] = await weather.scan(sessions, redis, settings, now)
    if "news" in wanted:
        out["news"] = await news.scan(sessions, redis, settings, now)
    return {**out, "developer_credit": await _credit(sessions)}
