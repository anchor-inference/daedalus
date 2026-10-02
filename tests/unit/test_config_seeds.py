"""A seed is applied to a config once; what the operator removes afterwards stays removed.

A seed exists to give a config written before a feature existed the entries that feature needs. A
file created now is not one of those — it starts with no models at all, by design — so the seeds
count as applied to it from the start.
"""

from __future__ import annotations

from pathlib import Path

import tomli_w

from daedalus.config import RuntimeConfig


def test_a_fresh_config_is_created_with_no_models_and_every_seed_marked_applied(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = RuntimeConfig.load(path)
    assert config.presets == {} and config.model.preset == "" and config.has_model is False
    assert "claude-subscription" in config.seeded
    assert "more-provider-endpoints" in config.seeded
    assert {"zai_coding", "minimax_plan", "kimi_coding"} <= set(config.providers)
    # And a restart does not quietly grow a model table behind the operator's back.
    assert RuntimeConfig.load(path).presets == {}


def test_the_claude_seed_is_applied_once_and_a_removed_preset_stays_removed(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    # A config written before the seed existed: presets of its own, no record of any seed.
    with path.open("wb") as fh:
        tomli_w.dump({"presets": {"vllm.m": {"provider": "vllm", "model": "m"}}, "model": {"preset": "vllm.m", "chain": []}}, fh)
    config = RuntimeConfig.load(path)
    assert "claude.opus-5" in config.presets  # the seed brought the subscription models with it
    assert config.model.preset == "vllm.m"  # and left the operator's own default alone
    # The operator removes every Claude preset and the file is saved without them.
    for pid in [p for p in config.presets if p.startswith("claude.")]:
        del config.presets[pid]
    config.save(path)
    reloaded = RuntimeConfig.load(path)
    assert not [p for p in reloaded.presets if p.startswith("claude.")]
    assert "claude-subscription" in reloaded.seeded
    # And a restart later still has them gone.
    assert not [p for p in RuntimeConfig.load(path).presets if p.startswith("claude.")]


def test_an_existing_config_keeps_its_models_when_the_defaults_stop_shipping_any(tmp_path: Path) -> None:
    """An upgrade must not disturb an installation that already chose its models."""
    path = tmp_path / "config.toml"
    with path.open("wb") as fh:
        tomli_w.dump(
            {
                "seeded": ["claude-subscription"],
                "model": {"preset": "openrouter.a", "chain": ["deepseek.b"]},
                "presets": {
                    "openrouter.a": {"provider": "openrouter", "model": "vendor/a", "images": True, "context_window": 200_000},
                    "deepseek.b": {"provider": "deepseek", "model": "b"},
                },
                "vision": {"preset": "openrouter.a"},
            },
            fh,
        )
    config = RuntimeConfig.load(path)
    assert "more-provider-endpoints" in config.seeded
    assert {"zai_coding", "minimax_plan", "kimi_coding"} <= set(config.providers)
    assert config.has_model is True
    assert config.model.preset == "openrouter.a" and config.model.chain == ["deepseek.b"]
    assert config.preset() == ("openrouter.a", config.presets["openrouter.a"])
    assert config.vision_preset()[0] == "openrouter.a"  # type: ignore[index]


def test_a_removed_provider_is_not_restored_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = RuntimeConfig.load(path)
    del config.providers["kimi_coding"]
    config.save(path)
    assert "kimi_coding" not in RuntimeConfig.load(path).providers


def test_a_config_from_before_presets_existed_still_migrates_to_one(tmp_path: Path) -> None:
    """The oldest shape names a provider and a model name; it becomes a preset, not nothing."""
    path = tmp_path / "config.toml"
    with path.open("wb") as fh:
        tomli_w.dump(
            {
                "seeded": ["claude-subscription"],
                "model": {"provider": "vllm", "name": "Qwen3.6", "thinking": False, "context_window": 64_000, "chain": ["vllm"]},
                "providers": {"vllm": {"kind": "vllm", "base_url": "http://x/v1", "default_model": "Qwen3.6", "supports_images": True}},
            },
            fh,
        )
    config = RuntimeConfig.load(path)
    assert config.has_model is True
    pid, preset = config.preset()
    assert (preset.provider, preset.model) == ("vllm", "Qwen3.6")
    assert preset.thinking is False and preset.context_window == 64_000
    assert config.model.preset == pid
