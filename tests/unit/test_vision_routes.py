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

from daedalus.config import ModelPresetConfig, RuntimeConfig, Settings, VisionConfig
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


@pytest.mark.parametrize(("provider", "model"), [("grok", "grok-4.7"), ("claude", "claude-sonnet-5-5"), ("codex", "gpt-6-luna")])
async def test_the_picture_reaches_a_subscription_route_with_the_configured_output_cap(tmp_path: Path, provider: str, model: str) -> None:
    store = FileBlobStore(tmp_path / "blobs")

    async def load(ref: str) -> tuple[bytes, str]:
        return await store.get(TENANT, ref), (await store.head(TENANT, ref)).content_type

    config = _config(f"{provider}.{model}", provider, model, 1500)
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
        answer, used = await vision.look((adapter, model, store, TENANT), manager, PICTURE, "image/png", "read the text", detail="full")
    finally:
        await adapter.aclose()
    assert (answer, used) == ("BLUE HERON 4721", model)
    body = sent[0]
    assert body["model"] == model and body["max_tokens"] == 1500
    parts = [part for message in body["messages"] if isinstance(message.get("content"), list) for part in message["content"]]
    images = [part["image_url"]["url"] for part in parts if part.get("type") == "image_url"]
    assert images == ["data:image/png;base64," + base64.b64encode(PICTURE).decode()]


class _Provider:
    def __init__(self, text: str = "", fail: Exception | None = None) -> None:
        self.text, self.fail, self.asked = text, fail, 0

    def accepts_images(self, model: str) -> bool:
        return True

    async def complete_text(self, request: Any) -> Any:
        from protocore.contracts.types import Message, MessageRole, TextBlock

        self.asked += 1
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=self.text)]))


class _RefusingStore:
    async def put(self, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied", "blobs")


async def test_a_store_that_cannot_write_says_the_model_was_never_asked() -> None:
    provider = _Provider("unused")
    with pytest.raises(vision.VisionUnavailable, match=r"could not be stored for the vision model grok-4\.7.*not asked"):
        await vision.look((provider, "grok-4.7", _RefusingStore(), TENANT), None, PICTURE, "image/png", "read it")
    assert provider.asked == 0


async def test_an_empty_answer_is_a_failure_and_not_a_description(tmp_path: Path) -> None:
    with pytest.raises(vision.VisionUnavailable, match=r"grok-4\.7 returned no text"):
        await vision.look((_Provider(""), "grok-4.7", FileBlobStore(tmp_path), TENANT), None, PICTURE, "image/png", "read it")


async def test_a_failing_route_names_the_model_and_the_reason(tmp_path: Path) -> None:
    provider = _Provider(fail=RuntimeError("HTTP 502: grok: not logged in"))
    with pytest.raises(vision.VisionUnavailable, match=r"vision model grok-4\.7 failed: HTTP 502: grok: not logged in"):
        await vision.look((provider, "grok-4.7", FileBlobStore(tmp_path), TENANT), None, PICTURE, "image/png", "read it")
