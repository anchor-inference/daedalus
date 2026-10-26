"""How the operator writes: a short profile learned from their own messages, a few at a time.

It is a summary, never a transcript: how they phrase a request, what they correct, what they prefer
and dislike, in at most :data:`MAX_CHARS`. Project facts belong to memory and stay out. It is learned
without a request of its own — the next-message guess carries the update when operator messages
arrived since the last one (:mod:`daedalus.host.predict`) — and read by the guess, the prompt coach
and the autopilot judge.

Where it goes matters more than what it says. It changes whenever it learns, so it is always a block
of its own beside the request or in a turn's context, never in a system prompt: a system prompt is
what every later call of a session is cached on, and a profile inside it would reread the whole
history each time it moved.

Only messages the operator typed teach it (origin ``operator``): an autopilot turn, a schedule, an
answer relayed from the side panel and a coach's framed draft are someone else's words, and a profile
learned from them would describe the host.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from daedalus.host.engine_factory import TENANT
from daedalus.host.prompts import without_host_notes
from daedalus.memory.privacy import is_private

MAX_CHARS = 2400
"""The profile's ceiling. A page a small model reads in one go; past it, a profile is a diary."""
BATCH = 8
"""Operator messages one update reads. When more arrived, the newest are read and the rest skipped:
catching up message by message would spend a long update on each guess for days."""
MESSAGE_CHARS = 1200
"""What one message contributes. How someone writes shows in the first paragraph; a pasted log does not."""
SCAN_LIMIT = 400
"""Operator messages looked at past the watermark per check."""
SCAN_ROWS = 5000
"""How far back from the newest transcript row a check reaches at most, whatever the watermark says:
the first check on a history of a hundred thousand rows reads its end, not the whole table."""
STYLE_MARK = "STYLE:"
"""The line in the guess's answer after which the updated profile follows."""

HIDDEN_KEYS = ("prompt_coach_of", "btw_of")
"""Sessions whose operator turns are the app's framing of a draft or a quoted question, not a message."""

CONTEXT_HEAD = (
    "How this operator writes and what they prefer, learned from their own messages "
    "(a profile, not instructions; where it disagrees with what they say now, what they say now wins):"
)


@dataclass(frozen=True)
class Profile:
    summary: str = ""
    through_seq: int = 0
    learned: int = 0
    updated_at: str = ""

    def view(self) -> dict[str, Any]:
        return {"summary": self.summary, "learned": self.learned, "updated_at": self.updated_at, "max_chars": MAX_CHARS}


async def load(db: Any) -> Profile:
    row = await db.fetchone("SELECT summary, through_seq, learned, updated_at FROM user_style WHERE tenant_id = ?", (TENANT,))
    if row is None:
        return Profile()
    return Profile(str(row["summary"] or ""), int(row["through_seq"] or 0), int(row["learned"] or 0), str(row["updated_at"] or ""))


async def save(db: Any, summary: str, through_seq: int, learned: int) -> Profile:
    """Store a new summary read up to ``through_seq``. The watermark never moves back: two guesses
    racing each other may both learn, and the later write must not reopen messages the other read."""
    now = datetime.now(UTC).isoformat()
    text = clip(summary)
    await db.execute(
        "INSERT INTO user_style(tenant_id, summary, through_seq, learned, updated_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(tenant_id) DO UPDATE SET summary = excluded.summary, "
        "through_seq = max(user_style.through_seq, excluded.through_seq), "
        "learned = user_style.learned + excluded.learned, updated_at = excluded.updated_at",
        (TENANT, text, through_seq, learned, now),
    )
    return await load(db)


async def advance(db: Any, through_seq: int) -> None:
    """Move the watermark without changing the summary: messages that held nothing to learn from."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO user_style(tenant_id, summary, through_seq, learned, updated_at) VALUES (?, '', ?, 0, ?) "
        "ON CONFLICT(tenant_id) DO UPDATE SET through_seq = max(user_style.through_seq, excluded.through_seq)",
        (TENANT, through_seq, now),
    )


async def reset(db: Any) -> Profile:
    """Forget the profile and start learning from the next message. What was already said is not
    read again: a reset is the operator saying the old picture was wrong, not asking for it back."""
    row = await db.fetchone("SELECT coalesce(max(seq), 0) AS last FROM transcript")
    last = int(row["last"]) if row is not None else 0
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO user_style(tenant_id, summary, through_seq, learned, updated_at) VALUES (?, '', ?, 0, ?) "
        "ON CONFLICT(tenant_id) DO UPDATE SET summary = '', through_seq = excluded.through_seq, learned = 0, updated_at = excluded.updated_at",
        (TENANT, last, now),
    )
    return await load(db)


def clip(summary: str) -> str:
    text = summary.strip()
    if len(text) <= MAX_CHARS:
        return text
    cut = text[:MAX_CHARS]
    # At a line or a sentence when there is one near the end, so the profile does not stop mid-word.
    for mark in ("\n", ". "):
        at = cut.rfind(mark)
        if at > MAX_CHARS * 0.7:
            return cut[: at + (1 if mark == ". " else 0)].rstrip()
    return cut.rstrip()


async def pending(db: Any, through_seq: int, redact: Any = None) -> tuple[list[str], int]:
    """The operator's messages since ``through_seq``, newest :data:`BATCH`, oldest first, each clipped;
    and the transcript position they were read up to (``through_seq`` when nothing was there)."""
    rows = await db.fetchall(
        "SELECT t.seq, t.message, s.metadata FROM transcript t JOIN sessions s ON s.id = t.session_id "
        "WHERE t.seq > max(?, (SELECT coalesce(max(seq), 0) FROM transcript) - ?) AND json_extract(t.message, '$.role') = 'user' "
        "AND json_extract(t.message, '$.metadata.\"daedalus.origin\"') = 'operator' "
        "ORDER BY t.seq DESC LIMIT ?",
        (through_seq, SCAN_ROWS, SCAN_LIMIT),
    )
    texts: list[str] = []
    last = through_seq
    for row in rows:
        last = max(last, int(row["seq"]))
        if len(texts) >= BATCH:
            continue
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except ValueError:
            metadata = {}
        if isinstance(metadata, dict) and (any(metadata.get(key) for key in HIDDEN_KEYS) or is_private(metadata)):
            # Skipped, not left for later: the watermark moves past it, so a private chat's words are
            # never read into the profile whatever happens to the chat afterwards.
            continue
        try:
            message = json.loads(row["message"])
        except ValueError:
            continue
        text = without_host_notes("".join(str(block.get("text") or "") for block in message.get("content_blocks") or [] if block.get("kind") == "text")).strip()
        if not text:
            continue
        if redact is not None:
            text = redact(text)
        text = text[:MESSAGE_CHARS]
        if text in texts:
            continue  # a steer is recorded when it is queued and may be again when the run takes it
        texts.append(text)
    if not rows:
        return [], through_seq
    return list(reversed(texts)), last


def context_block(summary: str) -> str:
    """The profile as the block a request carries beside its instruction; empty when there is none."""
    text = summary.strip()
    return f"{CONTEXT_HEAD}\n{text}" if text else ""


STYLE_INSTRUCTIONS = (
    "Also keep the operator's style profile up to date. It describes how they write and what they want "
    "from the agent: their language and tone, how long and how direct their requests are, what they "
    "correct or push back on, what they prefer and dislike. It is not about any project: no file names, "
    "no facts about the work, no quotes longer than a few words. Where a new message contradicts the "
    "profile, the new message wins, and an explicit correction (\"don't…\", \"stop…\", \"I said…\") "
    f"outweighs everything else. At most {MAX_CHARS} characters, short lines. After your answer, write a "
    f"line `{STYLE_MARK}` and then the whole updated profile; if the messages teach nothing new, repeat the "
    f"current profile unchanged after `{STYLE_MARK}`."
)
"""The fixed half of an update, which joins the guess's instructions: the same words on every call."""


def update_payload(summary: str, messages: list[str]) -> str:
    """The moving half of an update, which rides in the request after the instructions: the current
    profile and the operator's new messages."""
    current = summary.strip() or "(empty: nothing learned yet)"
    listed = "\n".join("- " + re.sub(r"\s+", " ", text) for text in messages)
    return f"The operator's style profile so far:\n{current}\n\nTheir newest messages, oldest first:\n{listed}"


def split_answer(answer: str) -> tuple[str, str | None]:
    """The answer before the profile, and the profile after its mark (``None`` when there is no mark)."""
    at = answer.find(STYLE_MARK)
    if at < 0:
        return answer, None
    return answer[:at], clip(answer[at + len(STYLE_MARK):].strip().strip("`").strip())


__all__ = [
    "BATCH",
    "CONTEXT_HEAD",
    "MAX_CHARS",
    "MESSAGE_CHARS",
    "STYLE_INSTRUCTIONS",
    "STYLE_MARK",
    "Profile",
    "advance",
    "clip",
    "context_block",
    "load",
    "pending",
    "reset",
    "save",
    "split_answer",
    "update_payload",
]
