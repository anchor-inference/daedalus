"""A helper's short call — a translation, a title — asks the model for no thinking, and gets none.

Leaving thinking unset is not "off" on the current Claude models: Haiku 5.5 thinks by default, and a
translation given room for itself alone spent all of it thinking and came back empty or cut short. The
host says "none" to the key proxy's Claude routes, and the proxy says "off" in each model's own words.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx
import pytest
from protocore.contracts.llm import LLMRequest
from protocore.contracts.types import Message, MessageRole, TextBlock
from protocore.runtime.query import build_llm_request

from daedalus.config import keyproxy_base
from daedalus.host import translation
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint

KEYPROXY = Path(__file__).resolve().parents[2] / "deploy" / "keyproxy"
sys.path.insert(0, str(KEYPROXY))
_spec = importlib.util.spec_from_file_location("keyproxy_claude_off", KEYPROXY / "claude.py")
assert _spec is not None and _spec.loader is not None
claude_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(claude_mod)

HI = [{"role": "user", "content": "hi"}]


async def _body(base_url: str, extra: dict[str, object]) -> dict[str, object]:
    provider = OpenAICompatibleProvider(
        ProviderEndpoint(id="p", kind="openai_compat", base_url=base_url),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    request = LLMRequest(model="claude-haiku-5-5", messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])], max_tokens=100, extra=extra)
    return await provider._build_body(request, stream=False)


async def test_the_host_asks_the_claude_route_for_no_thinking_only_when_told() -> None:
    assert (await _body(keyproxy_base() + "/claude/v1", {"enable_thinking": False, "thinking_off": True}))["reasoning_effort"] == "none"
    # A call that states nothing about thinking leaves the route its own default.
    assert "reasoning_effort" not in await _body(keyproxy_base() + "/claude/v1", {})
    # Another provider knows no "none" and is told nothing new.
    assert "reasoning_effort" not in await _body(keyproxy_base() + "/openrouter/v1", {"enable_thinking": False, "thinking_off": True})


async def _run_body(base_url: str, *, thinking: bool, effort: str) -> dict[str, object]:
    """The body of a run's call, its request built by the core as every run's is."""
    provider = OpenAICompatibleProvider(
        ProviderEndpoint(id="p", kind="openai_compat", base_url=base_url),
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    request = build_llm_request(model="claude-haiku-5-5", messages=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="hi")])],
                                max_tokens=100, thinking_enabled=thinking, reasoning_effort=effort)
    return await provider._build_body(request, stream=False)


async def test_a_run_set_to_no_thinking_says_none_to_the_claude_route() -> None:
    """A run's "off" (an effort of "none", or the session chooser's off) is ``enable_thinking: False``; on the
    key proxy's Claude routes that once went out as no effort at all, and Haiku 5.5 thought anyway."""
    claude = keyproxy_base() + "/claude/v1"
    assert (await _run_body(claude, thinking=False, effort="medium"))["reasoning_effort"] == "none"
    assert (await _run_body(keyproxy_base() + "/anthropic/v1", thinking=False, effort="medium"))["reasoning_effort"] == "none"
    assert (await _run_body(claude, thinking=True, effort="high"))["reasoning_effort"] == "high"


async def test_a_run_set_to_no_thinking_elsewhere_is_sent_as_before() -> None:
    for base in (keyproxy_base() + "/openrouter/v1", "http://127.0.0.1:9/v1"):
        assert "reasoning_effort" not in await _run_body(base, thinking=False, effort="medium")
        assert (await _run_body(base, thinking=True, effort="low"))["reasoning_effort"] == "low"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-haiku-5-5", {"thinking": {"type": "disabled"}}),
        ("claude-sonnet-5-5", {"thinking": {"type": "between_tools"}}),
        ("claude-opus-5-5", {"output_config": {"effort": "low"}}),
        ("claude-fable-5-2", {"output_config": {"effort": "low"}}),
    ],
)
def test_the_proxy_turns_thinking_off_in_each_models_words(model: str, expected: dict[str, object]) -> None:
    out, _ = claude_mod.chat_to_messages({"model": model, "reasoning_effort": "none", "messages": HI})
    for key, value in expected.items():
        assert out[key] == value
    assert "context_management" not in out
    if "thinking" not in expected:
        assert "thinking" not in out


def test_a_model_that_thinks_only_on_request_is_sent_nothing() -> None:
    out, _ = claude_mod.chat_to_messages({"model": "claude-haiku-4-5", "reasoning_effort": "none", "messages": HI})
    assert "thinking" not in out and "output_config" not in out


def test_a_translation_has_room_for_a_longer_script() -> None:
    text = "x" * 2000
    assert translation.output_tokens(text) >= 2000


async def test_a_slow_translation_is_asked_again_and_the_first_answer_wins() -> None:
    calls: list[int] = []

    async def attempt() -> str:
        calls.append(len(calls))
        if len(calls) == 1:
            await asyncio.sleep(30)  # the request that sits upstream
        return f"answer {len(calls)}"

    assert await translation.Translations._first_of(attempt, 0.05, 5) == "answer 2"
    assert len(calls) == 2


async def test_a_quick_failure_is_not_asked_again_and_a_late_one_waits_for_the_other() -> None:
    calls: list[int] = []

    async def refused() -> str:
        calls.append(1)
        raise translation.TranslationFailed("the translator failed: refused")

    with pytest.raises(translation.TranslationFailed, match="refused"):
        await translation.Translations._first_of(refused, 1, 5)
    assert calls == [1]

    order: list[int] = []

    async def first_fails_late() -> str:
        order.append(len(order))
        if len(order) == 1:
            await asyncio.sleep(0.1)
            raise translation.TranslationFailed("the translation lost or repeated protected text")
        await asyncio.sleep(0.2)
        return "second"

    assert await translation.Translations._first_of(first_fails_late, 0.05, 5) == "second"


async def test_no_answer_at_all_is_a_timeout() -> None:
    async def silent() -> str:
        await asyncio.sleep(30)
        return ""

    with pytest.raises(translation.TranslationFailed, match="did not answer within 0.2 s"):
        await translation.Translations._first_of(silent, 0.05, 0.2)
