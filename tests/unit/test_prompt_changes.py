"""Working-rules suggestions are bounded drafts, and only explicit review changes config."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from protocore.contracts.llm import ProviderDelta

from daedalus.config import ModelPresetConfig, RuntimeConfig
from daedalus.extensions.api import build_app
from daedalus.host.prompt_changes import KEY, PromptChangePlanner, digest, effective_rules
from tests.unit.test_components import HEAD, FakeApp


class MemoryDB:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def kv_get(self, key: str) -> Any:
        return self.values.get(key)

    async def kv_set(self, key: str, value: Any) -> None:
        self.values[key] = value


class Provider:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls
        self.requests = []

    async def stream_with_tools(self, request: Any) -> Any:
        self.requests.append(request)
        for index, (name, arguments) in enumerate(self.calls):
            yield ProviderDelta(kind="tool_use_start", tool_call_id=str(index), tool_name=name)
            yield ProviderDelta(kind="tool_use_stop", tool_call_id=str(index), tool_input_final=arguments)


def planner(provider: Any) -> PromptChangePlanner:
    preset = ModelPresetConfig(provider="test", model="model", max_output_tokens=16_000)
    config = RuntimeConfig(presets={"test": preset}, model={"preset": "test"})
    manager = SimpleNamespace(resolve_model=lambda _: ([(provider, "model")], preset), budget_exceeded=lambda: False, provider_costs_nothing=lambda *_: True)
    app = SimpleNamespace(config=config, manager=manager, db=MemoryDB())

    async def save_config(value: RuntimeConfig) -> None:
        app.config = value

    app.save_config = save_config
    return PromptChangePlanner(app)


@pytest.mark.asyncio
async def test_model_returns_reviewable_diff_and_cannot_apply_it() -> None:
    current = "Working rules:\n- Be concise.\n- Verify results."
    revised = "Working rules:\n- Be concise.\n- Ask before deleting files.\n- Verify results."
    instance = planner(Provider([("ProposeWorkingRules", {"rules": revised, "summary": "Added confirmation before deletion."})]))
    instance.app.config = instance.app.config.model_copy(update={"prompt": instance.app.config.prompt.model_copy(update={"rules": current})})
    started = await instance.start("ask before deleting files", "test")
    await instance.task
    proposal = await instance.proposal()
    assert proposal["id"] == started["id"] and proposal["state"] == "ready"
    assert "+- Ask before deleting files." in proposal["diff"]
    assert effective_rules(instance.app.config) == current
    assert [tool.name for tool in instance.app.manager.resolve_model({})[0][0][0].requests[0].tools] == ["ProposeWorkingRules"]

    result = await instance.approve(started["id"])
    assert result == {"applied": True, "rules": revised}
    assert await instance.approve(started["id"]) == result
    assert effective_rules(instance.app.config) == revised
    assert (await instance.app.db.kv_get(KEY))["state"] == "applied"


@pytest.mark.asyncio
async def test_stale_generation_cannot_replace_a_new_request() -> None:
    instance = planner(Provider([("ProposeWorkingRules", {"rules": "Working rules:\n- New.", "summary": "New"})]))
    value = await instance.start("new rule", "test")
    replacement = {**value, "generation_id": "replacement", "instruction": "another request"}
    await instance.app.db.kv_set(KEY, replacement)
    await instance.task
    assert await instance.app.db.kv_get(KEY) == replacement


@pytest.mark.asyncio
async def test_stale_proposal_cannot_replace_a_manual_edit() -> None:
    instance = planner(Provider([]))
    old = "Working rules:\n- Old."
    proposed = "Working rules:\n- Proposed."
    instance.app.config = instance.app.config.model_copy(update={"prompt": instance.app.config.prompt.model_copy(update={"rules": old})})
    await instance.app.db.kv_set(KEY, {"id": "p", "state": "ready", "base_digest": digest(old), "rules": proposed})
    instance.app.config = instance.app.config.model_copy(update={"prompt": instance.app.config.prompt.model_copy(update={"rules": "Working rules:\n- Manual."})})
    with pytest.raises(ValueError, match="changed after"):
        await instance.approve("p")
    assert "Manual" in effective_rules(instance.app.config)


@pytest.mark.asyncio
@pytest.mark.parametrize("calls", [[], [("OtherTool", {})], [("ProposeWorkingRules", {"rules": "same", "summary": "one"}), ("ProposeWorkingRules", {"rules": "other", "summary": "two"})]])
async def test_invalid_model_output_never_creates_a_review(calls: list[tuple[str, dict[str, Any]]]) -> None:
    instance = planner(Provider(calls))
    with pytest.raises(ValueError):
        await instance._run({"id": "a" * 32, "preset": "test", "instruction": "change"}, "same")


def test_prompt_change_view_requires_authentication(tmp_path: Any) -> None:
    app = FakeApp(tmp_path, native=True)
    app.db = MemoryDB()
    with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
        assert client.get("/api/prompt-change").status_code == 401
        response = client.get("/api/prompt-change", headers=HEAD)
        assert response.status_code == 200
        assert response.json() == {"proposal": None, "models": []}
