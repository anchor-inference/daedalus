"""The operator's likely next message, offered as ghost text in an empty composer.

Asked for by the app once an answer has settled, never pushed: a guess nobody is looking at is a
model call wasted, and the app knows when the composer is empty and on the screen. It is never
sent by itself — the operator accepts it with Tab or types over it.

Each guess is a small model call, so each has reasons to be skipped before it is made: the setting
is off, the session is working or waiting on a question, it is long (the guess would read a tail
that says little about it, and costs most there), or the day's guesses have spent their allowance.
A guess is kept for the message it follows, so the app asking twice costs one call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from protocore.contracts.types import MessageRole, TextBlock

from daedalus.host.prompts import without_turn_context

logger = logging.getLogger(__name__)

PURPOSE = "next_message"
"""The label the guesses' cost is booked under in Usage, and what the daily allowance sums."""

PROMPT = (
    "Below is the end of a conversation between an operator and their AI agent. Guess the operator's next "
    "message: the one they are most likely to send now, written as they would write it — short, in their "
    "language, at most twelve words, no quotes. If the conversation is finished or no follow-up is likely, "
    "answer NONE.\n\n"
)

TAIL_MESSAGES = 6
"""How much of the conversation the guess reads: the last few exchanges, each clipped."""

MAX_CHARS = 140


def clean(answer: str) -> str:
    """The guess as the composer shows it: one line, no quotes or label, nothing when the model declined."""
    line = next((part.strip() for part in answer.strip().splitlines() if part.strip()), "")
    line = re.sub(r"^(?:operator|next message|оператор)\s*:\s*", "", line, flags=re.IGNORECASE)
    line = line.strip().strip("\"'«»“”").strip()
    if not line or line.upper().rstrip(".") == "NONE":
        return ""
    return line[:MAX_CHARS].rstrip()


class Predictor:
    """The guesses of one installation: the setting's checks, the call, and the one kept per session."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._kept: dict[str, tuple[int, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def spent_today(self) -> float:
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        row = await self.manager.db.fetchone(
            "SELECT coalesce(sum(cost_usd), 0) AS spent FROM usage_events WHERE purpose = ? AND at >= ?", (PURPOSE, start),
        )
        return float(row["spent"] if row is not None else 0.0)

    async def suggest(self, session_id: str) -> dict[str, str]:
        """``{"text": guess}``, or ``{"text": "", "skipped": reason}`` when no call was made or it failed."""
        manager = self.manager
        settings = manager.config.assist
        if not settings.predict:
            return {"text": "", "skipped": "off"}
        state = await manager.get_state(session_id)
        if state is None:
            raise KeyError(session_id)
        if state.running or state.pending is not None:
            return {"text": "", "skipped": "busy"}
        if not manager.config.presets:
            return {"text": "", "skipped": "no_model"}
        row = await manager.db.fetchone("SELECT count(*) AS n, coalesce(max(seq), 0) AS last FROM transcript WHERE session_id = ?", (session_id,))
        count, last = (int(row["n"]), int(row["last"])) if row is not None else (0, 0)
        if count == 0:
            return {"text": "", "skipped": "empty"}
        if count > settings.predict_max_messages:
            return {"text": "", "skipped": "long"}
        kept = self._kept.get(session_id)
        if kept is not None and kept[0] == last:
            return {"text": kept[1]}
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            kept = self._kept.get(session_id)
            if kept is not None and kept[0] == last:
                return {"text": kept[1]}
            preset = settings.predict_preset if settings.predict_preset in manager.config.presets else (manager.config.middle_preset() or "")
            provider, model = manager.providers.rungs_for(manager.config, preset or None)[0]
            if not manager.provider_costs_nothing(provider.endpoint.id, model) and await self.spent_today() >= settings.predict_usd_per_day:
                return {"text": "", "skipped": "cap"}
            tail = await manager.sessions.list_transcript(session_id, limit=TAIL_MESSAGES * 3)
            lines: list[str] = []
            for message in tail:
                if message.role not in (MessageRole.user, MessageRole.assistant) or message.metadata.get("daedalus.origin") == "core":
                    continue
                text = without_turn_context("".join(block.text for block in message.content_blocks if isinstance(block, TextBlock))).strip()
                if text:
                    who = "Operator" if message.role is MessageRole.user else "Agent"
                    lines.append(f"{who}: {text[-1200:] if who == 'Agent' else text[:600]}")
            if not lines or not lines[-1].startswith("Agent:"):
                # The guess follows an answer; a conversation whose last word is the operator's has none.
                return {"text": "", "skipped": "no_answer"}
            try:
                answer = await asyncio.wait_for(
                    manager.small_model(PROMPT + "\n\n".join(lines[-TAIL_MESSAGES:]), preset, purpose=PURPOSE, session_id=session_id, max_tokens=48, temperature=0.3),
                    timeout=20,
                )
            except Exception as exc:  # noqa: BLE001 — no guess is the answer to any failure
                logger.info("no next-message guess for session %s: %s", session_id, exc)
                return {"text": "", "skipped": "failed"}
            guess = clean(answer)
            self._kept[session_id] = (last, guess)
            return {"text": guess}
