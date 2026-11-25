"""The marks on a broken or slow model: read off the refusals and the timings of calls already made, and
from the one check the operator asks for. Every call here is a fake; no test touches the network."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings
from daedalus.host import model_health
from daedalus.host.model_health import HealthUsageSink, ModelHealth, model_name, model_refusal, same_model
from daedalus.providers.registry import ProviderRegistry
from daedalus.stores.database import Database

CODEX = json.dumps({"error": {"message": "The 'gpt-6-sol' model is not supported when using Codex with a ChatGPT account.", "type": "invalid_request_error"}})
FREE_GONE = json.dumps({"error": {"message": "qwen/qwen3.8-27b:free is unavailable for free, the paid slug is qwen/qwen3.8-27b", "code": 404}})


def config(**presets: tuple[str, str]) -> RuntimeConfig:
    return RuntimeConfig(presets={pid: ModelPresetConfig(provider=provider, model=model, thinking=False) for pid, (provider, model) in presets.items()})


def test_the_same_model_is_told_apart_from_its_provider_spellings() -> None:
    assert same_model("deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash-1017")
    assert same_model("deepseek-v4.1-flash", "deepseek-flash")  # a provider's unversioned name is its current one
    assert same_model("claude-haiku-5-5", "anthropic/claude-haiku-5.5")
    assert same_model("qwen/qwen3.8-27b:free", "qwen3.8-27b")
    assert not same_model("gpt-6-sol", "gpt-6.1-sol")  # two versions that differ are two models
    assert not same_model("deepseek-v4.1-flash", "deepseek-v4.1-pro")
    assert not same_model("", "")
    assert model_name("deepseek/deepseek-v4.1-flash-20261017") == (frozenset({"deepseek", "flash"}), "4.1")


def test_only_a_refusal_about_the_model_itself_marks_it() -> None:
    assert model_refusal(400, CODEX) == "The 'gpt-6-sol' model is not supported when using Codex with a ChatGPT account."
    assert model_refusal(404, FREE_GONE).startswith("qwen/qwen3.8-27b:free is unavailable for free")
    assert model_refusal(404, '{"error": {"message": "No endpoints found for x/y."}}') == "No endpoints found for x/y."
    # The request's fault, not the model's.
    assert model_refusal(400, '{"error": {"message": "Image input is not supported by this model"}}') is None
    assert model_refusal(400, '{"error": {"message": "max_tokens is too large"}}') is None
    # Limits, keys and outages have handling of their own.
    assert model_refusal(429, CODEX) is None
    assert model_refusal(401, CODEX) is None


async def test_a_refusal_marks_the_preset_and_an_answer_clears_it(db: Database) -> None:
    health = ModelHealth(db)
    cfg = config(sol=("codex", "gpt-6-sol"), other=("codex", "gpt-6"))
    health.refused("codex", "gpt-6-sol", 400, CODEX)
    health.refused("codex", "gpt-6", 429, CODEX)
    assert health.broken(cfg, "sol") and health.broken(cfg, "other") is None
    view = await health.view(cfg)
    assert view["sol"]["state"] == "broken" and "not supported" in view["sol"]["detail"]
    await health.save()
    again = ModelHealth(db)
    await again.load()
    assert again.broken(cfg, "sol")  # a restart remembers it

    sink = SimpleNamespace(records=[])

    async def record(item: Any) -> None:
        sink.records.append(item)

    sink.record = record
    await HealthUsageSink(sink, again).record(SimpleNamespace(provider_id="codex", model="gpt-6-sol"))
    assert again.broken(cfg, "sol") is None and len(sink.records) == 1


async def _usage(db: Database, provider: str, model: str, *, first_byte: int | None, duration: int = 0, purpose: str = "stream", output: int = 10) -> None:
    await db.execute(
        "INSERT INTO usage_events(at,provider_id,model,purpose,output_tokens,duration_ms,raw,first_byte_ms) VALUES (?,?,?,?,?,?,?,?)",
        (datetime.now(UTC).isoformat(), provider, model, purpose, output, duration, "{}", first_byte),
    )


async def test_slow_first_bytes_over_recent_calls_mark_the_preset_slow(db: Database) -> None:
    health = ModelHealth(db)
    cfg = config(go=("opencode", "deepseek-v4.1-flash"), router=("openrouter", "deepseek/deepseek-v4.1-flash-1017"), new=("deepseek", "deepseek-flash"))
    for ms in (2500, 3200, 13600, 13600, 13800, 35000, 80000):
        await _usage(db, "opencode", "deepseek-v4.1-flash", first_byte=ms)
    for _ in range(6):
        # A short helper call sent without streaming: its duration is the wait.
        await _usage(db, "openrouter", "deepseek/deepseek-v4.1-flash-1017", first_byte=None, duration=600, purpose="translation")
    await _usage(db, "openrouter", "deepseek/deepseek-v4.1-flash-1017", first_byte=None, duration=90_000, purpose="compaction", output=4000)
    for _ in range(2):
        await _usage(db, "deepseek", "deepseek-flash", first_byte=20_000)
    view = await health.view(cfg)
    assert view["go"]["state"] == "slow" and view["go"]["median_ms"] == 13600
    assert view["router"] == {"state": "ok", "detail": "", "median_ms": 600, "calls": 6}  # the long summary is left out
    assert view["new"]["state"] == "ok"  # two calls say nothing yet


async def test_a_wait_abandoned_by_the_hedge_counts_toward_slowness(db: Database) -> None:
    health = ModelHealth(db)
    cfg = config(go=("opencode", "deepseek-v4.1-flash"))
    for _ in range(5):
        health.waited("opencode", "deepseek-v4.1-flash", 9000)
    health.waited("opencode", "deepseek-v4.1-flash", 1000)  # lost a race it never had time to run
    view = await health.view(cfg)
    assert view["go"]["state"] == "slow" and view["go"]["calls"] == 5


async def test_a_helper_on_a_broken_preset_gets_a_stand_in_and_the_log_says_so(db: Database, caplog: pytest.LogCaptureFixture) -> None:
    health = ModelHealth(db)
    cfg = config(go=("opencode", "deepseek-v4.1-flash"), haiku=("claude", "claude-haiku-5-5"), router=("openrouter", "deepseek/deepseek-v4.1-flash-1017"))
    health.refused("opencode", "deepseek-v4.1-flash", 404, '{"error": {"message": "model not found"}}')
    with caplog.at_level(logging.WARNING, logger="daedalus.host.model_health"):
        assert health.usable(cfg, "go", task="translation") == "router"
    assert "preset go is marked broken" in caplog.text and "using router instead" in caplog.text
    assert health.usable(cfg, "haiku") == "haiku"
    assert health.usable(cfg, "") == ""


async def test_the_check_sends_one_request_per_preset_and_reports_each(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_health, "CHECK_SECONDS", 0.1)
    health = ModelHealth(db)
    cfg = config(fine=("deepseek", "deepseek-flash"), sol=("codex", "gpt-6-sol"), stuck=("opencode", "deepseek-v4.1-flash"), down=("local", "llama"))
    health.refused("deepseek", "deepseek-flash", 404, '{"error": {"message": "model not found"}}')
    called: list[str] = []

    async def call(pid: str) -> str:
        called.append(pid)
        if pid == "sol":
            health.refused("codex", "gpt-6-sol", 400, CODEX)  # as the adapter does before raising
            raise RuntimeError("HTTP 400")
        if pid == "stuck":
            await asyncio.sleep(5)
        if pid == "down":
            raise ConnectionError("refused")
        return "OK"

    result = await health.check(cfg, call)
    assert sorted(called) == ["down", "fine", "sol", "stuck"]
    states = {pid: entry["state"] for pid, entry in result["results"].items()}
    assert states == {"fine": "ok", "sol": "broken", "stuck": "slow", "down": "failed"}
    assert health.broken(cfg, "fine") is None  # it answered, so its old mark went
    view = await health.view(cfg)
    assert view["sol"]["state"] == "broken" and view["stuck"]["state"] == "slow" and view["down"]["state"] == ""
    assert view["down"]["check"]["detail"].startswith("ConnectionError")


async def test_the_adapter_reports_a_refusal_but_not_a_routing_one() -> None:
    cfg = RuntimeConfig(
        providers={"codex": ProviderConfig(base_url="http://127.0.0.1:9/codex/v1"), "openrouter": ProviderConfig(kind="openrouter", base_url="http://127.0.0.1:9/openrouter/v1")},
        presets={"sol": ModelPresetConfig(provider="codex", model="gpt-6-sol"), "free": ModelPresetConfig(provider="openrouter", model="x/y")},
    )
    registry = ProviderRegistry(Settings(_env_file=None), cfg)  # type: ignore[call-arg]
    heard: list[tuple[str, str, int]] = []
    registry.on_model_refusal = lambda provider, model, status, text: heard.append((provider, model, status))
    answers = {
        "codex": httpx.Response(400, json=json.loads(CODEX)),
        "openrouter": httpx.Response(404, json={"error": {"message": "No allowed providers are available for the selected model.", "metadata": {"failed_routing_step": "Filter by Allowed Providers"}}}),
    }
    for provider, model in (("codex", "gpt-6-sol"), ("openrouter", "x/y")):
        adapter = registry.get(provider)
        adapter._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request, p=provider: answers[p]))
        with pytest.raises(Exception):  # noqa: B017 — the refusal is the core's error type; what matters is who heard it
            await adapter.complete_text(LLMRequest(model=model, messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])], max_tokens=16))
    assert heard == [("codex", "gpt-6-sol", 400)]
