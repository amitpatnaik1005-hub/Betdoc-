"""Wiring for the Omni-Sniper in a process (API worker or Celery worker): the gateway, its HTTP
clients, and the sandbox bookmaker where it is allowed."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security_vault import VaultCrypto
from app.models.execution import ExecutionVenue
from app.sandbox.bookmaker import SANDBOX_BASE_URL, build_sandbox_app, sandbox_credentials
from app.services.sniper import SniperGateway

logger = logging.getLogger("betdoc.sniper")

SANDBOX_VENUE_ID = "sandbox"


@dataclass(slots=True)
class SniperRuntime:
    gateway: SniperGateway
    http: httpx.AsyncClient
    sandbox_http: httpx.AsyncClient | None

    async def aclose(self) -> None:
        await self.http.aclose()
        if self.sandbox_http is not None:
            await self.sandbox_http.aclose()


def build_sniper_runtime(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis | None, settings: Settings, vault: VaultCrypto | None
) -> SniperRuntime:
    # follow_redirects=False: a venue redirecting an order elsewhere is refused, never followed
    http = httpx.AsyncClient(follow_redirects=False, limits=httpx.Limits(max_connections=50, max_keepalive_connections=20))
    sandbox_http = None
    if settings.sniper_sandbox_active and redis is not None:
        sandbox_app = build_sandbox_app(redis, session_factory, settings)
        sandbox_http = httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox_app), base_url=SANDBOX_BASE_URL, follow_redirects=False)
    gateway = SniperGateway(session_factory, redis, settings, vault, http, sandbox_http)
    return SniperRuntime(gateway, http, sandbox_http)


async def ensure_sandbox_venue(session_factory: async_sessionmaker[AsyncSession], vault: VaultCrypto | None, settings: Settings) -> bool:
    """Register (or refresh the credentials of) the sandbox venue in development. It stands in for
    every bookmaker (``routes = ["*"]``) that has no venue of its own."""
    if not settings.sniper_sandbox_active:
        return False
    if vault is None:
        logger.warning("Sandbox venue not registered: MASTER_VAULT_KEY is not configured")
        return False
    async with session_factory() as session:
        venue = await session.get(ExecutionVenue, SANDBOX_VENUE_ID)
        if venue is None:
            venue = ExecutionVenue(
                id=SANDBOX_VENUE_ID,
                display_name="BetDoc Sandbox",
                adapter="generic_json",
                base_url=SANDBOX_BASE_URL,
                auth_type="oauth2_client_credentials",
                token_path="/oauth/token",
                refresh_path="/oauth/token",
                place_path="/bets",
                status_path="/bets",
                events_path="/events",
                bets_per_second=settings.SNIPER_SANDBOX_BETS_PER_SECOND,
                burst=max(1, int(settings.SNIPER_SANDBOX_BETS_PER_SECOND)),
                routes=["*"],
                selection_codes={},
                is_enabled=True,
                is_sandbox=True,
            )
            session.add(venue)
        venue.encrypted_credentials = vault.encrypt_key(json.dumps(sandbox_credentials(settings)))
        venue.credentials_hint = "sandbox client · derived"
        await session.commit()
    return True
