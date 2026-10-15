"""The autopilot judge: what one small call decides when a chat's run ends with autopilot on.

It is the next-message guess (:mod:`daedalus.host.predict`) made explicit. The guess writes what the
operator would probably say next and leaves it in the composer; the judge says whether that next
message is obvious enough to send without them (``continue``), whether nothing is left to send
(``done``), or whether the next step is theirs to decide (``ask``, or ``choice`` between a few ways
forward, each with the message it would send). It reads the same tail of the conversation the guess
reads and the operator's style profile beside it, so a continuation is written the way they write.

This module is the judge's words and the reading of its answer, nothing else: no state, no delivery.
:mod:`daedalus.extensions.autopilot` decides when it is asked and what happens with the verdict.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

PURPOSE = "autopilot"
"""The label the judge's cost is booked under in Usage, and what its daily allowance sums."""

TAIL_MESSAGES = 8
"""The conversation the judge reads: a little more than the guess, since it decides rather than suggests."""

MAX_TOKENS = 600
GOAL_MAX = 2000
SUMMARY_MAX = 600
QUESTION_MAX = 600
REASON_MAX = 300
LABEL_MAX = 60
OPTIONS_MIN, OPTIONS_MAX = 2, 4

Kind = Literal["continue", "done", "ask", "choice"]

INSTRUCTIONS = f"""\
You decide what happens after an AI agent finished a turn of work for its operator, who switched \
autopilot on: when the next step is obvious and safe, the work goes on without waiting for them.

Answer with one JSON object and nothing else, one of:
{{"verdict": "continue", "next_goal": "..."}}
  The work is unfinished, the next step is clear, within what the operator asked, and needs no decision \
of theirs. next_goal is the next message to the agent, written as the operator would write it, in their \
language, at most a few sentences.
{{"verdict": "done", "summary": "..."}}
  What the operator asked for is done, or nothing useful is left to do without them. summary: one or two \
sentences for a notification, in their language.
{{"verdict": "ask", "question": "..."}}
  The work needs a decision or information only the operator has. question: one short question, in their language.
{{"verdict": "choice", "reason": "...", "options": [{{"label": "...", "next_goal": "..."}}, ...]}}
  There are {OPTIONS_MIN} to {OPTIONS_MAX} clear ways forward and the operator should pick one. reason: one \
sentence. label: a few words for a button. next_goal: the message sent to the agent when that button is pressed.

Rules:
- Never continue into anything destructive, irreversible, costly or outward-facing (deleting, sending, \
publishing, deploying, paying, merging) or beyond what the operator asked: ask or offer a choice instead.
- When the agent's last message asks the operator something or offers options, answer with ask or choice, \
never continue.
- When the agent is going in circles, failing the same way again or waiting for something outside, say done.
- Prefer done over inventing more work. Lines marked Autopilot were sent by autopilot, not by the operator.
"""


@dataclass(frozen=True)
class Option:
    label: str
    next_goal: str


@dataclass(frozen=True)
class Verdict:
    kind: Kind
    next_goal: str = ""
    summary: str = ""
    question: str = ""
    reason: str = ""
    options: tuple[Option, ...] = field(default_factory=tuple)

    def detail(self) -> dict[str, Any]:
        """The verdict as the decision row keeps it, next goals included: this never goes to the app."""
        return {
            "next_goal": self.next_goal,
            "summary": self.summary,
            "question": self.question,
            "reason": self.reason,
            "options": [{"label": o.label, "next_goal": o.next_goal} for o in self.options],
        }


def request(lines: list[str], *, style: str, used: int, allowed: int) -> str:
    """What the judge reads after its instructions: the operator's style, how many continuations this
    message of theirs has already had, and the end of the conversation."""
    parts = [style] if style else []
    parts.append(f"Autopilot turns since the operator's last message: {used} of {allowed} allowed.")
    parts.append("\n\n".join(lines))
    return "\n\n".join(parts)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip() if not isinstance(value, (dict, list)) else ""


def _goal(value: Any) -> str:
    # A next goal may have lines of its own (a short list of steps); only the edges are trimmed.
    text = str(value or "").strip() if isinstance(value, str) else ""
    return text[:GOAL_MAX].rstrip()


def parse(answer: str) -> Verdict | None:
    """The judge's answer as a verdict, or ``None`` when it is not one: no JSON object, an unknown
    verdict, or one missing what it needs. Nothing unreadable is guessed at — no verdict means
    autopilot does nothing, which is always safe."""
    text = answer.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    kind = str(data.get("verdict") or "").strip().lower()
    if kind == "continue":
        goal = _goal(data.get("next_goal"))
        return Verdict("continue", next_goal=goal) if goal else None
    if kind == "done":
        return Verdict("done", summary=_clean(data.get("summary"), SUMMARY_MAX))
    if kind == "ask":
        question = _clean(data.get("question"), QUESTION_MAX)
        return Verdict("ask", question=question) if question else None
    if kind == "choice":
        options: list[Option] = []
        for raw in data.get("options") or []:
            if not isinstance(raw, dict):
                continue
            label, goal = _clean(raw.get("label"), LABEL_MAX), _goal(raw.get("next_goal"))
            if label and goal and label.lower() not in {o.label.lower() for o in options}:
                options.append(Option(label, goal))
        if not OPTIONS_MIN <= len(options) <= OPTIONS_MAX:
            return None
        return Verdict("choice", reason=_clean(data.get("reason"), REASON_MAX), options=tuple(options))
    return None


__all__ = ["INSTRUCTIONS", "PURPOSE", "TAIL_MESSAGES", "Option", "Verdict", "parse", "request"]
