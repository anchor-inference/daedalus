"""How hard a task's model thinks: the one place a task's effort setting becomes a request's settings.

Every task that names a model preset in the settings (the translator, the action review, the
heartbeat, the subagents…) has an effort beside it, ``TaskEffort``: empty for the task's default,
``none`` for no thinking, or one of the reasoning efforts. The default is not the same for all of
them, and this is where it is decided:

- a one-shot helper call (a translation, a review, a guess, a page read for one word) does not
  think. Haiku 5.5 thinks unless told not to, and a translator's whole output budget once went on
  thinking until the answer came back empty, so "default" there means off, said in the words the key
  proxy's Claude routes understand (``thinking_off``), not merely "not enabled";
- a task that runs a whole agent run keeps its preset's own thinking and effort, which is what the
  operator set the preset up to do.

A helper given an effort is also given room to think in: the budgets they were written with are the
answer alone (a review's 160 tokens), and a model that thinks within the same cap spends it all on
thinking and answers nothing.
"""

from __future__ import annotations

from typing import Any

from daedalus.config import REASONING_EFFORTS, TASK_EFFORTS

THINKING_ROOM: dict[str, int] = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 31999}
"""Tokens a helper call is given on top of its answer's budget when it is asked to think. The key
proxy's own budgets for the Claude routes, so a budget model is not cut off below the thinking it was
promised."""


def check(effort: str) -> str:
    """``effort`` when it is one a task may name; a ``ValueError`` saying which ones are, otherwise."""
    if effort not in TASK_EFFORTS:
        raise ValueError(f"effort must be empty or one of {', '.join(TASK_EFFORTS[1:])}")
    return effort


def thinks(effort: str) -> bool:
    return effort in REASONING_EFFORTS


def helper_extra(effort: str) -> dict[str, Any]:
    """The thinking part of a one-shot helper call's ``LLMRequest.extra``: off unless an effort is named.

    An unknown word is read as the default rather than refused: the settings validate what is saved,
    and a call made with a stale value should still be the safe one."""
    if thinks(effort):
        return {"enable_thinking": True, "reasoning_effort": effort}
    return {"enable_thinking": False, "thinking_off": True}


def helper_tokens(max_tokens: int, effort: str, ceiling: int = 0) -> int:
    """A helper's output budget with room to think when it is asked to; unchanged when it is not.

    ``ceiling``, when given, is the model's own output cap (its preset's ``max_output_tokens``): the room
    stops there, since an endpoint refuses a request for more than its model can write."""
    if not thinks(effort):
        return max_tokens
    wanted = max_tokens + THINKING_ROOM[effort]
    return min(wanted, max(max_tokens, ceiling)) if ceiling else wanted


def run_overrides(effort: str) -> dict[str, Any]:
    """A run's live-control overrides for the effort, the same two a session's own effort chooser writes.

    Empty says nothing, and the preset's own thinking and effort (or the session's choice) stand.
    ``none`` is the session chooser's "off": thinking not enabled."""
    if effort == "none":
        return {"thinking_enabled": False}
    if thinks(effort):
        return {"thinking_enabled": True, "reasoning_effort": effort}
    return {}


def set_model_arguments(effort: str) -> dict[str, Any]:
    """:func:`run_overrides` as :meth:`SessionManager.set_model` takes them."""
    overrides = run_overrides(effort)
    out: dict[str, Any] = {}
    if "thinking_enabled" in overrides:
        out["thinking"] = overrides["thinking_enabled"]
    if "reasoning_effort" in overrides:
        out["reasoning_effort"] = overrides["reasoning_effort"]
    return out


__all__ = ["THINKING_ROOM", "check", "helper_extra", "helper_tokens", "run_overrides", "set_model_arguments", "thinks"]
