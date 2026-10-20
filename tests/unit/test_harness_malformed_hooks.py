"""A hook post an adapter cannot read is skipped; it does not end the member's events.

Each hook-driven adapter mapped its posts inside the loop that fed the runtime, so one post that
raised ended the stream: the runtime failed the member with "its events stopped", and every later
hook — the prompt, the tools, the turn's end — reached nobody.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.harness.contract import EventKind, HookPost, Launch
from daedalus.harness.grok import GrokAdapter
from daedalus.harness.pi import PiAdapter


class Term:
    id = "term-1"

    def __init__(self, posts: list[HookPost]) -> None:
        self.posts = posts

    async def hooks(self) -> AsyncIterator[HookPost]:
        for post in self.posts:
            yield post

    async def reply(self, reply_id: str, body: Any) -> bool:
        return True


GOOD = {
    "claude": HookPost("Stop", {"last_assistant_message": "done"}),
    "grok": HookPost("SessionStart", {"sessionId": "g1", "source": "startup"}),
    "pi": HookPost("pi", {"event": "agent_settled", "lastMessage": "done"}),
}


@pytest.mark.parametrize("adapter", [ClaudeCodeAdapter(), GrokAdapter(), PiAdapter()], ids=["claude", "grok", "pi"])
async def test_a_post_that_cannot_be_read_is_skipped_and_the_next_one_is_heard(adapter: Any) -> None:
    mapped = adapter._map

    def fragile(*args: Any) -> Any:
        post = args[-1]
        if post.body == {"malformed": True}:
            raise AttributeError("'str' object has no attribute 'get'")
        return mapped(*args)

    adapter._map = fragile
    good = GOOD[adapter.name]
    term = Term([HookPost(good.name, {"malformed": True}), good])
    launch = Launch("l1", "s1", adapter.name, "container", term.id, None, "", "", "", "")
    kinds = [event.kind async for event in adapter.events(term, launch)]
    assert kinds and kinds[-1] in (EventKind.TURN_COMPLETED, EventKind.READY), kinds


async def test_a_cursor_bridge_line_that_is_not_an_object_is_skipped() -> None:
    import asyncio

    from daedalus.harness.cursor import CursorAdapter, _Connection

    class Socket:
        def __init__(self) -> None:
            self.chunks = [b'[1, 2]\n"text"\n{"type": "hello", "session_id": "c1", "transcript": "/t"}\n']

        async def read(self) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

    state = _Connection(socket=Socket())
    waiting: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    state.pending[1] = waiting
    await CursorAdapter()._read(state)
    assert state.session_id == "c1" and state.ready.is_set()
    assert waiting.done() and isinstance(waiting.exception(), ConnectionError)
