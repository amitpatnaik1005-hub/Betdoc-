"""Guards against frontend/backend commander drift.

This caught a real bug: the frontend registry said DEVRAYA while the backend enum (and its
migration) still said VIKRAMADITYA, so heartbeats from commander #6 would have been rejected.
"""

import re
from pathlib import Path

import pytest

from app.models.the_hive import LegendaryBot

_FRONTEND_CONFIG = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "config" / "commanders.config.ts"
)


def _frontend_commander_ids() -> list[str]:
    source = _FRONTEND_CONFIG.read_text(encoding="utf-8")
    block = re.search(r"COMMANDER_IDS\s*=\s*\[(.*?)\]\s*as const", source, re.DOTALL)
    assert block is not None, "COMMANDER_IDS array not found in commanders.config.ts"
    return re.findall(r'"([^"]+)"', block.group(1))


@pytest.mark.skipif(not _FRONTEND_CONFIG.exists(), reason="frontend sources not present")
def test_frontend_commanders_match_backend_enum() -> None:
    assert sorted(_frontend_commander_ids()) == sorted(member.value for member in LegendaryBot)


@pytest.mark.skipif(not _FRONTEND_CONFIG.exists(), reason="frontend sources not present")
def test_commander_ids_are_unique() -> None:
    ids = _frontend_commander_ids()
    assert len(ids) == len(set(ids))
