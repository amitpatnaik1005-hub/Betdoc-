"""The Wire's record (Group 78). Developed for Amit Ashok Kumar Patnaik, BetDoc.

* ``wire_venue_locations``: venues learnt from ESPN's scoreboard (placed by Open-Meteo's geocoder) or entered by an
  administrator, per sport and home side (or tournament). They extend the seed registry in
  ``app/domain/the_wire/venue_geocoder.py`` and win over it.
* ``wire_weather_snapshots``: one row per forecast read for a fixture: the match window's conditions, the friction
  factor and its parts. The latest row is what the fortress's weather pillar was given.
* ``wire_injury_roster_reports``: absences per fixture and side, from ESPN's injury lists or an administrator, with
  the operator's rating of the player (carried to the player's later absences) and the cost it implies.
* ``wire_news_sentiment_articles``: every ingested article: polarity, tactical impact, credibility, the fixtures it
  names, the consensus at ingestion and, when the market followed, the catalyst it became.
* ``wire_officiating_records``: officiated matches (cards, penalties, goals per side), from ESPN once a match ends;
  a fixture's referee as named by ESPN or an administrator.
* ``wire_referee_profiles``: each referee's tendencies per league, recomputed from the records.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models import Base, utc_now

JsonColumn = JSON().with_variant(JSONB(), "postgresql")
ROOFS = ("OPEN_AIR", "RETRACTABLE", "FIXED_DOME", "INDOOR")
SURFACES = ("NATURAL_GRASS", "HYBRID", "ARTIFICIAL_TURF", "HARDCOURT", "CLAY")
ABSENCE_STATUSES = ("OUT", "SUSPENDED", "DOUBTFUL", "QUESTIONABLE")
IMPACTS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
SIDES = ("HOME", "AWAY")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class VenueLocation(Base):
    """A venue the Wire places fixtures at. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "wire_venue_locations"
    __table_args__ = (
        UniqueConstraint("sport", "alias", name="uq_wire_venue_locations_sport_alias"),
        CheckConstraint(_in("roof_type", ROOFS), name="roof_known"),
        CheckConstraint(f"surface_type IS NULL OR {_in('surface_type', SURFACES)}", name="surface_known"),
        CheckConstraint("latitude >= -90 AND latitude <= 90 AND longitude >= -180 AND longitude <= 180", name="coordinates_valid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sport: Mapped[str] = mapped_column(String(32))  # sport key prefix: soccer, americanfootball, ...
    alias: Mapped[str] = mapped_column(String(128))  # the home side (or tournament), normalised
    team_name: Mapped[str] = mapped_column(String(128))
    venue_name: Mapped[str] = mapped_column(String(160))
    city: Mapped[str | None] = mapped_column(String(96), nullable=True)
    country: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    elevation_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    roof_type: Mapped[str] = mapped_column(String(16))
    surface_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    source: Mapped[str] = mapped_column(String(64))  # "espn+open-meteo" or "admin:<name>"
    espn_venue_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WeatherSnapshot(Base):
    """A fixture's match-window forecast and its friction factor. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "wire_weather_snapshots"
    __table_args__ = (
        Index("ix_wire_weather_snapshots_fixture_fetched", "fixture_id", "fetched_at"),
        CheckConstraint(_in("roof_type", ROOFS), name="roof_known"),
        CheckConstraint("pitch_impact_score > 0", name="impact_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[str] = mapped_column(String(128))
    sport_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    home_team: Mapped[str] = mapped_column(String(128))
    away_team: Mapped[str] = mapped_column(String(128))
    kickoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    venue_name: Mapped[str | None] = mapped_column(String(160), nullable=True)  # none for an arena sport
    venue_source: Mapped[str | None] = mapped_column(String(64), nullable=True)  # seed / espn+open-meteo / admin:<name>
    roof_type: Mapped[str] = mapped_column(String(16))
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    elevation_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    window_hours: Mapped[float] = mapped_column(Float)
    temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)  # the window's strongest hour
    wind_direction_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_cardinal: Mapped[str | None] = mapped_column(String(4), nullable=True)
    precipitation_mmh: Mapped[float | None] = mapped_column(Float, nullable=True)  # the window's wettest hour
    snow: Mapped[bool] = mapped_column(Boolean, default=False)
    weather_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    condition: Mapped[str] = mapped_column(String(64))
    pitch_impact_score: Mapped[float] = mapped_column(Float)
    factors: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)
    is_indoor_dome: Mapped[bool] = mapped_column(Boolean, default=False)
    roof_may_close: Mapped[bool] = mapped_column(Boolean, default=False)
    dew_expected: Mapped[bool] = mapped_column(Boolean, default=False)
    tactical_advisory: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InjuryRosterReport(Base):
    """One absence of one side for one fixture. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "wire_injury_roster_reports"
    __table_args__ = (
        UniqueConstraint("fixture_id", "side", "player_name", name="uq_wire_injury_roster_reports_fixture_side_player"),
        Index("ix_wire_injury_roster_reports_player", "sport", "team_name", "player_name"),
        CheckConstraint(_in("side", SIDES), name="side_known"),
        CheckConstraint(_in("status", ABSENCE_STATUSES), name="status_known"),
        CheckConstraint("rating IS NULL OR rating >= 0", name="rating_non_negative"),
        CheckConstraint("replacement_quality IS NULL OR (replacement_quality >= 0 AND replacement_quality < 1)", name="replacement_bounded"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[str] = mapped_column(String(128), index=True)
    sport: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    team_name: Mapped[str] = mapped_column(String(128))
    player_name: Mapped[str] = mapped_column(String(128))
    position: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    injury_nature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    return_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)  # the operator's, 0 .. WIRE_RATING_SCALE
    replacement_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    rated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    impact_delta_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # the win probability it costs the side (points x 100)
    source_name: Mapped[str] = mapped_column(String(64))  # "espn" or "admin:<name>"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # still on the source's latest list
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class NewsArticleSentiment(Base):
    """One ingested article and what the Wire read in it. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "wire_news_sentiment_articles"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_wire_news_sentiment_articles_fingerprint"),
        Index("ix_wire_news_sentiment_articles_published", "published_at"),
        Index("ix_wire_news_sentiment_articles_impact_published", "tactical_impact", "published_at"),
        CheckConstraint(_in("tactical_impact", IMPACTS), name="impact_known"),
        CheckConstraint("sentiment_score >= -1 AND sentiment_score <= 1", name="sentiment_bounded"),
        CheckConstraint("source_credibility >= 0 AND source_credibility <= 1", name="credibility_bounded"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(64))  # sha256 of the normalised URL
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(String(1024))
    source_credibility: Mapped[float] = mapped_column(Float)
    sport: Mapped[str | None] = mapped_column(String(32), nullable=True)  # of the first fixture it names
    teams_mentioned: Mapped[list[Any]] = mapped_column(JsonColumn, default=list)
    players_mentioned: Mapped[list[Any]] = mapped_column(JsonColumn, default=list)  # recorded absentees the text names
    fixtures: Mapped[list[Any]] = mapped_column(JsonColumn, default=list)  # [{fixture_id, team, side}]
    subject: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)  # the side the polarity is about
    events: Mapped[list[Any]] = mapped_column(JsonColumn, default=list)  # ABSENCE, SACKING, DOUBT ...
    sentiment_score: Mapped[float] = mapped_column(Float)
    tactical_impact: Mapped[str] = mapped_column(String(16))
    baseline_probabilities: Mapped[dict[str, Any]] = mapped_column(JsonColumn, default=dict)  # fixture -> selection -> consensus at ingestion
    associated_steam_move_id: Mapped[str | None] = mapped_column(String(160), nullable=True)  # "<fixture>|Match Odds|<selection>"
    catalyst: Mapped[dict[str, Any] | None] = mapped_column(JsonColumn, nullable=True)
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class OfficiatingRecord(Base):
    """One officiated match. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "wire_officiating_records"
    __table_args__ = (
        UniqueConstraint("fixture_id", name="uq_wire_officiating_records_fixture_id"),
        UniqueConstraint("espn_event_id", name="uq_wire_officiating_records_espn_event_id"),
        Index("ix_wire_officiating_records_league_referee", "league", "referee_name"),
        CheckConstraint("status IN ('SCHEDULED', 'FINAL')", name="status_known"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fixture_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    espn_event_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    league: Mapped[str] = mapped_column(String(64))  # ESPN league path ("soccer/eng.1"), else the feed's sport key
    referee_name: Mapped[str | None] = mapped_column(String(96), nullable=True)
    home_team: Mapped[str] = mapped_column(String(128))
    away_team: Mapped[str] = mapped_column(String(128))
    played_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="SCHEDULED")
    home_yellow: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_yellow: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_red: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_red: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_penalties: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_penalties: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_goals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(64))  # "espn", "admin:<name>", "espn+admin:<name>"
    assigned_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class RefereeProfile(Base):
    """A referee's tendencies in one league, from the officiating records. Developed for Amit Ashok Kumar Patnaik."""

    __tablename__ = "wire_referee_profiles"
    __table_args__ = (UniqueConstraint("referee_name", "league", name="uq_wire_referee_profiles_referee_league"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    referee_name: Mapped[str] = mapped_column(String(96))
    league: Mapped[str] = mapped_column(String(64))
    sport: Mapped[str] = mapped_column(String(32))
    matches_officiated: Mapped[int] = mapped_column(Integer)
    avg_yellow_cards: Mapped[float] = mapped_column(Float)
    avg_red_cards: Mapped[float] = mapped_column(Float)
    cards_per_game: Mapped[float] = mapped_column(Float)
    penalties_per_90: Mapped[float] = mapped_column(Float)
    home_bias_ratio: Mapped[float] = mapped_column(Float)
    over_totals_pct: Mapped[float] = mapped_column(Float)
    baseline_matches: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
