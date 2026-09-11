"""Read-only projections of the Twin's state.



Nothing here mutates anything. There is no endpoint that places a bet, changes

a limit, or writes to the store, and there deliberately is not going to be one

in this router: keeping the presentation layer read-only means no HTTP request

can move money, whatever a future authentication bug allows through.



Responses are the frozen domain models themselves. FastAPI serialises them with

Pydantic V2, so enums render as their string values and datetimes as RFC 3339

without any hand-written mapping layer to drift out of sync.

"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from betdoc.domain.intelligence.account_models import (
    AccountState,
    BettorProfile,
    LinkedBookmaker,
    utc_now,
)
from betdoc.presentation.api.dependencies import ProfileIdDep, StateStoreDep

__all__ = ["AccountsResponse", "CacheHealthResponse", "router"]


_log: Final[structlog.stdlib.BoundLogger] = structlog.get_logger(
    component="presentation.api.intelligence"
)


router = APIRouter(prefix="/api/v1/intelligence", tags=["intelligence"])


class AccountsResponse(BaseModel):
    """Cached account states keyed by bookmaker, plus their ages.



    Age is returned alongside the state rather than left implicit. A frontend

    rendering a balance without showing how stale it is invites the user to

    trust a number the engine itself would refuse to size against.

    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: str = Field(description="Server time when this projection was built.")

    accounts: dict[str, AccountState]

    age_seconds: dict[str, float] = Field(default_factory=dict)

    total_realized_balance_paise: int = Field(ge=0)

    total_unsettled_exposure_paise: int = Field(ge=0)

    @property
    def total_account_value_paise(self) -> int:

        return self.total_realized_balance_paise + self.total_unsettled_exposure_paise


class CacheHealthResponse(BaseModel):
    """Readiness of the L1 cache, for dashboards and probes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_warm: bool

    profiles: int = Field(ge=0)

    accounts: int = Field(ge=0)

    active_modifiers: int = Field(ge=0)

    detail: dict[str, object] = Field(default_factory=dict)


@router.get(
    "/profile",
    response_model=BettorProfile,
    summary="Current bettor risk profile",
    responses={404: {"description": "No profile has been loaded yet"}},
)
async def get_profile(store: StateStoreDep, profile_id: ProfileIdDep) -> BettorProfile:
    """Return the active policy the firewall enforces.



    404 rather than an empty object when the cache is cold. An empty profile

    would render as zero limits, and a dashboard showing a zero daily loss

    limit is indistinguishable from one showing a genuine hard stop.

    """

    profile = await store.get_profile(profile_id)

    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no profile cached for {profile_id!r}",
        )

    return profile


@router.get(
    "/accounts",
    response_model=AccountsResponse,
    summary="Cached account state for every linked bookmaker",
)
async def get_accounts(store: StateStoreDep) -> AccountsResponse:
    """Project every cached account snapshot, keyed by bookmaker name.



    Served entirely from memory. This endpoint never touches a bookmaker, so a

    dashboard refresh cannot add load to a venue or consume an API quota, and

    it cannot be slowed by one.

    """

    accounts: dict[str, AccountState] = {}

    ages: dict[str, float] = {}

    realized = 0

    exposure = 0

    for bookmaker in LinkedBookmaker:
        state = await store.get_account_state(bookmaker)

        if state is None:
            continue

        accounts[bookmaker.value] = state

        age = store.account_age_seconds(bookmaker)

        ages[bookmaker.value] = round(age, 3) if age is not None else 0.0

        realized += state.realized_balance_paise

        exposure += state.unsettled_exposure_paise

    return AccountsResponse(
        as_of=utc_now().isoformat(),
        accounts=accounts,
        age_seconds=ages,
        total_realized_balance_paise=realized,
        total_unsettled_exposure_paise=exposure,
    )


@router.get(
    "/accounts/{bookmaker}",
    response_model=AccountState,
    summary="Cached account state for one bookmaker",
    responses={404: {"description": "No snapshot cached for that bookmaker"}},
)
async def get_account(bookmaker: LinkedBookmaker, store: StateStoreDep) -> AccountState:
    """Single-account projection.



    ``bookmaker`` is typed as the enum, so an unknown value is rejected by

    FastAPI with a 422 before reaching this function.

    """

    state = await store.get_account_state(bookmaker)

    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no cached state for {bookmaker.value!r}; the daemon may be cold",
        )

    return state


@router.get(
    "/health",
    response_model=CacheHealthResponse,
    summary="L1 cache readiness",
)
async def get_cache_health(store: StateStoreDep) -> CacheHealthResponse:
    """Whether the cache is warm enough for the engine to be sizing bets.



    ``is_warm`` false means the orchestrator is skipping batches rather than

    trading badly, which is the correct behaviour but must be visible.

    """

    detail = store.health()

    return CacheHealthResponse(
        is_warm=bool(detail.get("is_warm", False)),
        profiles=int(str(detail.get("profiles", 0))),
        accounts=len(detail.get("accounts", {})),  # type: ignore[arg-type]
        active_modifiers=int(str(detail.get("active_modifiers", 0))),
        detail=detail,
    )
