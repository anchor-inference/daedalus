"""Which tool call the code running now belongs to, for a backend that has to say so to another machine.

An :class:`~daedalus.host.filesystem.ExecBackend` is handed a command and nothing else: the tool
that called it is not one of its arguments, and threading it through every file tool would change
every signature for the sake of one backend. The remote backend needs it anyway, for three reasons:
a request to the operator's machine names the call it serves, so the operator's terminal can show
it and its guard can judge it; a request with no call behind it is one the host made up on its own,
which the backend refuses to run on someone's computer; and a call handed over again after a
disconnect has to issue the same requests in the same order, so the machine's journal answers the
ones it already ran.

So every tool invocation opens a :class:`CallScope` (``daedalus.tools._guarded`` does it for every
tool), and the backend reads it from this context variable. Parallel calls run in tasks of their own,
each with a copy of the context, so one call's scope never leaks into a sibling's.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class CallScope:
    """One tool invocation: its session, its call id, its tool, and how many requests it has made."""

    session_id: str
    tool_call_id: str
    tool: str
    requests: int = 0

    def next_request(self) -> int:
        """The ordinal of the invocation's next request to another machine, counting from 1.

        Counted per invocation, not per call: a call handed over again starts from 1, which is what
        makes its requests the same requests as the first time."""
        self.requests += 1
        return self.requests


CURRENT: ContextVar[CallScope | None] = ContextVar("daedalus_tool_call", default=None)


def current() -> CallScope | None:
    return CURRENT.get()


__all__ = ["CURRENT", "CallScope", "current"]
