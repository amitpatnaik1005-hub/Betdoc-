"""Orchestration for bookmaker configuration, comparison and routing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, TypedDict

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.bookmakers.adapters import create_adapter
from app.domain.bookmakers.analytics import find_best_odds
from app.domain.bookmakers.omniroute import (
    OmniRouteClient,
    OmniRouteResponse,
    default_omniroute_client,
)
from app.models.bookmakers import BookmakerConfigModel
from app.schemas.bookmakers import BookmakerConfigCreate, BookmakerConfigUpdate


class BookmakerError(Exception):
    """Base class for bookmaker domain errors."""


class BookmakerConfigNotFoundError(BookmakerError):
    def __init__(self, name: str) -> None:
        self.bookmaker_name = name
        super().__init__(f"Bookmaker config {name!r} was not found.")


class BookmakerConfigConflictError(BookmakerError):
    def __init__(self, name: str) -> None:
        self.bookmaker_name = name
        super().__init__(f"Bookmaker config {name!r} already exists (names are case-insensitive).")


class BookmakerInactiveError(BookmakerError):
    def __init__(self, name: str) -> None:
        self.bookmaker_name = name
        super().__init__(f"Bookmaker {name!r} is not active and cannot receive routed bets.")


DEFAULT_BOOKMAKERS: Final[tuple[Mapping[str, Any], ...]] = (
    {"name": "Stake", "priority_rank": 1, "base_url": "https://stake.com"},
    {"name": "Parimatch", "priority_rank": 2, "base_url": "https://parimatch.com"},
    {"name": "1xBet", "priority_rank": 3, "base_url": "https://1xbet.com"},
    {"name": "Pinnacle", "priority_rank": 4, "base_url": "https://www.pinnacle.com"},
    {"name": "Betfair", "priority_rank": 5, "base_url": "https://www.betfair.com"},
)


class ComparisonOutcome(TypedDict):
    best_bookmaker: str
    best_odds: float
    mean_odds: float
    edge_percentage: float
    placement_instruction: str
    considered_bookmakers: list[str]
    ignored_bookmakers: list[str]
    route: OmniRouteResponse | None


def _unwrap_secret(value: SecretStr | str | None) -> str | None:
    if value is None:
        return None
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    raw = raw.strip()
    return raw or None


def _url_to_str(value: object | None) -> str | None:
    return None if value is None else str(value)


async def has_any_config(db: AsyncSession) -> bool:
    result = await db.execute(select(BookmakerConfigModel.id).limit(1))
    return result.first() is not None


async def seed_default_configs(db: AsyncSession) -> bool:
    """Insert the default bookmakers. Returns False if a concurrent seeder won the race."""
    db.add_all(
        [
            BookmakerConfigModel(
                name=str(entry["name"]),
                is_active=False,
                api_key_encrypted=None,
                base_url=str(entry["base_url"]),
                priority_rank=int(entry["priority_rank"]),
            )
            for entry in DEFAULT_BOOKMAKERS
        ]
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    return True


async def ensure_seeded(db: AsyncSession) -> bool:
    """Seed the defaults when the table is empty. Safe to call on every request."""
    if await has_any_config(db):
        return False
    return await seed_default_configs(db)


async def get_config_by_name(db: AsyncSession, name: str) -> BookmakerConfigModel | None:
    normalized = name.strip().lower()
    if not normalized:
        return None
    stmt = (
        select(BookmakerConfigModel)
        .where(func.lower(BookmakerConfigModel.name) == normalized)
        .limit(1)
    )
    return await db.scalar(stmt)


async def get_all_configs(
    db: AsyncSession, *, active_only: bool = False
) -> list[BookmakerConfigModel]:
    await ensure_seeded(db)
    stmt = select(BookmakerConfigModel).order_by(
        BookmakerConfigModel.priority_rank.asc(),
        func.lower(BookmakerConfigModel.name).asc(),
        BookmakerConfigModel.name.asc(),
    )
    if active_only:
        stmt = stmt.where(BookmakerConfigModel.is_active.is_(True))
    result = await db.scalars(stmt)
    return list(result.all())


async def create_config(db: AsyncSession, payload: BookmakerConfigCreate) -> BookmakerConfigModel:
    await ensure_seeded(db)
    if await get_config_by_name(db, payload.name) is not None:
        raise BookmakerConfigConflictError(payload.name)

    config = BookmakerConfigModel(
        name=payload.name,
        is_active=payload.is_active,
        api_key_encrypted=_unwrap_secret(payload.api_key_encrypted),
        base_url=_url_to_str(payload.base_url),
        priority_rank=payload.priority_rank,
    )
    db.add(config)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise BookmakerConfigConflictError(payload.name) from exc
    await db.refresh(config)
    return config


async def update_config(
    db: AsyncSession, name: str, payload: BookmakerConfigUpdate
) -> BookmakerConfigModel:
    await ensure_seeded(db)
    config = await get_config_by_name(db, name)
    if config is None:
        raise BookmakerConfigNotFoundError(name)

    changes = payload.model_dump(exclude_unset=True)
    new_name: str | None = changes.get("name")
    if new_name is not None and new_name.lower() != config.name.lower():
        if await get_config_by_name(db, new_name) is not None:
            raise BookmakerConfigConflictError(new_name)

    target_name = new_name or config.name
    for field, value in changes.items():
        if field == "api_key_encrypted":
            value = _unwrap_secret(value)
        elif field == "base_url":
            value = _url_to_str(value)
        setattr(config, field, value)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise BookmakerConfigConflictError(target_name) from exc
    await db.refresh(config)
    return config


async def compare_and_route(
    db: AsyncSession,
    market_data: Mapping[str, float],
    *,
    sport: str,
    league: str,
    match: str,
    market: str,
    selection: str,
    execute: bool = False,
    stake: float | None = None,
    omniroute_client: OmniRouteClient | None = None,
) -> ComparisonOutcome:
    if execute and stake is None:
        raise ValueError("stake is required when execute is true.")

    active_configs = await get_all_configs(db, active_only=True)
    best = find_best_odds(market_data, active_configs)
    winner = next(config for config in active_configs if config.name == best["best_bookmaker"])

    adapter = create_adapter(winner.name, winner)
    instruction = adapter.generate_placement_instruction(
        sport, league, match, market, selection, best["best_odds"]
    )

    route: OmniRouteResponse | None = None
    if execute:
        client = omniroute_client if omniroute_client is not None else default_omniroute_client
        route = await client.execute_bet(
            winner.name,
            {"odds": best["best_odds"], "stake": stake, "market": market, "selection": selection},
        )

    return ComparisonOutcome(
        best_bookmaker=best["best_bookmaker"],
        best_odds=best["best_odds"],
        mean_odds=best["mean_market_odds"],
        edge_percentage=best["edge_percentage"],
        placement_instruction=instruction,
        considered_bookmakers=best["considered_bookmakers"],
        ignored_bookmakers=best["ignored_bookmakers"],
        route=route,
    )


async def execute_route(
    db: AsyncSession,
    bookmaker_name: str,
    payload: Mapping[str, Any],
    *,
    omniroute_client: OmniRouteClient | None = None,
) -> OmniRouteResponse:
    await ensure_seeded(db)
    config = await get_config_by_name(db, bookmaker_name)
    if config is None:
        raise BookmakerConfigNotFoundError(bookmaker_name)
    if not config.is_active:
        raise BookmakerInactiveError(config.name)
    client = omniroute_client if omniroute_client is not None else default_omniroute_client
    return await client.execute_bet(config.name, payload)
