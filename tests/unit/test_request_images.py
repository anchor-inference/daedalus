"""One request carries the newest pictures only, and the cut moves in steps so the prompt cache survives.

Every picture of a session used to be re-sent on every request; past 20 pictures or 32 MB Anthropic
refuses each one until a compaction removes them.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from protocore.contracts.llm import LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.wire import IMAGE_WINDOW_STEP, MAX_REQUEST_IMAGE_BYTES, MAX_REQUEST_IMAGES, images_left_out


def _pictures(count: int) -> list[Message]:
    messages = []
    for index in range(count):
        messages.append(Message(role=MessageRole.user, content_blocks=[TextBlock(text=f"shot {index}")], metadata={"image_refs": [{"ref": f"blob-{index}", "mime": "image/png"}]}))
        messages.append(Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="seen")]))
    return messages


class Loader:
    def __init__(self, size: int = 1000) -> None:
        self.size = size
        self.loaded: list[str] = []

    async def __call__(self, blob_ref: str) -> tuple[bytes, str]:
        self.loaded.append(blob_ref)
        return b"x" * self.size, "image/png"


async def test_a_session_within_the_bounds_sends_every_picture() -> None:
    assert await images_left_out(_pictures(MAX_REQUEST_IMAGES), Loader()) == frozenset()


async def test_past_the_count_the_oldest_leave_in_steps() -> None:
    assert MAX_REQUEST_IMAGES == 20 and IMAGE_WINDOW_STEP == 5
    one_over = await images_left_out(_pictures(21), Loader())
    assert one_over == {f"blob-{i}" for i in range(5)}
    # The same cut for the next four pictures, so the prefix the cache holds stays the same.
    for count in range(22, 26):
        assert await images_left_out(_pictures(count), Loader()) == one_over
    assert await images_left_out(_pictures(26), Loader()) == {f"blob-{i}" for i in range(10)}


async def test_past_the_bytes_the_oldest_leave_too_and_only_the_newest_are_read() -> None:
    loader = Loader(size=6 * 1024 * 1024)  # 8 MB each once encoded: three of them pass 20 MB
    left_out = await images_left_out(_pictures(5), loader)
    # Two fit; the step would leave out all five, but the newest that fits is always sent.
    assert left_out == {f"blob-{i}" for i in range(4)}
    assert loader.loaded == ["blob-4", "blob-3", "blob-2"]
    huge = Loader(size=MAX_REQUEST_IMAGE_BYTES)
    assert await images_left_out(_pictures(2), huge) == {"blob-0", "blob-1"}
    small = Loader(size=MAX_REQUEST_IMAGE_BYTES // 40)
    await images_left_out(_pictures(40), small)
    assert len(small.loaded) == MAX_REQUEST_IMAGES


async def test_the_request_names_what_it_left_out_and_reads_each_kept_picture_once() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        chunk = {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    loader = Loader()
    endpoint = ProviderEndpoint(id="claude", kind="openai_compat", base_url="https://x.test", api_key="k")
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), image_loader=loader, images_for=lambda _model: True)
    async for _ in provider.stream_with_tools(LLMRequest(model="m", messages=_pictures(25))):
        pass
    users = [m for m in captured["body"]["messages"] if m["role"] == "user"]
    sent = [part for m in users if isinstance(m["content"], list) for part in m["content"] if part["type"] == "image_url"]
    assert len(sent) == 20
    assert "left out" in users[0]["content"] and "blob-0" in users[0]["content"]
    # The twenty sent were read once each, to be measured and then sent; the five left out never.
    assert len(loader.loaded) == len(set(loader.loaded)) == 20
