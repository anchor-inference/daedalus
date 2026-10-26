"""The operator's likely next message, offered as ghost text in an empty composer.

Asked for by the app once an answer has settled, never pushed: a guess nobody is looking at is a
model call wasted, and the app knows when the composer is empty and on the screen. It is never
sent by itself — the operator accepts it with Tab or types over it.

Each guess is a small model call, so each has reasons to be skipped before it is made: the setting
is off, the session is working or waiting on a question, it is long (the guess would read a tail
that says little about it, and costs most there), or the day's guesses have spent their allowance.
A guess is kept for the message it follows, so the app asking twice costs one call.

The guess also carries the operator's style profile (:mod:`daedalus.host.user_style`): it reads it,
and when operator messages arrived since the profile last learned, the same call returns the profile
updated — no request of its own. The autopilot judge (:mod:`daedalus.host.autopilot`) is this guess
made explicit, and reads the conversation through the same :func:`conversation_lines`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from datetime import UTC, datetime
from typing import Any

from protocore.contracts.types import MessageRole, TextBlock

from daedalus.host import user_style
from daedalus.host.prompts import without_turn_context

logger = logging.getLogger(__name__)

PURPOSE = "next_message"
"""The label the guesses' cost is booked under in Usage, and what the daily allowance sums."""

PROMPT = (
    "Below is the end of a conversation between an operator and their AI agent. Guess the operator's next "
    "message: the one they are most likely to send now, written as they would write it — short, in their "
    "language, at most twelve words, no quotes. If the conversation is finished or no follow-up is likely, "
    "answer NONE."
)

TAIL_MESSAGES = 6
"""How much of the conversation the guess reads: the last few exchanges, each clipped."""

AGENT_CHARS = 1200
"""The end of each of the agent's messages: where its answer and its question to the operator are."""
OPERATOR_CHARS = 600
"""The start of each of the operator's: where the request is."""

MAX_CHARS = 140

GUESS_TOKENS = 48
LEARN_TOKENS = 900
"""What an answer that carries the updated profile may add: the profile's ceiling in tokens, with room."""

OFFERED_KEPT = 50
"""Recent guesses remembered, so a guess accepted and sent unchanged is not learned as the operator's."""

AUTOPILOT_ORIGIN = "autopilot"
"""The origin of a turn autopilot started on the operator's behalf. The tail names it as such, so
neither the guess nor the judge reads it as something the operator said."""


def clean(answer: str) -> str:
    """The guess as the composer shows it: one line, no quotes or label, nothing when the model declined."""
    line = next((part.strip() for part in answer.strip().splitlines() if part.strip()), "")
    line = re.sub(r"^(?:operator|next message|оператор)\s*:\s*", "", line, flags=re.IGNORECASE)
    line = line.strip().strip("\"'«»“”").strip()
    if not line or line.upper().rstrip(".") == "NONE":
        return ""
    return line[:MAX_CHARS].rstrip()


def _same(text: str) -> str:
    return " ".join(text.split()).casefold()


def helper_preset(manager: Any, wanted: str = "") -> str:
    """The preset a helper call goes to: ``wanted`` when it names one, else the guess's own, else a
    middle one of the table, as the action review uses."""
    presets = manager.config.presets
    if wanted and wanted in presets:
        return wanted
    own = manager.config.assist.predict_preset
    return own if own in presets else (manager.config.middle_preset() or "")


async def spent_today(db: Any, purpose: str) -> float:
    """What the calls booked under ``purpose`` cost since midnight UTC."""
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    row = await db.fetchone("SELECT coalesce(sum(cost_usd), 0) AS spent FROM usage_events WHERE purpose = ? AND at >= ?", (purpose, start))
    return float(row["spent"] if row is not None else 0.0)


async def conversation_lines(manager: Any, session_id: str, *, tail: int = TAIL_MESSAGES) -> list[str]:
    """The end of a conversation as a helper reads it: ``Operator:``, ``Autopilot:`` and ``Agent:``
    lines, each clipped, the host's notes and the core's own nudges left out, and anything secret-shaped
    masked the way the logs mask it — a helper's request goes to a model like any other."""
    messages = await manager.sessions.list_transcript(session_id, limit=tail * 3)
    redact = manager.redactor.redact
    lines: list[str] = []
    for message in messages:
        if message.role not in (MessageRole.user, MessageRole.assistant) or message.metadata.get("daedalus.origin") == "core":
            continue
        text = without_turn_context("".join(block.text for block in message.content_blocks if isinstance(block, TextBlock))).strip()
        if not text:
            continue
        if message.role is MessageRole.assistant:
            lines.append(f"Agent: {redact(text[-AGENT_CHARS:])}")
        else:
            who = "Autopilot" if message.metadata.get("daedalus.origin") == AUTOPILOT_ORIGIN else "Operator"
            lines.append(f"{who}: {redact(text[:OPERATOR_CHARS])}")
    return lines[-tail:]


class Predictor:
    """The guesses of one installation: the setting's checks, the call, and the one kept per session."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._kept: dict[str, tuple[int, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._learning = asyncio.Lock()
        self._offered: deque[str] = deque(maxlen=OFFERED_KEPT)

    async def spent_today(self) -> float:
        return await spent_today(self.manager.db, PURPOSE)

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
            preset = helper_preset(manager)
            provider, model = manager.providers.rungs_for(manager.config, preset or None)[0]
            if not manager.provider_costs_nothing(provider.endpoint.id, model) and await self.spent_today() >= settings.predict_usd_per_day:
                return {"text": "", "skipped": "cap"}
            lines = await conversation_lines(manager, session_id)
            if not lines or not lines[-1].startswith("Agent:"):
                # The guess follows an answer; a conversation whose last word is the operator's has none.
                return {"text": "", "skipped": "no_answer"}
            guess = await self._ask(session_id, preset, lines)
            if guess is None:
                return {"text": "", "skipped": "failed"}
            if guess:
                self._offered.append(_same(guess))
            self._kept[session_id] = (last, guess)
            return {"text": guess}

    async def _ask(self, session_id: str, preset: str, lines: list[str]) -> str | None:
        """The call itself, with the profile beside the conversation and, when there is something new to
        learn, the update riding along. ``None`` when the call failed."""
        manager = self.manager
        profile = await user_style.load(manager.db)
        learning: tuple[list[str], int] = ([], profile.through_seq)
        if manager.is_private(session_id):
            # A private chat is handed nothing learned from the others, and the guess made in it does
            # not carry the update either: the profile stays as the ordinary chats left it.
            profile = user_style.Profile(through_seq=profile.through_seq)
        elif not self._learning.locked():
            # One update at a time across sessions: two guesses learning the same messages at once
            # would each write a profile, and the second would overwrite the first's for nothing.
            learning = await user_style.pending(manager.db, profile.through_seq, manager.redactor.redact)
        new, through = learning
        # A guess the operator took with Tab and sent as it was is the model's sentence, not theirs:
        # learning from it would teach the profile to describe the guesser.
        new = [text for text in new if _same(text) not in self._offered]
        instructions = PROMPT + (f"\n\n{user_style.STYLE_INSTRUCTIONS}" if new else "")
        parts = [user_style.update_payload(profile.summary, new) if new else user_style.context_block(profile.summary), "\n\n".join(lines)]
        prompt = "\n\n".join(part for part in parts if part)
        try:
            if new:
                async with self._learning:
                    answer = await self._call(prompt, preset, session_id, instructions, GUESS_TOKENS + LEARN_TOKENS, 60)
                    head, learned = user_style.split_answer(answer)
                    if learned:
                        await user_style.save(manager.db, learned, through, len(new))
                    else:
                        # A model that left the update out still read the messages; asking it again with
                        # the same ones on every guess would spend the long answer for nothing each time.
                        logger.info("the guess for session %s carried no style update", session_id)
                        await user_style.advance(manager.db, through)
            else:
                if through > profile.through_seq:
                    await user_style.advance(manager.db, through)  # rows past the watermark, none of them the operator's
                head = await self._call(prompt, preset, session_id, instructions, GUESS_TOKENS, 20)
        except Exception as exc:  # noqa: BLE001 — no guess is the answer to any failure
            logger.info("no next-message guess for session %s: %s", session_id, exc)
            return None
        return clean(head)

    async def _call(self, prompt: str, preset: str, session_id: str, instructions: str, max_tokens: int, timeout: float) -> str:
        return await asyncio.wait_for(
            self.manager.small_model(prompt, preset, purpose=PURPOSE, session_id=session_id, max_tokens=max_tokens, temperature=0.3, instructions=instructions),
            timeout=timeout,
        )


__all__ = ["AUTOPILOT_ORIGIN", "PROMPT", "PURPOSE", "Predictor", "clean", "conversation_lines", "helper_preset", "spent_today"]
