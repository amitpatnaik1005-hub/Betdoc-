"""The sports desk's catalogue in the database (Group 79). Developed for Amit Ashok Kumar Patnaik.

``ensure_seeded`` inserts any seed sport or league that is missing (``INSERT ... ON CONFLICT DO NOTHING`` on
PostgreSQL and SQLite), so a fresh database, a concurrent first request and a later seed addition all converge on
the same rows without touching what an administrator changed. ``model_context`` merges a sport's stored model
parameters under the request's own context: the request wins, the desk's defaults fill the gaps.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sports.errors import InvalidSportConfigError, SportNotFoundError
from app.domain.sports.registry import SportAdapterRegistry
from app.domain.sports.seed import league_rows, sport_rows
from app.domain.sports.settlement import SettlementInputError
from app.models import utc_now
from app.models.sports_config import LeagueRegistry, SportConfiguration
from app.schemas.sports_config import LeagueCreate, LeagueUpdate, SportConfigUpdate


def _insert(session: AsyncSession, model: Any) -> Any:
    dialect = session.get_bind().dialect.name
    return (postgresql.insert(model) if dialect == "postgresql" else sqlite.insert(model)).on_conflict_do_nothing()


async def ensure_seeded(session: AsyncSession) -> int:
    """Insert missing seed rows; returns how many were added. Commits when anything was added."""
    have = set((await session.execute(select(SportConfiguration.sport_key))).scalars())
    leagues_have = set((await session.execute(select(LeagueRegistry.league_key))).scalars())
    now = utc_now()
    added = 0
    for row in sport_rows():
        if row["sport_key"] in have:
            continue
        result = await session.execute(_insert(session, SportConfiguration).values(id=str(uuid.uuid4()), created_at=now, updated_at=now, **row))
        added += result.rowcount or 0
    for row in league_rows():
        if row["league_key"] in leagues_have:
            continue
        result = await session.execute(_insert(session, LeagueRegistry).values(id=str(uuid.uuid4()), created_at=now, updated_at=now, **row))
        added += result.rowcount or 0
    if added:
        await session.commit()
    return added


async def list_configs(session: AsyncSession) -> list[SportConfiguration]:
    await ensure_seeded(session)
    return list((await session.execute(select(SportConfiguration).order_by(SportConfiguration.sport_key))).scalars())


async def get_config(session: AsyncSession, sport_key: str) -> SportConfiguration:
    await ensure_seeded(session)
    row = (await session.execute(select(SportConfiguration).where(SportConfiguration.sport_key == sport_key.strip().lower())
                                 .execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        raise SportNotFoundError(sport_key)
    return row


async def update_config(session: AsyncSession, sport_key: str, payload: SportConfigUpdate) -> SportConfiguration:
    row = await get_config(session, sport_key)
    if payload.supported_markets is not None:
        adapter = SportAdapterRegistry.get_adapter(row.sport_key)
        for market in payload.supported_markets:
            try:
                adapter.canonical_market(market)
            except SettlementInputError as exc:
                raise InvalidSportConfigError(str(exc)) from exc
        row.supported_markets = list(dict.fromkeys(payload.supported_markets))
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.default_kelly_fraction is not None:
        row.default_kelly_fraction = payload.default_kelly_fraction
    if payload.min_market_liquidity is not None:
        row.min_market_liquidity = payload.min_market_liquidity
    if payload.primary_odds_api_sport_key is not None:
        row.primary_odds_api_sport_key = payload.primary_odds_api_sport_key
    if payload.math_model_parameters is not None:
        row.math_model_parameters = dict(payload.math_model_parameters)
    row.updated_at = utc_now()
    await session.commit()
    await session.refresh(row)
    return row


async def list_leagues(session: AsyncSession, sport_key: str, *, active_only: bool = False) -> list[LeagueRegistry]:
    await ensure_seeded(session)
    query = select(LeagueRegistry).where(LeagueRegistry.sport_key == sport_key.strip().lower())
    if active_only:
        query = query.where(LeagueRegistry.is_active.is_(True))
    return list((await session.execute(query.order_by(LeagueRegistry.tier, LeagueRegistry.display_name))).scalars())


async def create_league(session: AsyncSession, payload: LeagueCreate) -> LeagueRegistry:
    await get_config(session, payload.sport_key)  # the parent sport must exist
    if (await session.execute(select(LeagueRegistry.id).where(LeagueRegistry.league_key == payload.league_key))).first() is not None:
        raise InvalidSportConfigError(f"league {payload.league_key!r} is already registered")
    now = utc_now()
    row = LeagueRegistry(id=str(uuid.uuid4()), created_at=now, updated_at=now, **{**payload.model_dump(), "sport_key": payload.sport_key.lower()})
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_league(session: AsyncSession, league_key: str, payload: LeagueUpdate) -> LeagueRegistry:
    await ensure_seeded(session)
    row = (await session.execute(select(LeagueRegistry).where(LeagueRegistry.league_key == league_key))).scalar_one_or_none()
    if row is None:
        raise SportNotFoundError(league_key)
    for name, value in payload.model_dump(exclude_unset=True).items():
        setattr(row, name, value)
    row.updated_at = utc_now()
    await session.commit()
    await session.refresh(row)
    return row


def model_context(config: SportConfiguration | None, context: Mapping[str, Any] | None) -> dict[str, Any]:
    """The desk's stored model parameters under the request's context (the request's keys win)."""
    base = {k: v for k, v in (config.math_model_parameters if config is not None else {}).items() if k != "model"}
    return {**base, **dict(context or {})}
