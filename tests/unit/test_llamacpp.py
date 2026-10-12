from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from protocore.contracts.llm import LLMRateLimitError, LLMRequest, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolDefinition, ToolParameterSchema
from protocore.tools.memory import build_memory_tools

from daedalus import doctor
from daedalus.config import ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings
from daedalus.extensions import api as api_module
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.providers.llamacpp import LlamaCppDiscovery, discover_llamacpp, tools_to_llamacpp_wire
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint, UsageRecord
from daedalus.providers.pricing import ModelPricing
from daedalus.providers.registry import ProviderRegistry
from daedalus.providers.wire import tools_to_wire
from daedalus.tools import discover_tools


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _props(*, sleeping: bool = False) -> dict[str, Any]:
    return {
        "default_generation_settings": {"params": {"n_ctx": 128000}},
        "model_path": "model.gguf",
        "model_alias": "local-model",
        "chat_template": "template",
        "chat_template_caps": {"supports_tools": True, "supports_parallel_tool_calls": True},
        "modalities": {"vision": True, "audio": False},
        "total_slots": 3,
        "is_sleeping": sleeping,
        "build_info": "b1234",
    }


def _models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": "local-model", "object": "model", "meta": {"n_ctx": 128000}}],
        "models": [{"name": "local-model", "model": "local-model", "capabilities": ["completion"], "details": {"format": "gguf"}}],
    }


async def test_discovery_reads_full_props_and_models() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_props() if request.url.path == "/props" else _models())

    client = _client(handler)
    try:
        result = await discover_llamacpp("http://127.0.0.1:8080/v1", "test-key", client=client)
    finally:
        await client.aclose()
    assert result.reachable is True and result.reason == ""
    assert result.model_id == "local-model" and result.context_window == 128000
    assert result.tools is True and result.images is True and result.slots == 3
    assert result.build == "b1234" and result.sleeping is False
    assert [request.url.path for request in seen] == ["/props", "/v1/models"]
    assert all(request.headers.get("authorization") == "Bearer test-key" for request in seen)


async def test_discovery_falls_back_when_props_is_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(404, json={"error": {"message": "not exposed"}})
        return httpx.Response(200, json=_models())

    client = _client(handler)
    try:
        result = await discover_llamacpp("http://127.0.0.1:8080", client=client)
    finally:
        await client.aclose()
    assert result.reachable is True and result.base_url == "http://127.0.0.1:8080/v1"
    assert result.model_id == "local-model" and result.context_window == 128000
    assert result.tools is None and result.images is None
    assert result.slots is None and result.build is None and result.sleeping is None


async def test_discovery_returns_a_reason_for_nonsense() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json={"hello": "world"})

    client = _client(handler)
    try:
        result = await discover_llamacpp("http://127.0.0.1:8080/v1", client=client)
    finally:
        await client.aclose()
    assert result.reachable is False
    assert "props: not JSON" in result.reason and "models: no model list" in result.reason
    assert result.model_id is None and result.context_window is None and result.tools is None


def _sse(chunks: list[dict[str, Any]]) -> str:
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _request() -> LLMRequest:
    return LLMRequest(
        model="local-model",
        messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hello")])],
        tools=[ToolDefinition(name="weather", description="Read weather", parameters=ToolParameterSchema(properties={"city": {"type": "string"}}, required=["city"]))],
        extra={"enable_thinking": True, "reasoning_effort": "high"},
    )


def test_llamacpp_removes_the_rejected_nested_max_length_boundary() -> None:
    tool = ToolDefinition(
        name="ask",
        description="Ask a question",
        parameters=ToolParameterSchema(
            properties={
                "questions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string", "maxLength": 2000},
                            "short": {"type": "string", "maxLength": 1999},
                            "larger": {"type": "string", "maxLength": 2001},
                        },
                        "required": ["question"],
                    },
                }
            },
            required=["questions"],
        ),
    )
    before = tools_to_wire([tool])[0]
    after = tools_to_llamacpp_wire([tool])[0]
    nested = after["function"]["parameters"]["properties"]["questions"]["items"]
    assert "maxLength" not in nested["properties"]["question"]
    assert nested["properties"]["short"]["maxLength"] == 1999
    assert nested["properties"]["larger"]["maxLength"] == 2001
    assert before["function"]["parameters"]["properties"]["questions"]["items"]["properties"]["question"]["maxLength"] == 2000


def test_llamacpp_translation_preserves_every_registry_tool_and_required_argument() -> None:
    definitions = [tool.definition for tool in discover_tools()]
    definitions += [tool.definition for tool in build_memory_tools(object())]
    before = tools_to_wire(definitions)
    after = tools_to_llamacpp_wire(definitions)
    expected = {
        entry["function"]["name"]: entry["function"]["parameters"].get("required", [])
        for entry in before
    }
    actual = {
        entry["function"]["name"]: entry["function"]["parameters"].get("required", [])
        for entry in after
    }
    assert actual == expected
    question = next(entry for entry in after if entry["function"]["name"] == "AskUser")
    nested = question["function"]["parameters"]["properties"]["questions"]["items"]
    assert nested["required"] == ["question"]
    assert "maxLength" not in nested["properties"]["question"]


def test_llamacpp_omits_only_an_untranslatable_tool_and_logs_its_name(caplog: pytest.LogCaptureFixture) -> None:
    broken = SimpleNamespace(
        name="BrokenTool",
        description="broken",
        parameters=SimpleNamespace(model_dump=lambda **kwargs: (_ for _ in ()).throw(ValueError("bad schema"))),
    )
    good = ToolDefinition(name="GoodTool", description="good", parameters=ToolParameterSchema(properties={}, required=[]))
    assert [entry["function"]["name"] for entry in tools_to_llamacpp_wire([broken, good])] == ["GoodTool"]  # type: ignore[list-item]
    assert "llama.cpp omitted tool BrokenTool" in caplog.text


def _provider(handler: Any, sink: Any = None) -> tuple[OpenAICompatibleProvider, dict[str, Any]]:
    captured: dict[str, Any] = {}

    def recording(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return handler(request)

    endpoint = ProviderEndpoint(
        id="local",
        kind="llamacpp",
        base_url="http://127.0.0.1:8080/v1",
        pricing={"local-model": ModelPricing(input=9, output=9, cache_hit=9)},
    )
    provider = OpenAICompatibleProvider(endpoint, client=_client(recording), usage_sink=sink)
    return provider, captured


async def test_llamacpp_tool_call_round_trip_uses_its_request_dialect() -> None:
    body = _sse(
        [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "weather", "arguments": '{"city":"Almaty"}'}}]}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 20, "completion_tokens": 6}},
        ]
    )
    provider, captured = _provider(lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    try:
        deltas = [delta async for delta in provider.stream_with_tools(_request())]
    finally:
        await provider.aclose()
    stop = next(delta for delta in deltas if delta.kind is ProviderDeltaKind.tool_use_stop)
    assert stop.tool_name == "weather" and stop.tool_input_final == {"city": "Almaty"}
    sent = captured["body"]
    assert sent["cache_prompt"] is True and sent["tools"][0]["function"]["name"] == "weather"
    assert "thinking" not in sent and "reasoning" not in sent
    # Bonsai's template rejects `high` (low | medium | xhigh) and defaults to xhigh when nothing is sent.
    assert sent["reasoning_effort"] == "xhigh"
    assert sent["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "xhigh"}


async def test_llamacpp_thinking_off_sends_the_template_switch_without_an_effort() -> None:
    body = _sse(
        [
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 1}},
        ]
    )
    provider, captured = _provider(lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    request = _request().model_copy(update={"extra": {"enable_thinking": False, "reasoning_effort": "low"}, "tools": []})
    try:
        [delta async for delta in provider.stream_with_tools(request)]
    finally:
        await provider.aclose()
    sent = captured["body"]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_effort" not in sent


async def test_llamacpp_keeps_low_and_medium_effort_names() -> None:
    body = _sse(
        [
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 4, "completion_tokens": 1}},
        ]
    )
    for asked in ("low", "medium", "xhigh"):
        provider, captured = _provider(lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
        request = _request().model_copy(update={"extra": {"enable_thinking": True, "reasoning_effort": asked}, "tools": []})
        try:
            [delta async for delta in provider.stream_with_tools(request)]
        finally:
            await provider.aclose()
        assert captured["body"]["reasoning_effort"] == asked
        assert captured["body"]["chat_template_kwargs"]["reasoning_effort"] == asked


async def test_llamacpp_streaming_round_trip_normalizes_cached_usage() -> None:
    body = _sse(
        [
            {"choices": [{"delta": {"content": "Hello "}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "there"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 14, "completion_tokens": 2, "prompt_tokens_details": {"cached_tokens": 9}}},
        ]
    )
    provider, _ = _provider(lambda request: httpx.Response(200, text=body, headers={"content-type": "text/event-stream"}))
    try:
        deltas = [delta async for delta in provider.stream_with_tools(_request())]
    finally:
        await provider.aclose()
    assert "".join(delta.content or "" for delta in deltas if delta.kind is ProviderDeltaKind.text) == "Hello there"
    usage = next(delta.usage for delta in deltas if delta.kind is ProviderDeltaKind.usage)
    assert usage["input_tokens"] == 14 and usage["cache_read_tokens"] == 9
    assert deltas[-1].kind is ProviderDeltaKind.finish


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, record: UsageRecord) -> None:
        self.records.append(record)


async def test_llamacpp_usage_is_always_recorded_at_zero_cost() -> None:
    response = {
        "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "cost": 123.0},
    }
    sink = RecordingSink()
    provider, _ = _provider(lambda request: httpx.Response(200, json=response), sink)
    try:
        completed = await provider.complete_text(_request())
    finally:
        await provider.aclose()
    assert completed.usage.response_cost_usd == 0.0
    assert sink.records[0].cost_usd == 0.0 and sink.records[0].normalized["cost_usd"] == 0.0


async def test_sleep_state_is_discovered_and_http_503_is_retryable() -> None:
    def discover_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_props(sleeping=True) if request.url.path == "/props" else _models())

    client = _client(discover_handler)
    try:
        result = await discover_llamacpp("http://127.0.0.1:8080/v1", client=client)
    finally:
        await client.aclose()
    assert result.sleeping is True

    provider, _ = _provider(lambda request: httpx.Response(503, json={"error": {"message": "no slot is available while the model loads"}}))
    try:
        with pytest.raises(LLMRateLimitError, match="busy"):
            async for _ in provider.stream_with_tools(_request()):
                pass
    finally:
        await provider.aclose()


async def test_registry_wires_llamacpp_as_a_free_openai_compatible_provider() -> None:
    config = RuntimeConfig(
        providers={"local": ProviderConfig(kind="llamacpp", name="Desk model", base_url="http://127.0.0.1:8080/v1", pricing={"local-model": {"input": 5, "output": 10}})},
        presets={"local.model": ModelPresetConfig(provider="local", model="local-model")},
        model={"preset": "local.model"},
    )
    registry = ProviderRegistry(Settings(_env_file=None), config)  # type: ignore[call-arg]
    try:
        provider, model = registry.rungs_for(config)[0]
        assert provider.endpoint.kind == "llamacpp" and model == "local-model"
        assert provider.endpoint.pricing == {}
    finally:
        await registry.aclose()


async def test_doctor_reports_llamacpp_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_discovery(base_url: str, api_key: str | None = None, *, client: httpx.AsyncClient | None = None) -> LlamaCppDiscovery:
        return LlamaCppDiscovery(reachable=True, model_id="local-model", context_window=128000, tools=True, slots=3, sleeping=False)

    monkeypatch.setattr(doctor, "discover_llamacpp", fake_discovery)
    config = RuntimeConfig(providers={"local": ProviderConfig(kind="llamacpp", name="Desk model", base_url="http://127.0.0.1:8080/v1")})
    checks = await doctor._providers(doctor.DoctorContext(settings=Settings(_env_file=None), config=config))  # type: ignore[call-arg]
    assert len(checks) == 1 and checks[0].name == "llama.cpp Desk model" and checks[0].ok is True
    assert "model=local-model" in checks[0].message and "context=128000" in checks[0].message
    assert "tools=true" in checks[0].message and "slots=3" in checks[0].message


async def test_spending_caps_do_not_refuse_a_zero_cost_llamacpp_run(settings: Settings, db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    config = RuntimeConfig(
        providers={"local": ProviderConfig(kind="llamacpp", base_url="http://127.0.0.1:8080/v1")},
        presets={"local.model": ModelPresetConfig(provider="local", model="local-model")},
        model={"preset": "local.model"},
    )
    config.limits.usd_total = 1.0
    manager = SessionManager(settings, config, db=db)
    await manager.start(recovering=False)
    state = await manager.create_session("local")
    await manager.usage.record(
        UsageRecord(provider_id="hosted", model="paid", purpose="stream", raw={}, normalized={}, cost_usd=5.0, duration_ms=1, run_id="older", session_id=state.session.id)
    )
    await manager.set_session_cap(state.session.id, 1.0)
    manager.budget_flag.parent.mkdir(parents=True, exist_ok=True)
    manager.budget_flag.write_text("hosted daily cap")

    async def fake_start(_state: Any, _message: Any, *, continue_turn: bool = False) -> str:
        return "local-run"

    monkeypatch.setattr(manager, "_start_run", fake_start)
    try:
        assert await manager.cap_breach(state, "local") is None
        assert await manager.submit(state.session.id, "hello") == "local-run"
    finally:
        await manager.close()


class FakeProviders:
    def available(self) -> list[str]:
        return ["local"]

    async def close_retired(self) -> None:
        return None


def test_add_model_lookup_prefills_discovered_values_through_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    config = RuntimeConfig(providers={"local": ProviderConfig(kind="llamacpp", base_url="http://127.0.0.1:8080/v1")})
    app = SimpleNamespace(settings=settings, config=config, manager=SimpleNamespace(providers=FakeProviders()), front=None, extensions={})

    async def fake_discovery(base_url: str, api_key: str | None = None) -> Any:
        client = _client(lambda request: httpx.Response(200, json=_props() if request.url.path == "/props" else _models()))
        try:
            return await discover_llamacpp(base_url, api_key, client=client)
        finally:
            await client.aclose()

    monkeypatch.setattr(api_module, "discover_llamacpp", fake_discovery)
    with TestClient(build_app(app, "token")) as client:  # type: ignore[arg-type]
        response = client.post("/api/providers/lookup-models", json={"provider": "local"}, headers={"X-Daedalus-Token": "token"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["models"] == ["local-model"]
    assert payload["entries"] == [{"id": "local-model", "context_length": 128000, "context_source": "provider", "images": True, "input_modalities": ["text", "image"]}]
    assert payload["discovery"]["tools"] is True and payload["discovery"]["slots"] == 3
