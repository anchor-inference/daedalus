"""A model known to read pictures is saved as one, and the presets saved without it are repaired once.

The subscription routes answer ``/models`` with ids alone, so Add a model had nothing to prefill the
Images switch from: Claude Opus, Sonnet and Fable, every Codex GPT and Grok were saved with it off,
and the vision fallback refused to let the session's Claude Opus look at a screenshot.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import tomli_w

from daedalus.config import SEEDS, RuntimeConfig, Settings
from daedalus.extensions.api import build_app, extend_model_list, model_entry
from daedalus.host.session_runner import SessionManager
from daedalus.model_capabilities import known_image_input
from daedalus.stores.database import Database
from tests.support.models import model_config

H = {"X-Daedalus-Token": "tok"}


@pytest.mark.parametrize("model", [
    "claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-haiku-4-5-20251001",
    "anthropic/claude-sonnet-4.6", "gpt-6-astra", "gpt-6.1-sol", "gpt-5.6-terra", "gpt-4o-mini", "grok-4.7", "grok-2-vision-1212",
])
def test_the_families_known_to_read_pictures(model: str) -> None:
    assert known_image_input(model) is True


@pytest.mark.parametrize("model", ["gpt-oss-120b", "gpt-3.5-turbo", "grok-3", "grok-code-fast-1", "deepseek-flash", "Qwen3.6", "space-bunny-free", ""])
def test_anything_else_is_unknown_rather_than_no(model: str) -> None:
    assert known_image_input(model) is None


def test_a_bare_id_from_a_subscription_route_is_prefilled_from_its_family() -> None:
    assert model_entry({"id": "claude-opus-5-5"}) == {"id": "claude-opus-5-5", "images": True}
    assert model_entry({"id": "deepseek-flash"}) == {"id": "deepseek-flash"}
    # What the endpoint itself says wins over the family.
    said = model_entry({"id": "claude-opus-5-5", "architecture": {"input_modalities": ["text"]}})
    assert said["images"] is False
    extended = extend_model_list({"models": [], "entries": []}, ["gpt-6.1-sol", "mystery"])
    assert extended["entries"] == [{"id": "gpt-6.1-sol", "images": True}, {"id": "mystery"}]


def _old_config(path: Path) -> None:
    """A config written before the repair: every seed before it applied, Images off on everything."""
    presets = {
        "claude.claude-opus-5-5": {"provider": "claude", "model": "claude-opus-5-5", "images": False},
        "claude.claude-haiku-5-5": {"provider": "claude", "model": "claude-haiku-5-5", "images": True},
        "codex.gpt-6-luna": {"provider": "codex", "model": "gpt-6-luna", "images": False},
        "grok.grok-4.7": {"provider": "grok", "model": "grok-4.7", "images": False},
        "vllm.Qwen3.6": {"provider": "vllm", "model": "Qwen3.6", "images": False},
        "opencode.space-bunny-free": {"provider": "opencode", "model": "space-bunny-free", "images": False},
    }
    seeded = [s for s in SEEDS if s != "known-image-models"]
    with path.open("wb") as fh:
        tomli_w.dump({"presets": presets, "model": {"preset": "claude.claude-opus-5-5", "chain": []}, "seeded": seeded}, fh)


def test_the_repair_turns_images_on_for_known_families_once_and_a_switch_turned_off_stays_off(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    _old_config(path)
    config = RuntimeConfig.load(path)
    images = {pid: p.images for pid, p in config.presets.items()}
    assert images == {
        "claude.claude-opus-5-5": True, "claude.claude-haiku-5-5": True, "codex.gpt-6-luna": True, "grok.grok-4.7": True,
        "vllm.Qwen3.6": False, "opencode.space-bunny-free": False,
    }
    assert "known-image-models" in config.seeded
    # The operator turns one off again; the next start leaves it off and changes nothing else.
    config.presets["grok.grok-4.7"].images = False
    config.save(path)
    before = path.read_bytes()
    again = RuntimeConfig.load(path)
    assert again.presets["grok.grok-4.7"].images is False and path.read_bytes() == before


def test_a_fresh_config_counts_the_repair_as_applied(tmp_path: Path) -> None:
    assert "known-image-models" in RuntimeConfig.load(tmp_path / "config.toml").seeded


async def test_a_new_preset_takes_its_images_switch_from_the_family_unless_the_caller_says(settings: Settings, db: Database) -> None:
    made = SessionManager(settings, model_config(), db=db)
    await made.start()
    app = SimpleNamespace(settings=settings, config=made.config, db=db, manager=made, front=None, extensions={}, guard=None, create_session=made.create_session)

    async def save_config(config: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        made.reload_config(config)
        app.config = config

    app.save_config = save_config
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            await client.put("/api/presets/or.opus", json={"provider": "openrouter", "model": "anthropic/claude-opus-5-5"}, headers=H)
            assert app.config.presets["or.opus"].images is True
            await client.put("/api/presets/or.opus-blind", json={"provider": "openrouter", "model": "anthropic/claude-opus-5-5", "images": False}, headers=H)
            assert app.config.presets["or.opus-blind"].images is False
            await client.put("/api/presets/or.other", json={"provider": "openrouter", "model": "some/text-model"}, headers=H)
            assert app.config.presets["or.other"].images is False
            # Editing an existing preset never reopens the question.
            await client.put("/api/presets/or.opus-blind", json={"label": "Blind Opus"}, headers=H)
            assert app.config.presets["or.opus-blind"].images is False
    finally:
        await made.close()
