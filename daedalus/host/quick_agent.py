"""A quick agent: a fast model the leader hands a bounded piece of work to, inside one of its own tool calls.

It is a session like a subagent's — the leader's project, workspace, tool refusals and MCP servers — with
three differences that are the point of it. The leader waits for it in the call that started it, and the
result comes back as that call's result, never as a message later. It asks no questions: it runs with
the session's checks lifted whatever the leader's switch says, because a question would reach nobody
while the leader sits in the call, and the operator chose this trade when they allowed the models; a
choice the task does not settle goes back to the leader in its report, and the leader, who holds the
context, asks. And it is nobody's to manage: no list shows it, the Subagents panel included; the
leader's transcript draws its steps under the call, read from the session, which is kept for that and
dropped with the leader.

What it does wait for is what only the operator can do in the browser: a CAPTCHA, a sign-in, a payment
(``BrowserHandoff``), the private sign-in form (``BrowserLogin``), the move into their Chrome
(``BrowserAskOperatorChrome``). An ordinary session asks, ends its turn and is woken with the answer; a
quick agent's turn ending is its run ending, so its call waits instead, up to ``operator_wait_minutes``,
and the answer that would have woken the leader resolves the call (``Browsers.wait_for_operator``).
Its own time limit stands still meanwhile, and the request is withdrawn when nobody answers or the
leader is stopped.

Which presets may run it is the operator's choice in Settings, and a session may choose its own list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

KEY = "quick_agent"
"""Session metadata on the helper: ``{"call": the leader's tool call id}``. Present means "a quick agent"."""

MODELS_KEY = "quick_models"
"""Session metadata on a leader: the presets its quick agents may run on, chosen for this session alone.
Absent means the default from Settings; an empty list turns the tool off here."""

KEEP_PER_LEADER = 20
"""How many finished quick agents a leader keeps for its transcript to draw. Their sessions hold every
page they read, and a leader handing out a few a turn would otherwise collect hundreds; the older calls
then show their result without the steps."""


CALL_CEILING_SECONDS = 4 * 3600
"""The longest a QuickAgent call may hold its leader, waits for the operator included: its dispatch budget
in place of the flat tool timeout, which cancelled the call (and deleted the helper) at 900 s. The call's
own clock stops the helper well before it."""

OPERATOR_WAIT_CEILING_SECONDS = 65 * 60
"""The dispatch budget of the browser's requests to the operator: the longest ``operator_wait_minutes``
allows, and the few minutes a sign-in spends reaching its page before it asks."""


def is_quick(metadata: Mapping[str, Any] | None) -> bool:
    return isinstance(metadata, Mapping) and isinstance(metadata.get(KEY), Mapping)


def call_of(metadata: Mapping[str, Any]) -> str:
    """The leader's tool call this helper answers, or ``""``."""
    entry = metadata.get(KEY)
    return str(entry.get("call") or "") if isinstance(entry, Mapping) else ""


def chosen(metadata: Mapping[str, Any], default: Iterable[str]) -> tuple[list[str], bool]:
    """``(presets, inherited)``: the session's own list, else the default from Settings."""
    own = metadata.get(MODELS_KEY)
    if isinstance(own, list):
        return [str(p) for p in own], False
    return [str(p) for p in default], True


def allowed(metadata: Mapping[str, Any], default: Iterable[str], *, ready: Iterable[str]) -> list[str]:
    """The presets a quick agent of this session may run on now, in the order chosen.

    A chosen preset that was deleted, or whose provider is gone, drops out rather than failing the call:
    the list is a choice among what can run, and the first that can is the default."""
    can_run = set(ready)
    return [p for p in dict.fromkeys(chosen(metadata, default)[0]) if p in can_run]


__all__ = ["CALL_CEILING_SECONDS", "KEEP_PER_LEADER", "KEY", "OPERATOR_WAIT_CEILING_SECONDS", "MODELS_KEY", "allowed", "call_of", "chosen", "is_quick"]
