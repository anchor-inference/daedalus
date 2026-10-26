"""Private chats: the one question every memory-shaped feature asks before it reads a conversation.

A private chat keeps its transcript like any other chat (it is the operator's own disk, and deleting
the chat removes it), but nothing else looks into it: it is never learned from, never indexed for
search, never handed the memory of other chats, and never found from another chat. The flag is
``private: true`` in the session's metadata, and :func:`is_private` is the only reader of it, so the
search, the memory pass and the injection all agree on what private means.

The flag is set when the chat is created and never changes afterwards, in either direction. Turned
on after the first turn it would promise something already broken: the turns before it may have been
indexed or learned from. Turned off it would release turns the operator wrote believing they stayed
out, and the next pass would read them. A chat that should be private is started private; one that
should not be is a new chat. Nothing in the API offers a change, and a session made from a private
chat (a fork, a subagent, a side question, a prompt coach) is private too, because it carries the
private conversation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from protocore.contracts.tools import Tool, ToolContext
from protocore.contracts.types import ToolDefinition, ToolResult

PRIVATE_KEY = "private"
"""The session metadata key. Read as ``metadata.private`` by the chat search too; keep the spelling."""

PARENT_KEYS = ("subagent_of", "btw_of", "prompt_coach_of")
"""Metadata naming the chat a session was made from, whose privacy it inherits (with ``forked_from``)."""

MEMORY_TOOLS = frozenset({"Remember", "Recall", "Forget"})

REFUSAL = (
    "This chat is private: it does not read, add to or change long-term memory. Nothing was {verb}. "
    "Do not try again in this chat, and do not keep the note in a file instead; if the operator wants it "
    "remembered, they can say it in an ordinary chat."
)

TURN_NOTE = (
    "This chat is private: nothing said here is kept in long-term memory, learned from or found by search "
    "from other chats, and the memory tools refuse here. Do not copy what is said here into memory notes "
    "(AGENTS.md, .agents/memory) unless the operator asks for exactly that."
)
"""What the agent is told at the start of each run of a private chat, so it does not spend calls on
tools that will refuse or write the conversation into the project's notes on its own initiative."""


def _metadata(subject: Any) -> Mapping[str, Any] | None:
    if subject is None:
        return None
    if isinstance(subject, Mapping):
        return subject
    if isinstance(subject, (str, bytes)):
        # A row read straight from SQL carries the metadata as its JSON text.
        try:
            parsed = json.loads(subject or "{}")
        except ValueError:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    metadata = getattr(subject, "metadata", None)
    if isinstance(metadata, Mapping):
        return metadata
    session = getattr(subject, "session", None)
    return _metadata(session) if session is not None else None


def is_private(subject: Any) -> bool:
    """Whether a chat is private, given its session state, its ``Session``, its metadata dict or the
    metadata's JSON text. ``None`` (a session that is not loaded or no longer exists) is not private.

    Only the literal ``True`` counts: a stray truthy value from an old row or a typo must not quietly
    switch the memory off, and the app only ever sends a boolean."""
    metadata = _metadata(subject)
    return bool(metadata is not None and metadata.get(PRIVATE_KEY) is True)


def parent_ids(metadata: Mapping[str, Any]) -> list[str]:
    """The ids of the chats a new session is made from, as its metadata names them."""
    ids = [str(metadata[key]) for key in PARENT_KEYS if metadata.get(key)]
    forked = metadata.get("forked_from")
    if isinstance(forked, Mapping) and forked.get("session_id"):
        ids.append(str(forked["session_id"]))
    return ids


def public_sessions_sql(alias: str = "s") -> str:
    """A SQL condition over a ``sessions`` row that holds for every chat that is not private.

    A JSON ``true`` reads back from ``json_extract`` as the integer 1; ``IS NOT`` keeps the rows
    whose metadata has no such key, where ``!=`` would drop them as NULL."""
    return f"json_extract({alias}.metadata, '$.{PRIVATE_KEY}') IS NOT 1"


class PrivateChatGuard(Tool):
    """One of the memory tools, refusing in a private chat and otherwise exactly the tool it wraps.

    A wrapper rather than a check inside each tool: Forget is the core's own class, and Remember and
    Recall are rebuilt around a new memory model, so the refusal lives in one place none of them has
    to remember. The tool stays advertised in a private chat (the session's tool list is part of its
    cached prefix and is the same for every chat), and the refusal tells the model why it stops."""

    def __init__(self, inner: Tool, private: Callable[[str], bool]) -> None:
        self._inner = inner
        self._private = private
        self.output_trust = inner.output_trust

    def __getattr__(self, name: str) -> Any:
        # Everything else the registry or the prompt-cache sort may read is the wrapped tool's. Asked
        # for ``_inner`` itself (a copy made before ``__init__`` ran), it must fail, not recurse.
        if name == "_inner":
            raise AttributeError(name)
        return getattr(self._inner, name)

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def definition(self) -> ToolDefinition:
        return self._inner.definition

    @property
    def inner(self) -> Tool:
        return self._inner

    async def invoke(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if self._private(context.session_id):
            verb = {"Remember": "saved", "Recall": "read", "Forget": "forgotten"}.get(self._inner.name, "changed")
            return ToolResult(
                tool_call_id=str(context.metadata.get("tool_call_id") or ""),
                content=REFUSAL.format(verb=verb),
                is_error=True,
                metadata={"refused": "private_chat"},
            )
        return await self._inner.invoke(context, arguments)


def guard_memory_tools(tools: list[Tool], private: Callable[[str], bool]) -> list[Tool]:
    """The memory tools wrapped in :class:`PrivateChatGuard`, in the order given; other tools as they are."""
    return [PrivateChatGuard(tool, private) if tool.name in MEMORY_TOOLS else tool for tool in tools]


__all__ = [
    "MEMORY_TOOLS",
    "PARENT_KEYS",
    "PRIVATE_KEY",
    "REFUSAL",
    "TURN_NOTE",
    "PrivateChatGuard",
    "guard_memory_tools",
    "is_private",
    "parent_ids",
    "public_sessions_sql",
]
