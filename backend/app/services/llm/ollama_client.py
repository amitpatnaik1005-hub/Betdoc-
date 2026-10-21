"""Scout Oracle's local language model (Group 100): an Ollama client, BetDoc's domain guardrail and the action cards.

Developed for Amit Ashok Kumar Patnaik.

* ``is_off_topic``: a cheap pre-classifier, whole words only. A question is refused only when it is clearly about
  something else (a poem, homework, politics, code...) and names nothing from sport or BetDoc; anything in between
  goes on, and the model's system prompt holds the same line.
* ``OllamaScoutClient``: one non-streamed ``/api/chat`` call to the configured local model with the asker's live
  book as facts. Disabled, unreachable, slow or empty: it returns None and the caller answers from its rule engine.
  A failed probe is remembered for ``SCOUT_LLM_RETRY_AFTER_SECONDS`` so a stopped Ollama does not cost every
  message a timeout.
* ``actions_for``: action cards derived from what the user asked, pointing only at pages that exist.

Everything stays on the machine that runs Ollama: nothing is sent to a hosted model.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

from app.core.config import Settings

logger = logging.getLogger("betdoc.scout.llm")

ARCHITECT = "Amit Ashok Kumar Patnaik"
REFUSAL = (
    "I am Scout Oracle, exclusively engineered for BetDoc. Ask me to build a parlay, predict a match score, "
    "audit your slip, or guide you through any BetDoc module."
)

SYSTEM_PROMPT = f"""You are Scout Oracle, the sports research assistant inside BetDoc. Architect: {ARCHITECT}.
Your only domain is BetDoc: sports analytics, match and score predictions, parlay building, the Digital Twin's 15-pillar
slip audit, staking and bankroll management in Indian Rupees (canonical) and US Dollars (display only).
If a question is about anything else, reply with exactly: "{REFUSAL}"
Rules:
- Quote the live figures given to you as facts. Never invent a bankroll, a balance, a result, an odds price or a statistic.
  When a figure is not in the facts, say BetDoc does not have it.
- Odds are decimal (1.85, 2.10). Money is in rupees (₹); give USD only if asked.
- Expected value is fair probability x odds - 1. Recommend stakes only as a fraction of the stated bankroll.
- Betting carries risk: no outcome is certain, and you never promise profit.
- Be concise: at most six short sentences or a short list."""

# Whole words (and short phrases) only, so "revolution" never reads as "ev" nor "window" as "win"
_SPORT_TERMS = re.compile(
    r"\b(bets?|betting|parlays?|accas?|accumulators?|odds|scores?|scorelines?|match(es)?|fixtures?|games?|league|cup|"
    r"football|soccer|cricket|ipl|tennis|nba|nfl|basketball|baseball|hockey|rugby|kabaddi|f1|epl|premier league|"
    r"goals?|wickets?|runs|innings|points|handicaps?|totals?|over|under|btts|draw|win|wins|winner|stakes?|staking|"
    r"bankroll|vault|ev|\+ev|value|edge|kelly|stop[- ]loss|drawdown|overround|vig|juice|margin|clv|closing line|"
    r"brier|tds|115bbj|inr|usd|rupees?|parimatch|1xbet|pinnacle|betfair|stake\.com|bookmakers?|tipsters?|"
    r"slips?|legs?|oracle|scout|betdoc|lab|archive|arena|hive|wire|ashoka|kumbha|panini|predict(ion|ions)?|"
    r"arsenal|chelsea|liverpool|madrid|barcelona|teams?|players?|injur(y|ies)|lineups?|referee|weather)\b",
    re.IGNORECASE,
)
_OFF_TOPIC_TERMS = re.compile(
    r"\b(poems?|poetry|essays?|recipes?|homework|lyrics|stor(y|ies)|jokes?|translate|translation|capital of|"
    r"president|prime minister|elections?|politics?|political|religion|history of|world war|python|javascript|"
    r"java|sql|flask|django|html|programming|write (me )?(a |some )?code|stocks?|shares|bitcoin|crypto|forex|"
    r"movies?|songs?|celebrit(y|ies)|dating|relationship|medical|diagnos(e|is)|legal advice)\b",
    re.IGNORECASE,
)


def is_off_topic(message: str) -> bool:
    """Refuse only what is clearly elsewhere: an off-topic marker and nothing from sport or BetDoc."""
    return bool(_OFF_TOPIC_TERMS.search(message)) and not _SPORT_TERMS.search(message)


# Action cards: (pattern on the user's question, card). Only routes the app has.
_ACTIONS: tuple[tuple[re.Pattern[str], dict[str, str]], ...] = (
    (re.compile(r"\b(parlays?|accas?|accumulators?|multis?|slips?|build)\b", re.I),
     {"type": "LOAD_PARLAY", "label": "Open the parlay workbench", "route": "/oracle"}),
    (re.compile(r"\b(audit|vet|vetting|fortress|pillars?|check my slip)\b", re.I),
     {"type": "OPEN_CONSULTANT", "label": "Run the Twin fortress", "route": "/oracle"}),
    (re.compile(r"\b(predict(ion|ions)?|scores?|scorelines?|probabilit(y|ies)|fair odds)\b", re.I),
     {"type": "VIEW_PREDICTION", "label": "Price it in the Model sandbox", "route": "/lab"}),
    (re.compile(r"\b(bankroll|vault|stake|staking|kelly|stop[- ]loss|drawdown|tds|tax|exposure|risk)\b", re.I),
     {"type": "NAVIGATE", "label": "Open The Vault", "route": "/vault"}),
    (re.compile(r"\b(clv|closing line|history|settled|calibration|brier)\b", re.I),
     {"type": "NAVIGATE", "label": "Open The Archive", "route": "/archive"}),
)
MAX_ACTIONS = 3


def actions_for(message: str) -> list[dict[str, Any]]:
    """Up to three cards for what the question is about, each pointing at a page that exists, without repeats."""
    cards: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for pattern, card in _ACTIONS:
        key = (card["type"], card["route"])
        if pattern.search(message) and key not in seen:
            seen.add(key)
            cards.append(dict(card))
        if len(cards) == MAX_ACTIONS:
            break
    return cards


class OllamaScoutClient:
    """One local model behind the Scout. Developer Credit: Amit Ashok Kumar Patnaik."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.base_url = settings.SCOUT_LLM_BASE_URL.rstrip("/")
        self.transport = transport
        self._down_until = 0.0  # monotonic time before which the model is not tried again

    @property
    def model(self) -> str:
        return self.settings.SCOUT_LLM_MODEL

    async def is_available(self) -> bool:
        if not self.settings.SCOUT_LLM_ENABLED or time.monotonic() < self._down_until:
            return False
        try:
            async with httpx.AsyncClient(timeout=self.settings.SCOUT_LLM_PROBE_TIMEOUT_SECONDS, transport=self.transport) as client:
                res = await client.get(f"{self.base_url}/api/tags")
            if res.status_code == 200:
                return True
        except (httpx.HTTPError, OSError):
            pass
        self._mark_down()
        return False

    def _mark_down(self) -> None:
        self._down_until = time.monotonic() + self.settings.SCOUT_LLM_RETRY_AFTER_SECONDS

    async def answer(self, message: str, page_context: str | None, facts: Mapping[str, Any] | None) -> str | None:
        """The model's answer, or None (disabled, unreachable, slow, failed or empty): the caller then uses its rule engine."""
        if not await self.is_available():
            return None
        facts_text = json.dumps(dict(facts), ensure_ascii=False) if facts else "none (the asker is anonymous or their book is unavailable)"
        system = f"{SYSTEM_PROMPT}\nPage the asker is on: {page_context or 'unknown'}\nLive facts (INR): {facts_text}"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": message}],
            "stream": False,
            "options": {"temperature": self.settings.SCOUT_LLM_TEMPERATURE, "top_p": 0.9, "num_predict": self.settings.SCOUT_LLM_MAX_TOKENS},
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.SCOUT_LLM_TIMEOUT_SECONDS, transport=self.transport) as client:
                res = await client.post(f"{self.base_url}/api/chat", json=payload)
            res.raise_for_status()
            content = str((res.json().get("message") or {}).get("content") or "").strip()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("Scout: the local model did not answer (%s); the rule engine answers instead", type(exc).__name__)
            self._mark_down()
            return None
        return content or None
