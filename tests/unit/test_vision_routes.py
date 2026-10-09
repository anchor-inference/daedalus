"""The image-understanding model can be any image-capable model, the subscription logins included.

On a native Windows install the operator set Grok 4.7 as the vision model and every ImageView died
with "Permission denied" on the blob store's directory: the picture never left the machine, and the
failure read as the subscription route not taking images. These tests follow a picture from the
configuration through the registry to the wire of each subscription route, and hold the failures
to saying which side failed.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import LLMResponse, LLMResponseUsage
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.config import ModelPresetConfig, RuntimeConfig, Settings, VisionConfig
from daedalus.host.setting_refs import VISION_OUTPUT
from daedalus.providers.registry import ProviderRegistry
from daedalus.stores import blobs as blob_module
from daedalus.stores.blobs import FileBlobStore
from daedalus.tools import vision

PICTURE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
TENANT = "daedalus"


def _windows_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behave as Windows does: a directory cannot be opened with ``os.open``."""
    real_open = os.open

    def refusing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.path.isdir(path):
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(blob_module.os, "open", refusing_open)
    monkeypatch.setattr(blob_module, "WINDOWS", True, raising=False)


async def test_a_blob_is_stored_where_a_directory_cannot_be_opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _windows_directories(monkeypatch)
    store = FileBlobStore(tmp_path / "blobs")
    meta = await store.put(TENANT, PICTURE, content_type="image/png")
    assert await store.get(TENANT, meta.ref) == PICTURE
    assert (await store.head(TENANT, meta.ref)).content_type == "image/png"


def _config(preset_id: str, provider: str, model: str, max_output: int) -> RuntimeConfig:
    config = RuntimeConfig()
    config.presets[preset_id] = ModelPresetConfig(provider=provider, model=model, images=True)
    config.vision = VisionConfig(preset=preset_id, max_output_tokens=max_output)
    return config


@pytest.mark.parametrize("detail", ["full", "focused"])
@pytest.mark.parametrize(("provider", "model"), [("grok", "grok-4.7"), ("claude", "claude-sonnet-5-5"), ("codex", "gpt-6-luna")])
async def test_the_picture_reaches_a_subscription_route_with_the_configured_output_cap(tmp_path: Path, provider: str, model: str, detail: str) -> None:
    # The operator's Max output tokens is the budget in both modes. A focused answer was capped at 800
    # behind it, and Claude Haiku spent the 800 thinking and wrote nothing although 16384 was set.
    store = FileBlobStore(tmp_path / "blobs")

    async def load(ref: str) -> tuple[bytes, str]:
        return await store.get(TENANT, ref), (await store.head(TENANT, ref)).content_type

    config = _config(f"{provider}.{model}", provider, model, 16384)
    registry = ProviderRegistry(Settings(), config, image_loader=load)
    found = config.vision_preset()
    assert found is not None and found[1].provider == provider
    adapter = registry.get(provider)
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/{provider}/v1/chat/completions")
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "BLUE HERON 4721"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 9, "completion_tokens": 4}})

    await adapter._client.aclose()
    adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        manager = SimpleNamespace(config=config)
        answer, used = await vision.look((adapter, model, store, TENANT), manager, PICTURE, "image/png", "read the text", detail=detail)
    finally:
        await adapter.aclose()
    assert (answer, used) == ("BLUE HERON 4721", model)
    body = sent[0]
    assert body["model"] == model and body["max_tokens"] == 16384
    assert "reasoning_effort" not in body, "a perception task asked the route to think"
    parts = [part for message in body["messages"] if isinstance(message.get("content"), list) for part in message["content"]]
    images = [part["image_url"]["url"] for part in parts if part.get("type") == "image_url"]
    assert images == ["data:image/png;base64," + base64.b64encode(PICTURE).decode()]


class _Provider:
    def __init__(self, text: str = "", fail: Exception | None = None) -> None:
        self.text, self.fail, self.asked = text, fail, 0

    def accepts_images(self, model: str) -> bool:
        return True

    async def complete_text(self, request: Any) -> Any:
        self.asked += 1
        if self.fail is not None:
            raise self.fail
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=self.text)]), stop_reason=StopReason.end_turn)


class _RefusingStore:
    async def put(self, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied", "blobs")


async def test_a_store_that_cannot_write_says_the_model_was_never_asked() -> None:
    provider = _Provider("unused")
    with pytest.raises(vision.VisionUnavailable, match=r"could not be stored for the vision model grok-4\.7.*not asked"):
        await vision.look((provider, "grok-4.7", _RefusingStore(), TENANT), None, PICTURE, "image/png", "read it")
    assert provider.asked == 0


async def test_an_empty_answer_is_a_failure_and_not_a_description(tmp_path: Path) -> None:
    with pytest.raises(vision.VisionUnavailable, match=r"grok-4\.7 finished without writing an answer"):
        await vision.look((_Provider(""), "grok-4.7", FileBlobStore(tmp_path), TENANT), None, PICTURE, "image/png", "read it")


async def test_a_failing_route_names_the_model_and_the_reason(tmp_path: Path) -> None:
    provider = _Provider(fail=RuntimeError("HTTP 502: grok: not logged in"))
    with pytest.raises(vision.VisionUnavailable, match=r"vision model grok-4\.7 failed: HTTP 502: grok: not logged in"):
        await vision.look((provider, "grok-4.7", FileBlobStore(tmp_path), TENANT), None, PICTURE, "image/png", "read it")


class _Thinker:
    """A model that thinks ``cost`` tokens before it writes, the way Claude's fifth generation does.

    ``always`` thinks whatever the request says (a model whose thinking cannot be switched off);
    otherwise it thinks only when the request asks for thinking. The answer is written only when the
    budget has room left after the thinking; a budget the thinking fills ends with no text at all.
    """

    def __init__(self, cost: int, *, always: bool) -> None:
        self.cost, self.always = cost, always
        self.budgets: list[int] = []
        self.thinking: list[bool] = []

    def accepts_images(self, model: str) -> bool:
        return True

    async def complete_text(self, request: Any) -> LLMResponse:
        asked = bool((request.extra or {}).get("enable_thinking", False))
        self.budgets.append(request.max_tokens)
        self.thinking.append(asked)
        spent = self.cost if (self.always or asked) else 0
        usage = LLMResponseUsage(input_tokens=1600, output_tokens=min(spent, request.max_tokens))
        if spent >= request.max_tokens:
            return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[]), stop_reason=StopReason.max_tokens, usage=usage)
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="a red Deploy button")]), stop_reason=StopReason.end_turn, usage=usage)


def _manager(max_output: int) -> Any:
    return SimpleNamespace(config=SimpleNamespace(vision=VisionConfig(max_output_tokens=max_output), model=SimpleNamespace(fallback_to_session=False)))


async def test_a_focused_look_sends_the_operators_budget_and_asks_for_no_thinking(tmp_path: Path) -> None:
    model = _Thinker(cost=5000, always=False)
    answer, _ = await vision.look((model, "claude-haiku-5-5", FileBlobStore(tmp_path), TENANT), _manager(16384), PICTURE, "image/png", "what is on it?", detail="concise")
    assert answer == "a red Deploy button"
    assert model.budgets == [16384] and model.thinking == [False]


async def test_a_model_that_thinks_anyway_gets_its_answer_with_a_small_budget(tmp_path: Path) -> None:
    # 1000 of budget, 1500 of thinking: the first reply is cut off empty, the one retry at twice the
    # budget has room for the answer.
    model = _Thinker(cost=1500, always=True)
    answer, used = await vision.look((model, "claude-haiku-5-5", FileBlobStore(tmp_path), TENANT), _manager(1000), PICTURE, "image/png", "what is on it?")
    assert (answer, used) == ("a red Deploy button", "claude-haiku-5-5")
    assert model.budgets == [1000, 2000]


async def test_thinking_that_fills_even_the_retry_is_named_and_opens_the_budget_row(tmp_path: Path) -> None:
    model = _Thinker(cost=50_000, always=True)
    with pytest.raises(vision.VisionUnavailable, match=r"used all 4000 of its output tokens even when asked a second time.*most likely on thinking") as failure:
        await vision.look((model, "claude-haiku-5-5", FileBlobStore(tmp_path), TENANT), _manager(2000), PICTURE, "image/png", "what is on it?")
    assert model.budgets == [2000, 4000]
    assert failure.value.setting == VISION_OUTPUT and failure.value.setting.key == "vision.max_output_tokens"


async def test_a_budget_at_the_ceiling_is_not_retried(tmp_path: Path) -> None:
    model = _Thinker(cost=50_000, always=True)
    with pytest.raises(vision.VisionUnavailable, match=r"used all 32000 of its output tokens before writing"):
        await vision.look((model, "claude-haiku-5-5", FileBlobStore(tmp_path), TENANT), _manager(32_000), PICTURE, "image/png", "what is on it?")
    assert model.budgets == [32_000]


def _empty(stop: StopReason, input_tokens: int = 0, output_tokens: int = 0) -> LLMResponse:
    return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[]), stop_reason=stop,
                       usage=LLMResponseUsage(input_tokens=input_tokens, output_tokens=output_tokens))


def test_the_empty_answer_says_which_of_the_causes_it_was() -> None:
    # The old sentence offered thinking and a dropped picture at once; they want different fixes.
    spent = vision.empty_answer_reason("m", _empty(StopReason.max_tokens, 1600, 800), 800, 60)
    assert "used all 800 of its output tokens" in spent and "thinking" in spent and "dropped" not in spent
    dropped = vision.empty_answer_reason("m", _empty(StopReason.end_turn, 90, 3), 16384, 60)
    assert "only 90 tokens" in dropped and "dropped the image" in dropped and "thinking" not in dropped
    reached = vision.empty_answer_reason("m", _empty(StopReason.end_turn, 1600, 3), 16384, 60)
    assert "picture reached it (1600 prompt tokens)" in reached and "dropped" not in reached
    unknown = vision.empty_answer_reason("m", _empty(StopReason.end_turn), 16384, 60)
    assert "reported no usage" in unknown and "cannot be told" in unknown
