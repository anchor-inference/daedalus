"""A tool's pictures through the key proxy: the agent core's carrier turn is folded into the results for
Claude, and reaches the Codex backend as a picture in a user turn after the outputs."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from protocore.contracts.llm import LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolResultImage, ToolUseBlock
from protocore.providers.openai_compat.wire import TOOL_IMAGES_CARRIER_HEADER as CORE_HEADER

from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint

KEYPROXY_DIR = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"
if str(KEYPROXY_DIR) not in sys.path:
    sys.path.insert(0, str(KEYPROXY_DIR))


def _load(name: str) -> Any:
    if name in sys.modules and getattr(sys.modules[name], "__file__", "") == str(KEYPROXY_DIR / f"{name}.py"):
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, KEYPROXY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


subs = _load("subscriptions")
claude_mod = _load("claude")

PIXELS = b"\x89PNG\r\n\x1a\nscreen"
ENCODED = base64.b64encode(PIXELS).decode()


def test_the_proxy_knows_the_cores_carrier_by_the_cores_own_words() -> None:
    assert subs.TOOL_IMAGES_CARRIER_HEADER == claude_mod.TOOL_IMAGES_CARRIER_HEADER == CORE_HEADER


async def _chat_body() -> dict[str, Any]:
    """What the bot sends for a batch of two calls whose second result carries a screenshot."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        chunk = {"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    async def loader(_ref: str) -> tuple[bytes, str]:
        return PIXELS, "image/png"

    calls = [ToolUseBlock(tool_call_id=f"c{i}", name="Screen", arguments_json="{}") for i in (1, 2)]
    messages = [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="click it")]),
        Message(role=MessageRole.assistant, content_blocks=calls),
        Message(role=MessageRole.tool, content_blocks=[
            ToolResultBlock(tool_call_id="c1", content="clicked"),
            ToolResultBlock(tool_call_id="c2", content="captured", images=(ToolResultImage(blob_ref="shot", width=1280, height=800),)),
        ]),
    ]
    endpoint = ProviderEndpoint(id="claude", kind="openai_compat", base_url="https://x.test", api_key="k")
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                        image_loader=loader, images_for=lambda _model: True)
    async for _ in provider.stream_with_tools(LLMRequest(model="claude-sonnet-5-5", messages=messages)):
        pass
    return captured["body"]


async def test_claude_gets_the_picture_inside_the_result_of_the_call_that_made_it() -> None:
    """In a user turn after the results Claude reads a screenshot as something the operator sent."""
    out, _names = claude_mod.chat_to_messages(await _chat_body())
    roles = [item["role"] for item in out["messages"]]
    assert roles == ["user", "assistant", "user"], "no turn of its own: the pictures went into the results"
    first, second = out["messages"][2]["content"]
    assert first["type"] == "tool_result" and first["tool_use_id"] == "c1" and first["content"] == "clicked"
    assert second["tool_use_id"] == "c2"
    blocks = second["content"]
    assert blocks[0] == {"type": "text", "text": "captured"}
    assert blocks[1] == {"type": "text", "text": "[image 1 of tool call c2: 1280x800 pixels]"}
    assert blocks[2]["type"] == "image" and blocks[2]["source"] == {"type": "base64", "media_type": "image/png", "data": ENCODED}
    assert CORE_HEADER not in json.dumps(out)


def test_a_carrier_naming_a_call_the_results_do_not_have_stays_a_user_turn() -> None:
    carrier = {"role": "user", "content": [{"type": "text", "text": CORE_HEADER}, {"type": "text", "text": "[images returned by tool call c9]"},
                                           {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ENCODED}"}}]}
    body = {"model": "claude-sonnet-5-5", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Screen", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "done"},
        carrier,
    ]}
    out, _names = claude_mod.chat_to_messages(body)
    assert [item["role"] for item in out["messages"]] == ["user", "assistant", "user", "user"]
    assert out["messages"][2]["content"][0]["content"] == "done"
    assert any(block.get("type") == "image" for block in out["messages"][3]["content"]), "nothing is lost"


def test_a_tool_message_with_its_own_picture_parts_keeps_them_in_the_result() -> None:
    body = {"model": "claude-sonnet-5-5", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Screen", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "captured"}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ENCODED}"}}]},
    ]}
    out, _names = claude_mod.chat_to_messages(body)
    [result] = out["messages"][2]["content"]
    assert [block["type"] for block in result["content"]] == ["text", "image"]


async def test_codex_gets_the_picture_as_an_input_image_after_the_outputs() -> None:
    responses = subs.chat_to_responses(await _chat_body())
    kinds = [item["type"] if item["type"] != "message" else f"message:{item['role']}" for item in responses["input"]]
    assert kinds == ["message:user", "function_call", "function_call", "function_call_output", "function_call_output", "message:user"]
    carrier = responses["input"][-1]["content"]
    assert carrier[0] == {"type": "input_text", "text": CORE_HEADER}
    assert {"type": "input_image", "image_url": f"data:image/png;base64,{ENCODED}", "detail": "auto"} in carrier


def test_codex_gets_a_tool_messages_own_picture_parts_after_the_batch_too() -> None:
    body = {"model": "gpt-6-sol", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "Screen", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "captured"}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{ENCODED}"}}]},
        {"role": "assistant", "content": "seen"},
    ]}
    items = subs.chat_to_responses(body)["input"]
    assert items[2] == {"type": "function_call_output", "call_id": "c1", "output": "captured"}
    assert items[3]["role"] == "user" and items[3]["content"][1]["type"] == "input_image"
    assert items[4]["role"] == "assistant"
