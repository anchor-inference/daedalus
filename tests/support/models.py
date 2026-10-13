"""The model table a test runs with.

A shipped installation has none: which model to run is the operator's first decision (see
``RuntimeConfig.presets``). Tests that actually run something say here which model they run,
so that "no model configured" stays a state a test can ask for rather than the accident every
test has to work around. ``tests/unit/test_no_model.py`` is the one that asks for it.
"""

from __future__ import annotations

from daedalus.config import CompanionConfig, ModelConfig, ModelPresetConfig, RuntimeConfig, VisionConfig

DEFAULT_PRESET = "deepseek.deepseek-v4-flash"
FALLBACK_PRESET = "openrouter.deepseek-v4-flash"
VISION_PRESET = "openrouter.qwen-qwen3.7-flash"


def presets() -> dict[str, ModelPresetConfig]:
    """A default, a fallback behind it, and a fast image-capable one for the vision and companion voice slots."""
    return {
        DEFAULT_PRESET: ModelPresetConfig(provider="deepseek", model="deepseek-flash"),
        FALLBACK_PRESET: ModelPresetConfig(provider="openrouter", model="deepseek/deepseek-v4-flash", images=True),
        VISION_PRESET: ModelPresetConfig(
            provider="openrouter", model="qwen/qwen3.7-flash", label="Qwen 3.7 Flash (vision)", thinking=False, images=True, max_output_tokens=4_000
        ),
    }


def model_config(**overrides: object) -> RuntimeConfig:
    """A :class:`RuntimeConfig` with a model in it; keyword arguments override any other section."""
    return RuntimeConfig(
        model=ModelConfig(preset=DEFAULT_PRESET, chain=[FALLBACK_PRESET], auto_fallback=True),
        presets=presets(),
        vision=VisionConfig(preset=VISION_PRESET),
        companion=CompanionConfig(preset=VISION_PRESET),
        **overrides,  # type: ignore[arg-type]
    )
