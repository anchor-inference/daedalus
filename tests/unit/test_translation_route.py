"""A translation must not hang on one slow route: the hedge goes to another one, quickly and without
thinking, and the operator's own message waits for it no longer than its short limit.

Measured on the live stack in November 2026: OpenCode Go queued a translation for 120 s, first attempt
and hedge alike, while OpenRouter served the same model in 0.6 s. Every model here is a fake."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import AssistConfig, ModelPresetConfig, RuntimeConfig, TranslationConfig
from daedalus.host import translation
from daedalus.host.model_health import ModelHealth
from daedalus.stores.database import Database

GO = "opencode.deepseek"
ROUTER = "openrouter.deepseek"
DIRECT = "deepseek.flash"
HAIKU = "claude.haiku"
OTHER_GO = "opencode.kimi"


def config(*, presets: dict[str, tuple[str, str]] | None = None, helper: str = HAIKU, **translation_fields: Any) -> RuntimeConfig:
    table = presets or {
        GO: ("opencode", "deepseek-v4.1-flash"),
        HAIKU: ("claude", "claude-haiku-5-5"),
        ROUTER: ("openrouter", "deepseek/deepseek-v4.1-flash-1017"),
        DIRECT: ("deepseek", "deepseek-flash"),
    }
    return RuntimeConfig(
        presets={pid: ModelPresetConfig(provider=provider, model=model, thinking=False) for pid, (provider, model) in table.items()},
        assist=AssistConfig(predict_preset=helper),
        translation=TranslationConfig(operator_language="ru", agent_language="en", preset=translation_fields.pop("preset", GO), **translation_fields),
    )


def test_the_fallback_is_the_same_model_on_another_provider() -> None:
    cfg = config()
    assert translation.fallback_preset(cfg, GO) == ROUTER
    # Not ready (no key, no adapter): the next one serving the same model.
    assert translation.fallback_preset(cfg, GO, ready={"opencode", "deepseek", "claude"}) == DIRECT


async def test_a_broken_twin_is_passed_over(db: Database) -> None:
    cfg = config()
    health = ModelHealth(db)
    health.refused("openrouter", "deepseek/deepseek-v4.1-flash-1017", 404, '{"error": {"message": "model not found"}}')
    assert translation.fallback_preset(cfg, GO, health=health) == DIRECT


def test_without_a_twin_the_helper_preset_on_another_route_then_the_same_route() -> None:
    lone = {GO: ("opencode", "deepseek-v4.1-flash"), HAIKU: ("claude", "claude-haiku-5-5"), OTHER_GO: ("opencode", "kimi-k3")}
    assert translation.fallback_preset(config(presets=lone), GO) == HAIKU
    # The helper preset is on the translator's own route: no other route to ask, so the same one, as before.
    assert translation.fallback_preset(config(presets=lone, helper=OTHER_GO), GO) == GO


def test_the_operators_choice_wins() -> None:
    assert translation.fallback_preset(config(fallback_preset=HAIKU), GO) == HAIKU
    assert translation.automatic_fallback(config(fallback_preset=HAIKU), GO) == ROUTER  # what Settings names as Automatic
    assert translation.fallback_preset(config(fallback_preset="gone"), GO) == ROUTER


class Translator:
    """A manager with one fake model: ``slow`` presets never answer, the others answer at once."""

    def __init__(self, cfg: RuntimeConfig, slow: set[str], health: ModelHealth | None = None) -> None:
        self.config = cfg
        self.slow = slow
        self.calls: list[tuple[str, str]] = []
        self.model_health = health
        self.providers = SimpleNamespace(available=lambda: tuple({p.provider for p in cfg.presets.values()}))

    async def small_model(self, prompt: str, preset: str, **kwargs: Any) -> str:
        self.calls.append((preset, kwargs.get("effort", "")))
        if preset in self.slow:
            await asyncio.sleep(60)
        return f"[{preset}] {prompt}"


def _state() -> Any:
    return SimpleNamespace(metadata={}, session=SimpleNamespace(id="s1"))


async def test_a_stalled_primary_is_rescued_by_the_fallback_with_no_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(translation, "hedge_after", lambda _text: 0.05)
    manager = Translator(config(effort="high"), slow={GO})
    started = time.monotonic()
    out = await translation.Translations(manager).outgoing(_state(), "Почини это")  # type: ignore[arg-type]
    assert time.monotonic() - started < 2
    assert out.text == f"[{ROUTER}] Почини это" and out.mark is not None and out.mark["state"] == "done"
    # The operator's effort for their translator; the rescue does not think.
    assert manager.calls == [(GO, "high"), (ROUTER, "")]


async def test_the_abandoned_primary_is_remembered_as_slow(monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    monkeypatch.setattr(translation, "hedge_after", lambda _text: 0.05)
    health = ModelHealth(db)
    heard: list[tuple[str, str, float]] = []
    monkeypatch.setattr(health, "waited", lambda provider, model, ms: heard.append((provider, model, ms)))
    manager = Translator(config(), slow={GO}, health=health)
    await translation.Translations(manager).outgoing(_state(), "Почини это")  # type: ignore[arg-type]
    await asyncio.sleep(0.05)  # the loser is cancelled, not awaited: its note follows the answer
    assert [(provider, model) for provider, model, _ms in heard] == [("opencode", "deepseek-v4.1-flash")]


async def test_the_operators_message_goes_as_written_once_the_blocking_limit_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(translation, "hedge_after", lambda _text: 0.05)
    cfg = config()
    cfg.translation.blocking_timeout_seconds = 0.3  # type: ignore[assignment] — the real floor is 5 s
    manager = Translator(cfg, slow={GO, ROUTER})
    started = time.monotonic()
    out = await translation.Translations(manager).outgoing(_state(), "Почини это")  # type: ignore[arg-type]
    assert time.monotonic() - started < 2  # not the 120 s display limit
    assert out.text == "Почини это"
    assert out.mark is not None and out.mark["state"] == "failed" and "0.3 s" in out.mark["error"]


async def test_each_direction_has_its_own_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    limits: list[float] = []

    async def first_of(_first: Any, _second: Any, _hedge: float, limit: float) -> str:
        limits.append(limit)
        return "translated"

    monkeypatch.setattr(translation.Translations, "_first_of", staticmethod(first_of))
    translations = translation.Translations(Translator(config(), slow=set()))  # type: ignore[arg-type]
    await translations.outgoing(_state(), "Почини это")
    await translations.answer_words(_state(), "Да, так")
    await translations.translate("Done, see the diff.", "en", "ru", session_id="s1")  # an answer, for display
    assert limits == [30, 30, 120]
    assert TranslationConfig().blocking_timeout_seconds == 30 and TranslationConfig().timeout_seconds == 120
