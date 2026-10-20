"""The screenshot a Computer call returns reaches the model that made the call, through the core.

Everything between the tool and the provider is the real thing: the ``Computer`` tool class, the core's
``QueryEngine`` (which moves the image into the run's blob store and keeps a reference in the history),
and the host's chat-completions provider with its wire, which sends the batch's pictures in one user
turn after the results, in the core's carrier shape. Only the far end is fake: the daemon
(``fake_deskd``) and the HTTP server the provider posts to (``httpx.MockTransport``).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
from protocore.contracts.runtime_constants import LoopConstants
from protocore.contracts.types import Message, MessageRole, TextBlock
from protocore.providers.openai_compat.wire import TOOL_IMAGES_CARRIER_HEADER
from protocore.runtime.query_engine import QueryEngine, QueryEngineConfig
from protocore.tests_support.adapters import (
    InMemoryBlobStore,
    InMemoryEventStream,
    InMemoryHookManager,
    InMemorySkillStore,
    InMemoryToolRegistry,
)

import daedalus.tools.computer as computer_tools
from daedalus.computer.agent import Caller, ComputerAgent
from daedalus.computer.wire import DeskClient
from daedalus.model_capabilities import CLAUDE_PROFILE
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint
from tests.support.fake_deskd import FakeDeskd

TENANT = "tenant-computer"
SESSION = "session-computer"


def _chunk(delta: dict[str, Any], finish: str | None = None) -> str:
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


async def test_the_screenshot_after_a_click_is_in_the_next_request(tmp_path: Path, monkeypatch: Any) -> None:
    desk = await FakeDeskd(tmp_path / "run").start()
    client = DeskClient(desk.run_dir)
    agent = ComputerAgent(client, settle=0)
    desk.grant(SESSION)
    monkeypatch.setattr(computer_tools, "_agent", lambda context: agent)
    monkeypatch.setattr(computer_tools, "_caller", lambda context: Caller(session_id=SESSION, profile=CLAUDE_PROFILE))

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            call = {"index": 0, "id": "call-shot", "type": "function", "function": {"name": "Computer", "arguments": json.dumps({"action": "screenshot"})}}
            text = _chunk({"role": "assistant", "tool_calls": [call]}) + _chunk({}, "tool_calls") + "data: [DONE]\n\n"
        else:
            text = _chunk({"role": "assistant", "content": "I see the editor."}) + _chunk({}, "stop") + "data: [DONE]\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    blobs = InMemoryBlobStore()

    async def loader(blob_ref: str) -> tuple[bytes, str]:
        data = await blobs.get(TENANT, blob_ref)
        assert data is not None
        return bytes(data), "image/jpeg"

    endpoint = ProviderEndpoint(id="claude", kind="openai_compat", base_url="https://model.test", api_key="k")
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), image_loader=loader, images_for=lambda _model: True)
    registry = InMemoryToolRegistry()
    registry.register(computer_tools.computer())
    engine = QueryEngine(
        config=QueryEngineConfig(run_id="run-computer", tenant_id=TENANT, account_id=TENANT, session_id=SESSION, model_name="claude-opus-5-5", rc=LoopConstants(model_context_window=200_000)),
        llm_provider=provider,
        tool_registry=registry,
        event_stream=InMemoryEventStream(),
        hook_manager=InMemoryHookManager(),
        skill_store=InMemorySkillStore(),
        blob_store=blobs,
    )
    try:
        async for _ in engine.run(Message(role=MessageRole.user, content_blocks=[TextBlock(text="look at my screen")])):
            pass
    finally:
        await client.close()
        await desk.stop()

    assert len(bodies) == 2
    sent = bodies[1]["messages"]
    tool_message = next(m for m in sent if m["role"] == "tool")
    assert "data on the operator's screen" in tool_message["content"]
    carrier = sent[sent.index(tool_message) + 1]
    assert carrier["role"] == "user" and carrier["content"][0] == {"type": "text", "text": TOOL_IMAGES_CARRIER_HEADER}
    assert carrier["content"][1]["text"] == "[images returned by tool call call-shot]"
    assert carrier["content"][2]["text"] == "[image 1 of tool call call-shot: 1356x847 pixels]"
    url = carrier["content"][3]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    picture = base64.b64decode(url.split(",", 1)[1])
    assert picture == agent.shots[SESSION].data, "the very image the tool was shown is what the model receives"
