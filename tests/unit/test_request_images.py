"""One request carries the newest pictures only, and the cut moves in steps so the prompt cache survives.

Every picture of a session used to be re-sent on every request; past 20 pictures or 32 MB Anthropic
refuses each one until a compaction removes them.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from protocore.contracts.llm import LLMRequest
from protocore.contracts.types import (
    IMAGE_REFS_METADATA_KEY,
    ImageRefBlock,
    Message,
    MessageRole,
    TextBlock,
    ToolResultBlock,
    ToolResultImage,
    ToolUseBlock,
)
from protocore.providers.openai_compat.wire import TOOL_IMAGES_CARRIER_HEADER

from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from daedalus.providers.wire import (
    CARRIED_IMAGES,
    IMAGE_WINDOW_STEP,
    MAX_REQUEST_IMAGE_BYTES,
    MAX_REQUEST_IMAGES,
    images_left_out,
    place_carried_images,
)


def _pictures(count: int) -> list[Message]:
    messages = []
    for index in range(count):
        messages.append(Message(role=MessageRole.user, content_blocks=[TextBlock(text=f"shot {index}")], metadata={IMAGE_REFS_METADATA_KEY: [ImageRefBlock(blob_ref=f"blob-{index}").model_dump(mode="json")]}))
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


def _tool_round() -> list[Message]:
    """A batch of two calls whose second result carries a screenshot, then the model's reply."""
    calls = [ToolUseBlock(tool_call_id=f"c{i}", name="Screen", arguments_json="{}") for i in (1, 2)]
    shot = ToolResultImage(blob_ref="blob-shot", width=1280, height=800)
    return [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="click it")]),
        Message(role=MessageRole.assistant, content_blocks=calls),
        Message(role=MessageRole.tool, content_blocks=[
            ToolResultBlock(tool_call_id="c1", content="clicked"),
            ToolResultBlock(tool_call_id="c2", content="captured", images=(shot,)),
        ]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="I see the dialog.")]),
    ]


async def _sent(images: bool) -> list[dict[str, Any]]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        chunk = {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    endpoint = ProviderEndpoint(id="claude", kind="openai_compat", base_url="https://x.test", api_key="k")
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                        image_loader=Loader(), images_for=lambda _model: images)
    async for _ in provider.stream_with_tools(LLMRequest(model="m", messages=_tool_round())):
        pass
    return captured["body"]["messages"]


async def test_a_tool_result_shows_its_picture_in_one_turn_after_the_whole_batch() -> None:
    """A tool's pictures used to be dropped on the way to the model. They ride after the last result of
    the batch, in the core's carrier shape, which the key proxy folds back into the results for Claude."""
    sent = await _sent(images=True)
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "tool", "user", "assistant"]
    assert [m["content"] for m in sent[2:4]] == ["clicked", "captured"], "the results keep their own text"
    carrier = sent[4]["content"]
    assert carrier[0] == {"type": "text", "text": TOOL_IMAGES_CARRIER_HEADER}
    assert carrier[1]["text"] == "[images returned by tool call c2]"
    assert carrier[2]["text"] == "[image 1 of tool call c2: 1280x800 pixels]"
    assert carrier[3]["type"] == "image_url" and carrier[3]["image_url"]["url"].startswith("data:image/png;base64,")
    assert not any("daedalus.carried_images" in m for m in sent)


async def test_a_model_that_takes_no_images_is_told_in_the_result_and_given_no_extra_turn() -> None:
    sent = await _sent(images=False)
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "tool", "assistant"]
    assert sent[3]["content"] == "captured\n[image not sent, this model does not take images: blob-shot]"


def test_a_breakpoint_on_the_last_result_lands_on_the_carrier_after_it() -> None:
    entries = [{"role": "user"}, {"role": "assistant"}, {"role": "tool", CARRIED_IMAGES: [{"type": "text", "text": "x"}]}, {"role": "assistant"}]
    placed, index_map = place_carried_images(entries, [0, 1, 2, 3, -1])
    assert [e["role"] for e in placed] == ["user", "assistant", "tool", "user", "assistant"]
    assert index_map == [0, 1, 3, 4, -1]
