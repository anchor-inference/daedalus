"""A seed is applied to a config once; what the operator removes afterwards stays removed.

A seed exists to give a config written before a feature existed the entries that feature needs. A
file created now is not one of those — it starts with no models at all, by design — so the seeds
count as applied to it from the start.
"""

from __future__ import annotations

from pathlib import Path

import pytest
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
                "seeded": ["claude-subscription", "openai-anthropic-keys"],
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


def _write(path: Path, data: dict) -> None:
    with path.open("wb") as fh:
        tomli_w.dump(data, fh)


@pytest.fixture(autouse=True)
def _no_setup_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAEDALUS_LOCAL_MODEL", raising=False)
    monkeypatch.delenv("DAEDALUS_FREE_PROVIDER", raising=False)
    monkeypatch.delenv("DAEDALUS_FREE_MODEL", raising=False)
    monkeypatch.delenv("DAEDALUS_VOICE", raising=False)


def test_a_fresh_config_has_the_openai_and_anthropic_providers(tmp_path: Path) -> None:
    config = RuntimeConfig.load(tmp_path / "config.toml")
    assert config.providers["openai"].base_url.endswith("/openai") and config.providers["openai"].kind == "openai_compat"
    assert config.providers["anthropic"].base_url.endswith("/anthropic") and config.providers["anthropic"].kind == "openai_compat"
    assert "openai-anthropic-keys" in config.seeded


def test_an_old_config_gets_the_key_providers_once_and_a_removal_sticks(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _write(path, {"seeded": ["claude-subscription"], "providers": {"deepseek": {"kind": "deepseek", "base_url": "http://kp/deepseek"}}})
    config = RuntimeConfig.load(path)
    assert {"openai", "anthropic"} <= set(config.providers) and "deepseek" in config.providers
    del config.providers["anthropic"]
    config.save(path)
    reloaded = RuntimeConfig.load(path)
    assert "anthropic" not in reloaded.providers and "openai" in reloaded.providers


def test_a_local_model_becomes_a_provider_a_preset_and_the_default_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_LOCAL_MODEL", "qwen3:8b")
    path = tmp_path / "config.toml"
    config = RuntimeConfig.load(path)  # a fresh file takes the wizard's answer too
    local = config.providers["local"]
    assert local.kind == "openai_compat" and local.name == "Local endpoint" and local.base_url.endswith("/local") and local.timeout_seconds == 600
    preset = config.presets["local.qwen3-8b"]
    assert (preset.provider, preset.model, preset.label, preset.thinking) == ("local", "qwen3:8b", "qwen3:8b (local)", False)
    assert config.model.preset == "local.qwen3-8b"
    assert "local-model:qwen3:8b" in config.seeded
    # Deleted by the operator: a second load with the same answer in the environment leaves it deleted.
    del config.presets["local.qwen3-8b"]
    config.model.preset = ""
    config.save(path)
    again = RuntimeConfig.load(path)
    assert "local.qwen3-8b" not in again.presets and again.model.preset == ""


def test_a_local_model_keeps_an_existing_default_and_an_existing_local_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_LOCAL_MODEL", "m1")
    path = tmp_path / "config.toml"
    _write(path, {"seeded": ["claude-subscription", "openai-anthropic-keys"], "model": {"preset": "vllm.x", "chain": []}, "presets": {"vllm.x": {"provider": "vllm", "model": "x"}}, "providers": {"local": {"kind": "openai_compat", "base_url": "http://mine/v1", "timeout_seconds": 30.0}}})
    config = RuntimeConfig.load(path)
    assert config.model.preset == "vllm.x"
    assert "local.m1" in config.presets
    assert config.providers["local"].base_url == "http://mine/v1" and config.providers["local"].timeout_seconds == 30.0
    # Idempotent: the file on disk is already what a second load would produce.
    assert RuntimeConfig.load(path).model_dump() == config.model_dump()
    monkeypatch.setenv("DAEDALUS_LOCAL_MODEL", "m2")
    third = RuntimeConfig.load(path)
    assert {"local.m1", "local.m2"} <= set(third.presets) and third.model.preset == "vllm.x"
    assert {"local-model:m1", "local-model:m2"} <= set(third.seeded)


def test_no_local_model_adds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_LOCAL_MODEL", "")
    config = RuntimeConfig.load(tmp_path / "config.toml")
    assert "local" not in config.providers and config.presets == {}


def test_free_setup_seeds_a_single_free_only_preset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_FREE_PROVIDER", "kilo")
    monkeypatch.setenv("DAEDALUS_FREE_MODEL", "stepfun/step-3.7-flash:free")
    path = tmp_path / "config.toml"
    config = RuntimeConfig.load(path)
    pid = "kilo.stepfun-step-3.7-flash-free"
    assert config.presets[pid].free_only
    assert config.model.preset == pid
    assert config.providers["kilo"].base_url.endswith("/kilo")
    del config.presets[pid]
    config.model.preset = ""
    config.save(path)
    assert pid not in RuntimeConfig.load(path).presets


def test_free_setup_keeps_a_long_model_id_with_a_short_unique_preset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.config import free_preset_id_for

    first = "publisher/a-very-long-free-model-name-with-many-variants-and-a-context-window:free"
    second = first.replace("window", "memory")
    assert len(free_preset_id_for("openrouter", first)) == 64
    assert free_preset_id_for("openrouter", first) != free_preset_id_for("openrouter", second)
    monkeypatch.setenv("DAEDALUS_FREE_PROVIDER", "openrouter")
    monkeypatch.setenv("DAEDALUS_FREE_MODEL", first)
    config = RuntimeConfig.load(tmp_path / "config.toml")
    assert config.presets[free_preset_id_for("openrouter", first)].model == first
    assert free_preset_id_for("openrouter", "a/модель/b") == "openrouter.a-b"


def test_cloud_voice_points_an_unset_asr_at_the_openai_provider_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_VOICE", "cloud")
    path = tmp_path / "config.toml"
    config = RuntimeConfig.load(path)
    assert config.asr.provider == "openai" and "voice-cloud" in config.seeded
    config.asr.provider = ""
    config.save(path)
    assert RuntimeConfig.load(path).asr.provider == ""


def test_cloud_voice_leaves_a_configured_asr_alone_and_local_or_off_change_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAEDALUS_VOICE", "cloud")
    path = tmp_path / "config.toml"
    _write(path, {"seeded": ["claude-subscription", "openai-anthropic-keys"], "asr": {"url": "http://127.0.0.1:9000/v1"}})
    config = RuntimeConfig.load(path)
    assert config.asr.provider == "" and config.asr.url == "http://127.0.0.1:9000/v1"
    for answer in ("local", "off"):
        monkeypatch.setenv("DAEDALUS_VOICE", answer)
        other = tmp_path / f"{answer}.toml"
        assert RuntimeConfig.load(other).asr.provider == ""
