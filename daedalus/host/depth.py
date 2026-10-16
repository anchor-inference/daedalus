"""How deep one operator message wants the agent to go, when the session leaves that to the host.

A session in the ``auto`` mode has each message it starts a run with sorted into one of three
levels before the run is built: ``quick`` (a question with a short answer), ``normal`` (ordinary
work) or ``long`` (a task that takes many steps). The level picks the mode of that one run and moves
its reasoning effort from the one the session runs at otherwise; the session itself stays on ``auto``.

A small model sorts the message when one is configured and answers in time; a heuristic of the
message's length and words sorts it otherwise. The model is bounded by a short timeout, because the
operator's message waits behind the sort: a sort that took longer than the run it shapes would cost
more than a wrong guess, which the operator corrects with one click on the turn.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

Level = Literal["quick", "normal", "long"]

AUTO_MODE = "auto"
"""The session mode that asks for a level per message. It is not one of the configured modes: it
names none of their limits itself, it picks one of them for each run."""

LEVELS: tuple[Level, ...] = ("quick", "normal", "long")

LEVEL_MODES: dict[str, str] = {"quick": "quick", "normal": "", "long": "deep"}
"""Level → the mode the run takes. ``normal`` is the plain agent: the level the host falls back to
when it cannot tell, so it must be the one that changes nothing."""

EFFORTS = ("low", "medium", "high", "xhigh")


def effort_for(level: str, base: str) -> str:
    """The reasoning effort one run of ``level`` takes, from the effort the session runs at otherwise.

    The level moves the session's own effort rather than replacing it: a fixed table ran every
    ordinary message at medium whatever the model was set to, so a model kept at high was quietly
    turned down, and a deep task on a model kept at the top effort ran below it. ``quick`` is the
    lowest effort, ``normal`` is the session's own and ``long`` is at least high.
    """
    if base not in EFFORTS:
        base = "medium"
    if level == "quick":
        return EFFORTS[0]
    if level == "long":
        return max(base, "high", key=EFFORTS.index)
    return base


CLASSIFY_PROMPT = (
    "You sort a message an operator sent to their AI agent by how much work answering it takes.\n"
    "quick: a greeting, thanks, a yes or no, a fact, a definition, a translation, a short question with a short answer.\n"
    "normal: a question that needs some reading or a few steps; a small change; an explanation.\n"
    "long: a task of many steps — build, implement, refactor, migrate, investigate, research, debug something broken, "
    "write a document or a program, anything that will take the agent many tool calls.\n"
    "Answer with one word: quick, normal or long.\n\nMessage:\n"
)

_LONG_WORDS = re.compile(
    r"\b(implement|refactor|rewrite|migrate|investigat\w*|research|debug|build|deploy|set ?up|"
    r"benchmark|audit|end[- ]to[- ]end|step by step|from scratch|whole|entire|all (?:the )?(?:files|tests)|root cause|"
    r"реализ\w*|отрефактор\w*|рефактор\w*|перепиш\w*|мигрируй|смигрируй|перенеси (?:вс|стар)\w*|исследу\w*|исследов\w*|"
    r"разбер(?:ись|итесь)|почини|отлад\w*|задеплой\w*|настрой|настроить|собери|напиши (?:приложени|сервис|скрипт|программ|бота)\w*|"
    r"сделай (?:мне )?(?:приложени|сервис|сайт|бота|полноценн)\w*|с нуля|целиком|полностью|по шагам|проанализиру\w*|аудит\w*)\b",
    re.IGNORECASE,
)
_TESTS_TOO = re.compile(r"\b(tests?|тест\w*)\b", re.IGNORECASE)
"""A request that also asks for tests is a change made and proven, which is several steps by itself."""
_QUICK_WORDS = re.compile(
    r"^(?:hi|hello|hey|thanks|thx|thank you|ok|okay|yes|no|nope|sure|good|great|"
    r"привет|спасибо|ок|окей|да|нет|не|хорошо|отлично|понял|ясно)\b|"
    r"\b(what is|what's|whats|what does|who is|when is|when was|which|how many|how much|what time|define|"
    r"что такое|что значит|кто такой|кто такая|когда|сколько|который час|какой|какая|какое)\b",
    re.IGNORECASE,
)
_TRANSLATION = re.compile(
    r"^\s*(?:translate|how do you say|how to say|переведи|перевод|как будет|как сказать|как по-\w+)\b", re.IGNORECASE,
)
"""A translation is quick however long its text, and the text is not the operator's request: a
sentence to translate that says "the build is broken" asks for no investigation."""


def heuristic(text: str) -> Level:
    """The level a message's shape suggests, without asking anyone.

    Words that name a long task win over its length, and a short message with a question's words
    is quick. Everything in between is normal, which runs the session as it would run without
    the mode — the safe answer when the words say nothing.
    """
    stripped = " ".join(text.split())
    if not stripped:
        return "normal"
    if _TRANSLATION.search(stripped) and len(stripped) <= 400:
        return "quick"
    lines = [line for line in text.splitlines() if line.strip()]
    listed = sum(1 for line in lines if re.match(r"\s*(?:\d+[.)]|[-*•])\s", line))
    clauses = len(re.findall(r"[,;:]|\band\b|\bи\b|\+", stripped))
    if _LONG_WORDS.search(stripped) or len(stripped) > 600 or listed >= 3:
        return "long"
    if _TESTS_TOO.search(stripped) and clauses >= 3:
        return "long"
    if len(stripped) <= 80 and "/" not in stripped and "." not in stripped.rstrip(".!?") and (_QUICK_WORDS.search(stripped) or len(stripped) <= 25):
        return "quick"
    return "normal"


def parse_level(answer: str) -> Level | None:
    """The level a model's answer names, or ``None`` when it named none of them."""
    for word in re.findall(r"[a-z]+", answer.lower())[:4]:
        if word in LEVELS:
            return word  # type: ignore[return-value]
    return None


@dataclass(frozen=True)
class Depth:
    level: Level
    source: Literal["model", "heuristic", "override"]
    milliseconds: int

    @property
    def mode(self) -> str:
        return LEVEL_MODES[self.level]

    def metadata(self) -> dict[str, object]:
        """What the operator's message carries, for the run's build and the chip the app draws on the turn.

        No effort: it follows from the level and the effort the session runs at when the run is built.
        """
        return {"level": self.level, "mode": self.mode, "source": self.source, "ms": self.milliseconds}


Ask = Callable[[str], Awaitable[str]]


async def classify(text: str, ask: Ask | None, *, timeout: float) -> Depth:
    """Sort one message: the model's answer when it gives one in time, else the heuristic's.

    Never raises: a model that fails, is slow or answers nonsense costs the operator the heuristic,
    not the message.
    """
    started = time.monotonic()
    if ask is not None and text.strip():
        try:
            answer = await asyncio.wait_for(ask(CLASSIFY_PROMPT + text[:2000]), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — timeouts and provider errors alike fall back
            answer = ""
        level = parse_level(answer)
        if level is not None:
            return Depth(level, "model", int((time.monotonic() - started) * 1000))
    return Depth(heuristic(text), "heuristic", int((time.monotonic() - started) * 1000))
