"""Celery task: Scheduled quorum resolution for recently ingested events."""

from __future__ import annotations

import logging
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.celery_app import celery_app
from app.core.config import get_settings
from app.services.omni_normalizer import QuorumService, QuorumConsensusEngine, QuorumPolicy
from app.models.omni_vault import OmniRawPayload
from app.adapters.base_adapter import StandardizedEvent

logger = logging.getLogger(__name__)
_settings = get_settings()

@celery_app.task(name="omni.run_scheduled_quorum", acks_late=True)
def run_scheduled_quorum() -> dict[str, Any]:
    """
    Groups recent normalized events by topic and runs the QuorumService to find a consensus.
    Runs synchronously via asyncio.run() to integrate into Celery without blocking the loop.
    """
    summary: dict[str, Any] = {"topics_processed": 0, "quarantined": 0}
    
    async def _process_quorum() -> None:
        engine = create_async_engine(_settings.DATABASE_URL.get_secret_value())
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        
        policy = QuorumPolicy.from_settings(_settings)
        consensus_engine = QuorumConsensusEngine(policy)
        quorum_service = QuorumService(consensus_engine)
        
        # Look back 5 minutes
        cutoff = datetime.now(UTC) - timedelta(seconds=_settings.omni_quorum_max_age_seconds)
        
        async with session_factory() as session:
            # We would typically select events grouped by topic here.
            # OmniRawPayload doesn't store the normalized event directly (it's published),
            # but in a full implementation we'd read from a short-term buffer table or Redis stream
            # of recent normalized events, or we'd re-normalize on the fly.
            
            # Since this is the scaffold from Opus, we'll log its execution.
            # Implementation of the actual read-group-resolve loop depends on the storage of 
            # normalized StandardizedEvent models (likely in Redis for speed, or a DB ledger).
            logger.info("Quorum task triggered. Ready to process events since %s", cutoff)
            
        await engine.dispose()
        
    try:
        asyncio.run(_process_quorum())
    except Exception as exc:
        logger.exception("Scheduled quorum task failed.")
        summary["error"] = str(exc)
        
    return summary
