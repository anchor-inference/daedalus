"""Provider fallback chain: one-way cursor and the reason behind every demotion."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from daedalus.providers.chain import ProviderChain, build_chain
from daedalus.providers.openai_compat import OpenAICompatibleProvider, ProviderEndpoint


def _provider(endpoint_id: str) -> OpenAICompatibleProvider:
    endpoint = ProviderEndpoint(id=endpoint_id, kind=endpoint_id, base_url="https://x.test", api_key="k")
    return OpenAICompatibleProvider(endpoint, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))


async def test_advance_records_rung_and_reason_in_order() -> None:
    chain = ProviderChain([(_provider("a"), "m1"), (_provider("b"), "m2"), (_provider("c"), "m3")])
    assert chain.current_model_name() == "m1"
    assert chain.attempted() == ()  # nothing ruled out yet
    assert await chain.advance(reason="rate limit") is True
    assert chain.current_model_name() == "m2"
    assert await chain.advance(reason="5xx") is True
    assert chain.current_model_name() == "m3"
    # a fallback must be explainable after the fact: (rung, reason) in order
    assert chain.attempted() == (("a:m1", "rate limit"), ("b:m2", "5xx"))
    assert await chain.advance(reason="no more rungs") is False
    assert chain.attempted() == (("a:m1", "rate limit"), ("b:m2", "5xx"))


async def test_concurrent_advances_are_serialized() -> None:
    """Two failures landing at once must each demote exactly once, in order.

    The contract makes ``advance`` async because a rung may be materialised
    (with awaits) when the run reaches it; the check-and-advance must stay
    one atomic transition even when callers race.
    """
    chain = ProviderChain([(_provider("a"), "m1"), (_provider("b"), "m2"), (_provider("c"), "m3")])
    results = await asyncio.gather(chain.advance(reason="429"), chain.advance(reason="429"))
    assert results == [True, True]
    assert chain.current_model_name() == "m3"
    assert chain.attempted() == (("a:m1", "429"), ("b:m2", "429"))


def test_attempted_shape_matches_core_contract() -> None:
    chain = ProviderChain([(_provider("a"), "m1"), (_provider("b"), "m2")])
    pairs = chain.attempted()
    assert isinstance(pairs, tuple)
    for item in pairs:
        assert isinstance(item, tuple) and len(item) == 2 and all(isinstance(x, str) for x in item)


def test_build_chain_needs_two_rungs() -> None:
    assert build_chain([(_provider("a"), "m1")]) is None
    assert build_chain([(_provider("a"), "m1"), (_provider("b"), "m2")]) is not None


def test_empty_chain_is_rejected() -> None:
    with pytest.raises(ValueError):
        ProviderChain([])


@pytest.mark.asyncio
async def test_a_rung_whose_window_cannot_hold_the_prompt_is_skipped_with_that_reason() -> None:
    big, small, other = _provider("big"), _provider("small"), _provider("other")
    room = {("big", "m"): (1_000_000, 32_000), ("small", "q"): (165_000, 24_000), ("other", "o"): (256_000, 65_536)}
    chain = ProviderChain([(big, "m"), (small, "q"), (other, "o")], room=room)
    chain.bind_prompt_size(lambda: 150_000)
    assert await chain.advance(reason="llm_stream_idle") is True
    assert chain.current_model_name() == "o"
    assert [r for _, r in chain.attempted()] == ["llm_stream_idle", "context window 165000 cannot hold the prompt (150000 tokens) and the reply (24000)"]
    # With nothing left that fits, the chain is exhausted and the loop retries where it was.
    chain.bind_prompt_size(lambda: 240_000)
    chain2 = ProviderChain([(big, "m"), (small, "q"), (other, "o")], room=room, prompt_tokens=lambda: 240_000)
    assert await chain2.advance(reason="llm_stream_idle") is False
    assert chain2.current_model_name() == "m" and chain2.attempted() == ()


async def test_a_resting_fallback_is_skipped_with_its_reason_and_the_primary_never() -> None:
    resting = {"a": "a is empty", "b": "b is empty"}
    chain = ProviderChain([(_provider("a"), "m1"), (_provider("b"), "m2"), (_provider("c"), "m3")], resting=resting)
    assert chain.current_model_name() == "m1"  # the primary is the operator's choice, whatever was observed
    assert await chain.advance(reason="overloaded") is True
    assert chain.current_model_name() == "m3"
    assert chain.attempted() == (("a:m1", "overloaded"), ("b:m2", "b is empty"))


async def test_a_chain_of_only_resting_fallbacks_does_not_advance() -> None:
    chain = ProviderChain([(_provider("a"), "m1"), (_provider("b"), "m2")], resting={"b": "b is empty"})
    assert await chain.advance(reason="429") is False
    assert chain.current_model_name() == "m1"
    assert chain.attempted() == ()
