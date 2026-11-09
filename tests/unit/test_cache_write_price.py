"""A cache write is priced apart where the vendor bills it apart: the Anthropic key, at the hour's rate."""

from __future__ import annotations

import pytest

from daedalus.config import ProviderConfig, RuntimeConfig, Settings, keyproxy_base
from daedalus.providers.openai_compat import normalize_usage
from daedalus.providers.pricing import ModelPricing
from daedalus.providers.registry import ProviderRegistry


def test_a_write_with_its_own_price_is_taken_out_of_the_fresh_input() -> None:
    usage = normalize_usage({"prompt_tokens": 100_000, "completion_tokens": 1_000, "prompt_tokens_details": {"cached_tokens": 90_000},
                             "cache_read_input_tokens": 90_000, "cache_creation_input_tokens": 8_000})
    priced = ModelPricing(input=4.0, output=20.0, cache_hit=0.2, cache_write=8.0)
    # 2k fresh at $4, 90k read at $0.20, 8k written at $8, 1k out at $20.
    assert priced.cost(usage) == pytest.approx((2_000 * 4 + 90_000 * 0.2 + 8_000 * 8 + 1_000 * 20) / 1e6)
    # Without a write price the write stays inside the fresh input, as every other vendor's cache is billed.
    plain = ModelPricing(input=4.0, output=20.0, cache_hit=0.2)
    assert plain.cost(usage) == pytest.approx((10_000 * 4 + 90_000 * 0.2 + 1_000 * 20) / 1e6)


def test_a_write_count_larger_than_the_uncached_prompt_is_held_to_it() -> None:
    priced = ModelPricing(input=1.0, output=0.0, cache_hit=0.1, cache_write=2.0)
    assert priced.cost({"input_tokens": 100, "cache_read_tokens": 90, "cache_creation_input_tokens": 50}) == pytest.approx((90 * 0.1 + 10 * 2.0) / 1e6)


def test_the_anthropic_key_prices_writes_at_twice_the_input_unless_told_otherwise() -> None:
    # The operator's typed prices carry input, output and the read only; a write booked at the input price
    # showed half of what the hour-long cache costs.
    config = RuntimeConfig(providers={
        "anthropic": ProviderConfig(base_url=keyproxy_base() + "/anthropic", pricing={
            "claude-opus-5-5": {"input": 4, "output": 20, "cache_hit": 0.2},
            "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_hit": 0.01, "cache_write": 0.125},
        }),
        "openai": ProviderConfig(base_url=keyproxy_base() + "/openai", pricing={"gpt-x": {"input": 1, "output": 2, "cache_hit": 0.1}}),
    })
    registry = ProviderRegistry(Settings(_env_file=None), config)  # type: ignore[call-arg]
    anthropic = registry.get("anthropic").endpoint
    assert anthropic.pricing_for("claude-opus-5-5").cache_write == 8.0
    assert anthropic.pricing_for("claude-haiku-5-5").cache_write == 0.125
    assert registry.get("openai").endpoint.pricing_for("gpt-x").cache_write is None
