"""OracleScoutManager: context-aware mock responses and chronological history for ASHOKA."""

import asyncio
import logging
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.oracle_scout.errors import ScoutDomainError
from app.models.oracle_scout import OracleScoutHistoryModel

logger = logging.getLogger("betdoc.ashoka")

MAX_MESSAGE_LENGTH = 4_000
MAX_PAGE_CONTEXT_LENGTH = 120
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200
DEFAULT_LATENCY_S = 0.01
TOPIC_PREVIEW_LENGTH = 80

ScoutZone = Literal["vault", "arena", "default"]


def normalize_page_context(page_context: str | None) -> str | None:
    """Lowercase and trim the page context; blank or missing becomes None."""
    if page_context is None:
        return None
    normalized = str(page_context).strip().lower()
    return normalized or None


def resolve_zone(page_context: str | None) -> ScoutZone:
    """Vault takes precedence over Arena if a context mentions both."""
    if page_context is None:
        return "default"
    if "vault" in page_context:
        return "vault"
    if "arena" in page_context:
        return "arena"
    return "default"


def _topic_preview(message: str) -> str:
    collapsed = " ".join(message.split())
    if len(collapsed) <= TOPIC_PREVIEW_LENGTH:
        return collapsed
    return collapsed[: TOPIC_PREVIEW_LENGTH - 3].rstrip() + "..."


def compose_response(zone: ScoutZone, message: str) -> str:
    topic = _topic_preview(message)
    if zone == "vault":
        return (
            f'ASHOKA Vault briefing on "{topic}": solid bankroll management comes first. '
            "Keep each stake to 1-2% of your bankroll, cap your total daily risk, and never chase losses."
        )
    if zone == "arena":
        return (
            f'ASHOKA Arena briefing on "{topic}": watch the live odds for sharp movement and use in-play tracking '
            "to spot momentum shifts before the market adjusts."
        )
    return (
        f'ASHOKA briefing on "{topic}": sharp betting means finding a price better than the true probability. '
        "Compare lines, track closing line value, and only bet when you have a measurable edge."
    )


class OracleScoutManager:
    def __init__(self, *, latency_s: float = DEFAULT_LATENCY_S) -> None:
        if latency_s < 0:
            raise ValueError("latency_s must be non-negative.")
        self._latency_s = latency_s

    async def chat(
        self,
        db: AsyncSession,
        user_message: str,
        page_context: str | None,
        user_id: UUID | None,
    ) -> OracleScoutHistoryModel:
        message = (user_message or "").strip()
        if not message:
            raise ScoutDomainError("user_message must not be blank.")
        if len(message) > MAX_MESSAGE_LENGTH:
            raise ScoutDomainError(f"user_message must be at most {MAX_MESSAGE_LENGTH} characters.")

        context = normalize_page_context(page_context)
        if context is not None and len(context) > MAX_PAGE_CONTEXT_LENGTH:
            raise ScoutDomainError(f"page_context must be at most {MAX_PAGE_CONTEXT_LENGTH} characters.")

        zone = resolve_zone(context)
        response_text = compose_response(zone, message)

        await asyncio.sleep(self._latency_s)  # mock AI latency without blocking the event loop

        entry = OracleScoutHistoryModel(
            user_id=user_id,
            page_context=context,
            user_message=message,
            oracle_response=response_text,
        )
        db.add(entry)
        try:
            await db.commit()
        except SQLAlchemyError:
            await db.rollback()
            logger.error("ASHOKA: failed to persist a %s-zone conversation.", zone, exc_info=True)
            raise
        await db.refresh(entry)  # loads the server-side created_at

        logger.info(
            "ASHOKA: answered a %s-zone question (%d chars) for %s.",
            zone,
            len(message),
            user_id or "anonymous",
        )
        return entry

    async def get_history(
        self,
        db: AsyncSession,
        user_id: UUID | None,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> list[OracleScoutHistoryModel]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY_LIMIT:
            raise ScoutDomainError(f"limit must be an integer between 1 and {MAX_HISTORY_LIMIT}.")

        owner_filter = (
            OracleScoutHistoryModel.user_id.is_(None)
            if user_id is None
            else OracleScoutHistoryModel.user_id == user_id
        )
        statement = (
            select(OracleScoutHistoryModel)
            .where(owner_filter)
            .order_by(OracleScoutHistoryModel.created_at.desc(), OracleScoutHistoryModel.id.desc())
            .limit(limit)
        )
        results = list((await db.execute(statement)).scalars().all())
        history = results[::-1]  # newest N, returned oldest-to-newest

        logger.info("ASHOKA: served %d history entries for %s.", len(history), user_id or "anonymous")
        return history
