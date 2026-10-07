"""SportsManager: loads per-sport configuration from the database and drives the math modules."""

import logging
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.sports import basketball, cricket, tennis
from app.domain.sports.errors import (
    InvalidSportConfigError,
    SportInactiveError,
    SportNotFoundError,
    SportsDomainError,
)
from app.models.sports import CONFIG_JSON_MAX_LENGTH, SportConfigModel
from app.schemas.sports import SPORT_CONFIG_SCHEMAS, BasketballConfig, CricketConfig, TennisConfig

logger = logging.getLogger("betdoc.sports")

ConfigT = TypeVar("ConfigT", bound=BaseModel)
ResultT = TypeVar("ResultT")


def _run(fn: Callable[..., ResultT], **kwargs: Any) -> ResultT:
    try:
        return fn(**kwargs)
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise SportsDomainError(str(exc)) from exc


class SportsManager:
    # ------------------------------------------------------------------ configuration

    @staticmethod
    def _normalize(sport_name: str) -> str:
        sport = (sport_name or "").strip().lower()
        if sport not in SPORT_CONFIG_SCHEMAS:
            raise SportNotFoundError(sport_name)
        return sport

    @staticmethod
    def _serialize(config: BaseModel) -> str:
        encoded = config.model_dump_json()
        if len(encoded) > CONFIG_JSON_MAX_LENGTH:
            raise InvalidSportConfigError(f"Configuration exceeds {CONFIG_JSON_MAX_LENGTH} characters.")
        return encoded

    async def _find(self, db: AsyncSession, sport: str) -> SportConfigModel | None:
        result = await db.execute(
            select(SportConfigModel)
            .where(SportConfigModel.sport_name == sport)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_config(self, db: AsyncSession, sport_name: str) -> SportConfigModel:
        sport = self._normalize(sport_name)
        row = await self._find(db, sport)
        if row is None:
            neutral = SPORT_CONFIG_SCHEMAS[sport]()
            candidate = SportConfigModel(sport_name=sport, is_active=True, config_json=self._serialize(neutral))
            db.add(candidate)
            try:
                await db.commit()
                row = candidate
                logger.info("SPORTS: seeded neutral %s configuration.", sport)
            except IntegrityError:
                await db.rollback()
                logger.warning("SPORTS: concurrent %s seed detected; loading the existing row.", sport)
                row = await self._find(db, sport)
                if row is None:
                    raise SportsDomainError(f"Configuration for {sport!r} could not be created or loaded.") from None
        await db.refresh(row)
        return row

    def parse_config(self, row: SportConfigModel) -> BaseModel:
        schema = SPORT_CONFIG_SCHEMAS[self._normalize(row.sport_name)]
        try:
            return schema.model_validate_json(row.config_json)
        except ValidationError as exc:
            raise InvalidSportConfigError(f"Stored {row.sport_name} configuration is invalid.") from exc

    async def _active_config(self, db: AsyncSession, sport: str, expected: type[ConfigT]) -> ConfigT:
        row = await self.get_config(db, sport)
        if not row.is_active:
            raise SportInactiveError(sport)
        config = self.parse_config(row)
        if not isinstance(config, expected):
            raise InvalidSportConfigError(f"Configuration for {sport!r} has an unexpected shape.")
        return config

    async def update_config(
        self, db: AsyncSession, sport_name: str, config_payload: Mapping[str, Any], is_active: bool
    ) -> SportConfigModel:
        sport = self._normalize(sport_name)
        schema = SPORT_CONFIG_SCHEMAS[sport]
        try:
            config = schema.model_validate(dict(config_payload))
        except ValidationError as exc:
            raise InvalidSportConfigError(
                f"Invalid {sport} configuration ({exc.error_count()} error(s))."
            ) from exc

        row = await self.get_config(db, sport)
        row.config_json = self._serialize(config)
        row.is_active = is_active
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise SportsDomainError(f"{sport} configuration rejected by storage constraints.") from exc
        await db.refresh(row)
        logger.info("SPORTS: %s configuration updated (active=%s).", sport, is_active)
        return row

    # ------------------------------------------------------------------ cricket

    async def calculate_dls(self, db: AsyncSession, resources_left_pct: float, original_target: float) -> dict[str, Any]:
        config = await self._active_config(db, "cricket", CricketConfig)
        par_score = _run(
            cricket.calculate_dls_par_score,
            resources_left_pct=resources_left_pct,
            original_target=original_target,
            dls_weight_factor=config.dls_weight_factor,
        )
        return {
            "par_score": par_score,
            "original_target": original_target,
            "resources_left_pct": resources_left_pct,
            "dls_weight_factor": config.dls_weight_factor,
        }

    async def project_first_innings(
        self,
        db: AsyncSession,
        current_score: float,
        overs_bowled: float,
        wickets_lost: int,
        pitch_degradation_factor: float,
        total_overs: float | None = None,
    ) -> dict[str, Any]:
        config = await self._active_config(db, "cricket", CricketConfig)
        effective_total_overs = config.total_overs if total_overs is None else total_overs
        projected = _run(
            cricket.calculate_first_innings_expectation,
            current_score=current_score,
            overs_bowled=overs_bowled,
            wickets_lost=wickets_lost,
            pitch_degradation_factor=pitch_degradation_factor,
            total_overs=effective_total_overs,
            wickets_per_innings=config.wickets_per_innings,
        )
        return {
            "projected_score": projected,
            "total_overs": effective_total_overs,
            "wickets_per_innings": config.wickets_per_innings,
            "innings_complete": wickets_lost >= config.wickets_per_innings or overs_bowled >= effective_total_overs,
        }

    # ------------------------------------------------------------------ basketball

    async def calculate_basketball_spread(
        self,
        db: AsyncSession,
        home_rating: float,
        away_rating: float,
        home_pace: float,
        away_pace: float,
        league_avg_pace: float,
    ) -> dict[str, Any]:
        config = await self._active_config(db, "basketball", BasketballConfig)
        spread = _run(
            basketball.calculate_pace_adjusted_spread,
            home_rating=home_rating,
            away_rating=away_rating,
            home_pace=home_pace,
            away_pace=away_pace,
            league_avg_pace=league_avg_pace,
        )
        home_win = _run(basketball.calculate_win_probability_from_spread, spread=spread, std_dev=config.std_dev)
        return {
            "pace_adjusted_spread": spread,
            "home_win_probability": home_win,
            "away_win_probability": round(1.0 - home_win, 4) + 0.0,
            "std_dev": config.std_dev,
        }

    # ------------------------------------------------------------------ tennis

    async def calculate_tennis_game(
        self,
        db: AsyncSession,
        base_serve_prob: float,
        player_surface_elo: float,
        opponent_surface_elo: float,
    ) -> dict[str, Any]:
        config = await self._active_config(db, "tennis", TennisConfig)
        elo_prob = _run(
            tennis.calculate_elo_win_probability,
            player_surface_elo=player_surface_elo,
            opponent_surface_elo=opponent_surface_elo,
            elo_scale=config.elo_scale,
        )
        adjusted = _run(
            tennis.calculate_surface_adjusted_serve_prob,
            base_serve_prob=base_serve_prob,
            player_surface_elo=player_surface_elo,
            opponent_surface_elo=opponent_surface_elo,
            elo_scale=config.elo_scale,
        )
        game_prob = _run(tennis.calculate_game_win_probability, p=adjusted)
        return {
            "elo_win_probability": elo_prob,
            "adjusted_serve_prob": adjusted,
            "game_win_probability": game_prob,
            "elo_scale": config.elo_scale,
        }
