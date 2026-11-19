"""A reply cut off by a dropped connection goes back to the model followed by a user turn, never as a prefill.

The newer Claude models refuse a request that ends on an assistant message, and the core's retry after a
dropped stream sends exactly that: the history with the part of the reply that already streamed.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from protocore.contracts.llm import LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolUseBlock

from daedalus.providers.openai_compat import CUT_REPLY_NOTE, OpenAICompatibleProvider, ProviderEndpoint


def _provider() -> OpenAICompatibleProvider:
    endpoint = ProviderEndpoint(id="claude", kind="openai_compat", base_url="http://127.0.0.1:9/claude/v1")
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))


async def _wire(messages: list[Message]) -> list[dict[str, Any]]:
    body = await _provider()._build_body(LLMRequest(model="claude-opus-5-5", messages=messages), stream=True)
    return json.loads(json.dumps(body["messages"]))


USER = Message(role=MessageRole.user, content_blocks=[TextBlock(text="check the dates")])


async def test_a_request_ending_on_the_models_prose_ends_with_a_user_turn() -> None:
    partial = Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="The first date is wr")])
    wire = await _wire([USER, partial])
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert wire[1]["content"] == "The first date is wr"
    assert wire[-1]["content"] == CUT_REPLY_NOTE


async def test_a_request_ending_on_a_user_turn_or_a_tool_call_is_left_as_it_is() -> None:
    assert [m["role"] for m in await _wire([USER])] == ["user"]
    call = Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Read", arguments_json='{"path": "a"}')])
    wire = await _wire([USER, call])
    assert wire[-1]["role"] == "assistant" and wire[-1]["tool_calls"]
