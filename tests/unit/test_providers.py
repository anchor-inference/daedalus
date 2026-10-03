from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import LLMRateLimitError, LLMRequest, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, TextBlock, ToolDefinition, ToolParameterSchema

from daedalus.providers.openai_compat import (
    OpenAICompatibleProvider,
    ProviderEndpoint,
    UsageRecord,
    normalize_usage,
    parse_json_text,
)
from daedalus.providers.wire import messages_to_wire, parse_json_arguments


def _sse(chunks: list[dict]) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}], "usage": usage}


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, record: UsageRecord) -> None:
        self.records.append(record)


def _provider(body: str, status: int = 200, sink: RecordingSink | None = None) -> OpenAICompatibleProvider:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})

    endpoint = ProviderEndpoint(id="deepseek", kind="deepseek", base_url="https://x.test", api_key="k")
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), usage_sink=sink)
    provider.captured = captured  # type: ignore[attr-defined]
    return provider


def _request(**extra) -> LLMRequest:
    return LLMRequest(
        model="deepseek-v4-flash",
        messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])],
        tools=[ToolDefinition(name="t", description="d", parameters=ToolParameterSchema(properties={"a": {"type": "string"}}, required=["a"]))],
        extra=extra,
    )


async def test_stream_orders_usage_before_finish_and_pairs_tool_calls() -> None:
    body = _sse(
        [
            _chunk({"role": "assistant", "reasoning_content": "think"}),
            _chunk({"content": "Hello"}),
            _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "t", "arguments": '{"a":'}}]}),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"b"}'}}]}),
            _chunk({}, finish="tool_calls"),
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "prompt_cache_hit_tokens": 4, "completion_tokens_details": {"reasoning_tokens": 2}}},
        ]
    )
    sink = RecordingSink()
    provider = _provider(body, sink=sink)
    kinds = []
    deltas = []
    async for delta in provider.stream_with_tools(_request(enable_thinking=True, reasoning_effort="low")):
        kinds.append(delta.kind)
        deltas.append(delta)
    assert kinds == [
        ProviderDeltaKind.thinking,
        ProviderDeltaKind.text,
        ProviderDeltaKind.tool_use_start,
        ProviderDeltaKind.tool_use_input,
        ProviderDeltaKind.tool_use_input,
        ProviderDeltaKind.tool_use_stop,
        ProviderDeltaKind.usage,
        ProviderDeltaKind.finish,
    ]
    stop = deltas[5]
    assert stop.tool_call_id == "call_1" and stop.tool_input_final == {"a": "b"} and not stop.args_partial_truncated
    assert deltas[6].usage["input_tokens"] == 10 and deltas[6].usage["cache_read_tokens"] == 4
    assert deltas[7].finish_reason == "tool_use"
    assert sink.records[0].normalized["reasoning_tokens"] == 2
    sent = provider.captured["json"]  # type: ignore[attr-defined]
    assert sent["thinking"] == {"type": "enabled"} and sent["reasoning_effort"] == "low"
    assert sent["stream_options"] == {"include_usage": True}
    assert sent["tools"][0]["function"]["name"] == "t"


async def test_truncated_tool_call_is_flagged() -> None:
    body = _sse(
        [
            _chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "t", "arguments": '{"a": "unfinished'}}]}),
            _chunk({}, finish="length"),
        ]
    )
    provider = _provider(body)
    stops = [d async for d in provider.stream_with_tools(_request()) if d.kind is ProviderDeltaKind.tool_use_stop]
    assert stops[0].args_partial_truncated and stops[0].truncated_by_output_cap
    assert stops[0].tool_input_final == {"a": "unfinished"}


async def test_rate_limit_maps_to_core_error() -> None:
    provider = _provider('{"error": {"message": "slow down"}}', status=429)
    with pytest.raises(LLMRateLimitError):
        async for _ in provider.stream_with_tools(_request()):
            pass


async def test_thinking_disabled_sends_disabled_flag() -> None:
    provider = _provider(_sse([_chunk({"content": "x"}, finish="stop")]))
    async for _ in provider.stream_with_tools(_request(enable_thinking=False)):
        pass
    sent = provider.captured["json"]  # type: ignore[attr-defined]
    assert sent["thinking"] == {"type": "disabled"} and "reasoning_effort" not in sent


def test_parse_json_text_strips_fences() -> None:
    assert parse_json_text('Sure:\n```json\n{"ok": true}\n```') == {"ok": True}
    assert parse_json_text('prefix {"a": 1} suffix') == {"a": 1}
    assert parse_json_text("no json") is None


def test_parse_json_arguments_repairs_truncation() -> None:
    assert parse_json_arguments('{"a": [1, 2') == {"a": [1, 2]}
    assert parse_json_arguments("") == {}


def test_normalize_usage_openai_shape() -> None:
    n = normalize_usage({"prompt_tokens": 7, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 5}})
    assert n["input_tokens"] == 7 and n["cache_read_tokens"] == 5 and n["total_tokens"] == 10


async def test_messages_to_wire_roundtrips_tool_history() -> None:
    from protocore.contracts.types import ToolResultBlock, ToolUseBlock

    messages = [
        Message(role=MessageRole.system, content_blocks=[TextBlock(text="sys")]),
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="do it")]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="t", arguments_json='{"a":"b"}')]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="done")]),
    ]
    wire = await messages_to_wire(messages)
    assert wire[0] == {"role": "system", "content": "sys"}
    assert wire[2]["tool_calls"][0]["id"] == "c1" and wire[2]["content"] is None
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": "done"}


async def test_dsml_markup_in_content_becomes_tool_calls() -> None:
    markup = (
        "Continuing.\n\n<｜｜DSML｜｜tool_calls>\n<｜｜DSML｜｜invoke name=\"t\">\n"
        "<｜｜DSML｜｜parameter name=\"a\" string=\"true\">hello</｜｜DSML｜｜parameter>\n"
        "</｜｜DSML｜｜invoke>\n</｜｜DSML｜｜tool_calls>"
    )
    body = _sse([_chunk({"content": markup[:30]}), _chunk({"content": markup[30:]}, finish="stop", usage={"prompt_tokens": 1, "completion_tokens": 1})])
    provider = _provider(body)
    deltas = [d async for d in provider.stream_with_tools(_request())]
    kinds = [d.kind for d in deltas]
    assert ProviderDeltaKind.tool_use_start in kinds and ProviderDeltaKind.tool_use_stop in kinds
    stop = next(d for d in deltas if d.kind is ProviderDeltaKind.tool_use_stop)
    assert stop.tool_name == "t" and stop.tool_input_final == {"a": "hello"}
    text = "".join(d.content or "" for d in deltas if d.kind is ProviderDeltaKind.text)
    assert "DSML" not in text and text.strip() == "Continuing."
    assert deltas[-1].finish_reason == "tool_use"


def test_builtin_pricing_and_peak_windows() -> None:
    from datetime import UTC, datetime

    from daedalus.providers.pricing import pricing_table

    table = pricing_table("deepseek", {})
    price = table["deepseek-v4-flash"]
    usage = {"input_tokens": 1_000_000, "cache_read_tokens": 0, "output_tokens": 0}
    peak = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)  # Monday 02:00 UTC
    off = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    weekend = datetime(2026, 9, 6, 2, 0, tzinfo=UTC)  # Sunday
    assert price.cost(usage, now=peak) == 0.30
    assert price.cost(usage, now=off) == 0.15
    assert price.cost(usage, now=weekend) == 0.15
    override = pricing_table("deepseek", {"deepseek-v4-flash": {"input": 1.0, "output": 2.0, "cache_hit": 0.1}})
    assert override["deepseek-v4-flash"].cost(usage, now=peak) == 1.0


def test_legacy_off_peak_pricing_entry_still_prices_off_peak() -> None:
    from datetime import UTC, datetime

    from daedalus.providers.pricing import ModelPricing

    legacy = ModelPricing.from_entry({"input": 0.44, "output": 1.32, "cache_hit": 0.014, "input_off_peak": 0.22, "off_peak_utc": "16:30-00:30"})
    usage = {"input_tokens": 1_000_000}
    assert legacy.cost(usage, now=datetime(2026, 9, 5, 20, 0, tzinfo=UTC)) == 0.22
    assert legacy.cost(usage, now=datetime(2026, 9, 5, 12, 0, tzinfo=UTC)) == 0.44


def test_pricing_prefers_the_longest_prefix_and_config_overrides() -> None:
    from daedalus.providers.openai_compat import ProviderEndpoint
    from daedalus.providers.pricing import pricing_table

    table = pricing_table("deepseek", {"deepseek-v4": {"input": 9.0, "output": 9.0, "cache_hit": 9.0}})
    endpoint = ProviderEndpoint(id="d", kind="deepseek", base_url="x", pricing=table)
    assert endpoint.pricing_for("deepseek-v4-flash-2027").input == 0.30  # built-in longer prefix wins
    assert endpoint.pricing_for("deepseek-v4-ultra").input == 9.0


def test_partial_price_edit_preserves_provider_bound_without_reusing_an_old_discount() -> None:
    from datetime import UTC, datetime

    from daedalus.providers.pricing import pricing_table

    table = pricing_table("deepseek", {"deepseek-flash": {"input": 1}, "unknown-model": {"input": 1}})
    known = table["deepseek-flash"]
    assert known.input_limit == 1_048_576 and known.limit_source
    assert known.output == 1.2
    assert known.cost({"input_tokens": 1_000_000}, now=datetime(2026, 9, 6, 12, tzinfo=UTC)) == 1.0
    assert table["unknown-model"].output is None and table["unknown-model"].input_limit is None
    cleared = pricing_table("deepseek", {"deepseek-flash": {"input_limit": None}})
    assert cleared["deepseek-flash"].input_limit is None


def test_dsml_guard_keeps_prose_after_the_block_and_marker_mentions() -> None:
    from daedalus.providers.dsml import DsmlGuard, parse_dsml

    text = 'before <|DSML|tool_calls><|DSML|invoke name="t"><|DSML|parameter name="n">7</|DSML|parameter><|DSML|parameter name="j">{"a":1}</|DSML|parameter></|DSML|invoke></|DSML|tool_calls> after'
    prose, calls = parse_dsml(text)
    assert prose == "before\n\nafter" and calls[0].arguments == {"n": "7", "j": {"a": 1}}
    g = DsmlGuard()
    shown = g.feed("the marker <|DS") + g.feed("ML| appears in prose only")
    rest, calls = g.finish()
    assert shown + rest == "the marker <|DSML| appears in prose only" and calls == []


async def test_structured_completion_returns_the_object_as_its_json_text() -> None:
    from protocore.contracts.llm import LLMResponse

    body = json.dumps({"choices": [{"message": {"content": '{"summary": "Ran ls; touched /srv/x.py"}'}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
    provider = _provider(body)
    response = await provider.complete_structured(_request(), {"type": "object"})
    assert isinstance(response, LLMResponse)
    assert json.loads(response.message.text)["summary"].startswith("Ran ls")
    assert provider.captured["json"]["response_format"] == {"type": "json_object"}  # type: ignore[attr-defined]


async def test_core_tier2_compaction_runs_through_the_provider() -> None:
    """The real core summariser, fed by our adapter: old turns collapse into <compacted-turn> summaries."""
    from protocore.contracts.types import ToolResultBlock, ToolUseBlock
    from protocore.runtime.context.compaction import CompactionState, run_tier2_summarisation
    from protocore.runtime.runtime_constants import default_runtime_constants

    body = json.dumps({"choices": [{"message": {"content": "## Progress\nListed /srv with Exec and read config.py."}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
    provider = _provider(body)
    history: list[Message] = [Message(role=MessageRole.user, content_blocks=[TextBlock(text="do the thing")])]
    for i in range(8):
        history.append(Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id=f"c{i}", name="Exec", arguments_json=json.dumps({"command": f"ls /srv/{i} && cat config{i}.py"}))]))
        history.append(Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id=f"c{i}", content=("file line\n" * 400))]))
    rc = default_runtime_constants(model_context_window=32_000, compaction_summary_max_output_tokens=1024)
    result = await run_tier2_summarisation(history, provider, CompactionState(), rc, model_name="deepseek-v4-flash")
    assert result.turns_summarised > 0 and result.tokens_freed > 0
    summaries = [m for m in history if m.metadata.get("protocore.compaction_summary")]
    assert summaries and "<compacted-turn" in summaries[0].text
    assert "Listed /srv with Exec and read config.py." in summaries[0].text
    # The summary was asked for as plain text, with the instruction as the system message.
    sent = provider.captured["json"]  # type: ignore[attr-defined]
    assert "response_format" not in sent and sent["messages"][0]["role"] == "system"
    # The most recent turns stay verbatim so the model knows where it stopped.
    assert history[-1].content_blocks[0].content.startswith("file line")  # type: ignore[union-attr]


async def test_reloaded_clients_survive_until_every_run_releases_them() -> None:
    from daedalus.config import Settings
    from daedalus.providers.registry import ProviderRegistry
    from tests.support.models import model_config

    config = model_config()
    registry = ProviderRegistry(Settings(), config)
    provider = registry.get("openrouter")
    with registry.hold([provider]):
        with registry.hold([provider]):
            changed = config.model_copy(deep=True)
            changed.providers["openrouter"].timeout_seconds += 1
            registry.reload(changed)
            await registry.close_retired()
            assert registry.get("openrouter") is not provider
            assert not provider._client.is_closed
        await registry.close_retired()
        assert not provider._client.is_closed
    await registry.close_retired()
    assert provider._client.is_closed
    with registry.hold([registry.get("openrouter")]):
        await registry.aclose()


def test_preset_rungs_put_the_chosen_model_first_then_the_chain() -> None:
    from daedalus.config import ModelPresetConfig, Settings
    from daedalus.providers.registry import ProviderRegistry
    from tests.support.models import model_config

    settings = Settings(deepseek_api_key="a", openrouter_api_key="b", vllm_base_url="http://vllm.test/v1")
    config = model_config()
    config.presets["vllm.Qwen3.6"] = ModelPresetConfig(provider="vllm", model="Qwen3.6", images=True)
    registry = ProviderRegistry(settings, config)
    rungs = registry.rungs_for(config, "vllm.Qwen3.6")
    assert rungs[0][0].endpoint.id == "vllm" and rungs[0][1] == "Qwen3.6"
    assert [(p.endpoint.id, m) for p, m in rungs[1:]] == [("openrouter", "deepseek/deepseek-v4-flash"), ("deepseek", "deepseek-flash")]
    assert registry.rungs_for(config, "nope")[0][0].endpoint.id == "deepseek"  # unknown preset -> default
    assert registry.rungs_for_pair(config, "vllm", "other")[0][1] == "other"
    # image capability is a property of the preset, read through the adapter
    assert rungs[0][0].accepts_images("Qwen3.6") is False  # no image loader wired in this registry



async def test_core_tier2_keeps_operator_turns_verbatim() -> None:
    from protocore.contracts.types import ToolResultBlock, ToolUseBlock
    from protocore.runtime.context.compaction import CompactionState, run_tier2_summarisation
    from protocore.runtime.runtime_constants import default_runtime_constants

    body = json.dumps({"choices": [{"message": {"content": "## Progress\ndid things"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
    provider = _provider(body)
    history: list[Message] = [Message(role=MessageRole.user, content_blocks=[TextBlock(text="do the thing")])]
    for i in range(4):
        history.append(Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id=f"c{i}", name="Exec", arguments_json='{"command": "ls"}')]))
        history.append(Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id=f"c{i}", content="x\n" * 400)]))
    history.append(Message(role=MessageRole.user, content_blocks=[TextBlock(text="remove the model-name field, keep only the global default")]))
    for i in range(4, 10):
        history.append(Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id=f"c{i}", name="Exec", arguments_json='{"command": "ls"}')]))
        history.append(Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id=f"c{i}", content="x\n" * 400)]))
    rc = default_runtime_constants(model_context_window=32_000, compaction_summary_max_output_tokens=1024)
    result = await run_tier2_summarisation(history, provider, CompactionState(), rc, model_name="m")
    assert result.turns_summarised > 0
    texts = [m.text for m in history if m.role is MessageRole.user]
    assert "remove the model-name field, keep only the global default" in texts


async def test_deepseek_gets_the_effort_names_it_knows() -> None:
    """``medium`` is DeepSeek's default ``high``; an unknown name would silently mean ``high`` for ``minimal`` too."""
    for asked, sent_as in (("medium", "high"), ("minimal", "low"), ("xhigh", "max"), ("low", "low")):
        provider = _provider(_sse([_chunk({"content": "ok"}), _chunk({}, finish="stop")]))
        async for _ in provider.stream_with_tools(_request(enable_thinking=True, reasoning_effort=asked)):
            pass
        assert provider.captured["json"]["reasoning_effort"] == sent_as  # type: ignore[attr-defined]


async def test_deepseek_thinking_request_gives_every_tool_call_turn_a_reasoning_content() -> None:
    """A tool-call turn made with thinking off has no reasoning; DeepSeek wants the key present anyway."""
    from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolUseBlock

    provider = _provider(_sse([_chunk({"content": "ok"}), _chunk({}, finish="stop")]))
    request = _request(enable_thinking=True, reasoning_effort="low")
    request = request.model_copy(update={"messages": [
        *request.messages,
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Reading."), ToolUseBlock(tool_call_id="c1", name="t", arguments_json="{}")]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="hello")]),
    ]})
    async for _ in provider.stream_with_tools(request):
        pass
    sent = provider.captured["json"]["messages"]  # type: ignore[attr-defined]
    turn = next(m for m in sent if m["role"] == "assistant" and m.get("tool_calls"))
    assert turn["reasoning_content"] == ""


async def test_deepseek_thinking_request_leaves_a_reasoning_only_turn_off_the_wire() -> None:
    """The partial the core keeps when a stream dies mid-thought has no text and no call: DeepSeek gets nothing for it."""
    from protocore.contracts.types import Message, MessageRole, ToolResultBlock, ToolUseBlock

    provider = _provider(_sse([_chunk({"content": "ok"}), _chunk({}, finish="stop")]))
    request = _request(enable_thinking=True, reasoning_effort="low")
    request = request.model_copy(update={"messages": [
        *request.messages,
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="t", arguments_json="{}")], reasoning_content="why"),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="hello")]),
        Message(role=MessageRole.assistant, content_blocks=[], reasoning_content="half a thought"),
    ]})
    async for _ in provider.stream_with_tools(request):
        pass
    sent = provider.captured["json"]["messages"]  # type: ignore[attr-defined]
    assert [m["role"] for m in sent][-2:] == ["assistant", "tool"]
    assert all("reasoning_content" in m for m in sent if m["role"] == "assistant")


async def test_opencode_sends_the_session_id_and_deepseek_shaped_thinking() -> None:
    """OpenCode Go routes and caches by conversation and passes DeepSeek's thinking fields through as they are."""
    from protocore.contracts.llm import LLMObservabilityContext

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, text=_sse([_chunk({"content": "ok"}), _chunk({}, finish="stop")]), headers={"content-type": "text/event-stream"})

    endpoint = ProviderEndpoint(id="opencode", kind="opencode", base_url="https://x.test", api_key="k")
    provider = OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), usage_sink=RecordingSink())
    request = _request(enable_thinking=True, reasoning_effort="medium").model_copy(update={"observability": LLMObservabilityContext(session_id="sess-42", run_id="run-1")})
    async for _ in provider.stream_with_tools(request):
        pass
    assert captured["headers"]["x-opencode-session"] == "sess-42"
    assert captured["headers"]["user-agent"].startswith("daedalus/")
    assert captured["json"]["thinking"] == {"type": "enabled"} and captured["json"]["reasoning_effort"] == "high"


async def test_an_unset_temperature_is_left_off_the_wire() -> None:
    """The core leaves the temperature to the server unless a caller states one; a null is not "unset" everywhere."""
    provider = _provider(_sse([_chunk({"content": "x"}, finish="stop")]))
    async for _ in provider.stream_with_tools(_request()):
        pass
    assert "temperature" not in provider.captured["json"]  # type: ignore[attr-defined]

    stated = _request()
    stated = stated.model_copy(update={"temperature": 0.2})
    provider = _provider(_sse([_chunk({"content": "x"}, finish="stop")]))
    async for _ in provider.stream_with_tools(stated):
        pass
    assert provider.captured["json"]["temperature"] == 0.2  # type: ignore[attr-defined]


async def test_a_required_tool_choice_is_rendered() -> None:
    provider = _provider(_sse([_chunk({"content": "x"}, finish="stop")]))
    async for _ in provider.stream_with_tools(_request(tool_choice_required=True)):
        pass
    assert provider.captured["json"]["tool_choice"] == "required"  # type: ignore[attr-defined]


async def test_a_context_refusal_carries_the_sizes_the_server_stated() -> None:
    from protocore.contracts.llm import LLMContextWindowExceeded

    body = json.dumps({"error": {"message": "This model's maximum context length is 256000 tokens. However, you requested 256023 tokens (190487 in the messages, 65536 in the completion). Please reduce the length of the messages or completion.", "type": "BadRequestError"}})
    provider = _provider(body, status=400)
    with pytest.raises(LLMContextWindowExceeded) as raised:
        async for _ in provider.stream_with_tools(_request()):
            pass
    assert (raised.value.context_window, raised.value.input_tokens, raised.value.requested_output_tokens) == (256000, 190487, 65536)

    bare = _provider(json.dumps({"error": {"message": "prompt is too long"}}), status=400)
    with pytest.raises(LLMContextWindowExceeded) as raised:
        async for _ in bare.stream_with_tools(_request()):
            pass
    assert raised.value.context_window is None and raised.value.input_tokens is None


async def test_a_summary_the_output_cap_cut_is_still_refused() -> None:
    from protocore.contracts.llm import LLMProviderError

    cut = '{"summary": "Turn 1: explored the repo; two tool calls; contents not yet"'
    body = json.dumps({"choices": [{"message": {"content": cut}, "finish_reason": "length"}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
    with pytest.raises(LLMProviderError, match="truncated"):
        await _provider(body).complete_structured(_request(), {"type": "object"})
