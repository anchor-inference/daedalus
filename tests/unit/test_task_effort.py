"""Every task that names a model in the settings also names how hard that model thinks.

A one-shot helper (a translation, a review, a guess, a page read) does not think unless its effort
says so, and says "off" in the words the key proxy's Claude routes understand; a task that runs a whole
agent run (a heartbeat, the orchestrators, the companion, the subagents) keeps its preset's own thinking
unless its effort says otherwise. These tests follow the chosen effort from the setting to the request
each task sends, against fakes: no test here touches the network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import LLMObservabilityContext, LLMRequest, LLMResponse
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import browser as browser_extension
from daedalus.extensions import dispatcher as dispatcher_extension
from daedalus.extensions import heartbeat as heartbeat_extension
from daedalus.extensions import subagents as subagents_extension
from daedalus.extensions.api import build_app
from daedalus.extensions.companion import Companion
from daedalus.host import action_rules, task_effort
from daedalus.host import translation as translation_module
from daedalus.host.session_runner import SessionManager
from daedalus.memory.synthesis import ModelChoice, Synthesizer
from daedalus.stores.database import Database
from daedalus.stores.projects import OrchestratorSettings
from daedalus.tools import vision
from tests.support.models import DEFAULT_PRESET, VISION_PRESET, model_config

REAL_CLIENT = httpx.AsyncClient
OFF = {"enable_thinking": False, "thinking_off": True}


class Recorder:
    """A provider that answers every call with ``answer`` and keeps the requests it was sent."""

    def __init__(self, answer: str = "ok") -> None:
        self.answer = answer
        self.requests: list[LLMRequest] = []
        self.endpoint = SimpleNamespace(id="deepseek")

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=self.answer)]), stop_reason=StopReason.end_turn)

    def accepts_images(self, _model: str) -> bool:
        return True

    @property
    def thinking(self) -> dict[str, Any]:
        """The thinking part of the last request's ``extra``."""
        extra = self.requests[-1].extra or {}
        return {key: extra[key] for key in ("enable_thinking", "thinking_off", "reasoning_effort") if key in extra}


def _routed(manager: SessionManager, provider: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager.providers, "rungs_for", lambda *_args, **_kw: [(provider, "fake-model")])


@pytest.fixture
async def manager(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    yield manager
    await manager.close()


# -- the one place ------------------------------------------------------------------------------------


def test_a_helper_does_not_think_unless_told_and_then_has_room_to() -> None:
    assert task_effort.helper_extra("") == OFF
    assert task_effort.helper_extra("none") == OFF
    assert task_effort.helper_extra("stale-word") == OFF, "an unknown word is the safe default, not an error mid-call"
    assert task_effort.helper_extra("high") == {"enable_thinking": True, "reasoning_effort": "high"}
    assert task_effort.helper_tokens(160, "") == 160
    assert task_effort.helper_tokens(160, "low") == 160 + task_effort.THINKING_ROOM["low"]
    assert task_effort.helper_tokens(2500, "xhigh", 8000) == 8000, "the room stops at the model's own cap"


def test_a_run_keeps_its_preset_unless_told_and_none_is_the_session_choosers_off() -> None:
    assert task_effort.run_overrides("") == {}
    assert task_effort.run_overrides("none") == {"thinking_enabled": False}
    assert task_effort.run_overrides("medium") == {"thinking_enabled": True, "reasoning_effort": "medium"}
    assert task_effort.set_model_arguments("xhigh") == {"thinking": True, "reasoning_effort": "xhigh"}
    with pytest.raises(ValueError, match="effort must be empty or one of"):
        task_effort.check("max-ish")


def test_the_settings_refuse_an_effort_no_task_knows() -> None:
    with pytest.raises(ValueError):
        model_config().model_validate({**model_config().model_dump(), "translation": {"effort": "turbo"}})
    config = RuntimeConfig.model_validate({**model_config().model_dump(), "translation": {"effort": "high"}, "heartbeat": {"effort": "none"}})
    assert (config.translation.effort, config.heartbeat.effort) == ("high", "none")


# -- the helpers ----------------------------------------------------------------------------------------


async def test_small_model_sends_the_tasks_effort_and_off_by_default(manager: SessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Recorder()
    _routed(manager, provider, monkeypatch)
    await manager.small_model("hi", DEFAULT_PRESET, purpose="t", max_tokens=100)
    assert provider.thinking == OFF and provider.requests[-1].max_tokens == 100
    await manager.small_model("hi", DEFAULT_PRESET, purpose="t", max_tokens=100, effort="low")
    assert provider.thinking == {"enable_thinking": True, "reasoning_effort": "low"}
    assert provider.requests[-1].max_tokens == 100 + task_effort.THINKING_ROOM["low"]


async def test_the_translator_sends_its_effort(manager: SessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Recorder("Hallo Welt")
    _routed(manager, provider, monkeypatch)
    translations = translation_module.Translations(manager)
    await translations.translate("Hello world", "en", "de", session_id="s")
    assert provider.thinking == OFF
    manager.config.translation.effort = "medium"
    await translations.translate("Hello there world", "en", "de", session_id="s")
    assert provider.thinking == {"enable_thinking": True, "reasoning_effort": "medium"}


async def test_the_next_message_guess_passes_its_effort(manager: SessionManager) -> None:
    """The autopilot judge's is held in ``test_autopilot.py``, where its fixture lives."""
    from daedalus.host.predict import Predictor

    efforts: list[str] = []

    async def small_model(prompt: str, preset: str, **kwargs: Any) -> str:
        efforts.append(kwargs["effort"])
        return "a guess"

    manager.small_model = small_model  # type: ignore[method-assign]
    manager.config.assist.predict_effort = "low"
    await Predictor(manager)._call("p", DEFAULT_PRESET, "s", "", 10, 5)
    assert efforts == ["low"]


async def test_the_action_review_and_the_injection_check_send_their_efforts(manager: SessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Recorder("ALLOW the operator asked for it")
    _routed(manager, provider, monkeypatch)
    gate = manager.action_rules
    await gate._review(None, action_rules.Ruling("push", "auto", True), action_rules.ActionSummary("git push"))
    assert provider.thinking == OFF and provider.requests[-1].max_tokens == 160
    manager.config.action_rules.review_effort = "low"
    await gate._review(None, action_rules.Ruling("push", "auto", True), action_rules.ActionSummary("git push"))
    assert provider.thinking == {"enable_thinking": True, "reasoning_effort": "low"}
    assert provider.requests[-1].max_tokens == 160 + task_effort.THINKING_ROOM["low"], "a thinking review has room to answer"

    manager.config.action_rules.injection_model = True
    manager.config.action_rules.injection_effort = "medium"
    provider.answer = "SAFE"
    assert await gate.injection_check("https://example.org", "a page") == []
    assert provider.thinking == {"enable_thinking": True, "reasoning_effort": "medium"}


async def test_the_memory_classifier_and_the_memory_pass_send_their_efforts(manager: SessionManager, monkeypatch: pytest.MonkeyPatch, db: Database) -> None:
    provider = Recorder("preference")
    _routed(manager, provider, monkeypatch)
    manager.config.memory.classify_preset = DEFAULT_PRESET
    ask = manager.memory_classifier()
    assert ask is not None
    await ask("classify this")
    assert provider.thinking == OFF
    manager.config.memory.classify_effort = "high"
    ask = manager.memory_classifier()
    assert ask is not None
    await ask("classify this")
    assert provider.thinking == {"enable_thinking": True, "reasoning_effort": "high"}

    manager.config.memory.synthesis_effort = "xhigh"
    reader = Synthesizer(db, SimpleNamespace(), lambda: manager.config, manager.providers)  # type: ignore[arg-type]
    await reader.ask(ModelChoice(DEFAULT_PRESET), [Message(role=MessageRole.user, content_blocks=[TextBlock(text="x")])], "run")
    assert provider.thinking == {"enable_thinking": True, "reasoning_effort": "xhigh"}
    assert provider.requests[-1].extra["json_object"] is True, "the effort does not cost the call its JSON answer"


async def test_the_summariser_sends_its_effort(manager: SessionManager) -> None:
    sent: list[LLMRequest] = []

    async def summary_request(_provider: Any, request: LLMRequest) -> str:
        sent.append(request)
        return "a note"

    manager._summary_request = summary_request  # type: ignore[method-assign]
    observability = LLMObservabilityContext(call_purpose="compaction")
    await manager._summary_call(Recorder(), "m", "summarise", "body", observability, strict=False, max_tokens=1000)
    assert {k: sent[-1].extra[k] for k in OFF} == OFF
    manager.config.compaction.effort = "medium"
    await manager._summary_call(Recorder(), "m", "summarise", "body", observability, strict=False, max_tokens=1000)
    assert sent[-1].extra["reasoning_effort"] == "medium" and sent[-1].extra["enable_thinking"] is True


async def test_the_browsers_page_reads_send_their_efforts(manager: SessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = Recorder("SAFE")
    _routed(manager, provider, monkeypatch)
    app = SimpleNamespace(manager=manager, config=manager.config)
    await browser_extension.page_model(app, "page", preset="", effort=manager.config.browser.injection_monitor_effort, purpose="m", max_tokens=120, timeout=5)  # type: ignore[arg-type]
    assert provider.thinking == OFF
    await browser_extension.page_model(app, "page", preset="", effort="low", purpose="x", max_tokens=2000, timeout=5)  # type: ignore[arg-type]
    assert provider.thinking == {"enable_thinking": True, "reasoning_effort": "low"}


async def test_the_vision_model_thinks_as_the_settings_or_the_session_say(manager: SessionManager) -> None:
    state = await manager.create_session("pictures")
    assert manager.vision_model(state.session.id)[4] == ""
    manager.config.vision.effort = "low"
    assert manager.vision_model(state.session.id)[4] == "low", "a session with no choice of its own follows the settings"
    await manager.set_vision_effort(state.session.id, "high")
    assert manager.vision_model(state.session.id)[4] == "high"
    assert manager.vision_model(None)[4] == "low"
    assert manager.vision_view(state.session.id)["vision_effort"] == "high"
    with pytest.raises(ValueError):
        await manager.set_vision_effort(state.session.id, "loud")

    provider = Recorder("a chart")
    route = (provider, "eyes", manager.blobs, "default", "high")
    await vision.look(route, manager, b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png", "what is it")
    assert provider.thinking == {"enable_thinking": True, "reasoning_effort": "high"}
    # The session's own model standing in for a missing vision model carries no effort and does not think.
    await vision.look((provider, "eyes", manager.blobs, "default"), manager, b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png", "what is it")
    assert provider.thinking == OFF


async def test_a_sessions_vision_effort_survives_a_restart(settings: Settings, db: Database) -> None:
    first = SessionManager(settings, model_config(), db=db)
    await first.start()
    state = await first.create_session("pictures")
    await first.set_vision_effort(state.session.id, "medium")
    await first.close()
    second = SessionManager(settings, model_config(), db=db)
    await second.start()
    try:
        assert second.vision_effort_for(state.session.id) == "medium"
    finally:
        await second.close()


async def test_the_session_vision_endpoint_takes_an_effort_alone(manager: SessionManager, settings: Settings, db: Database) -> None:
    async def save_config(config: Any, *, expected_revision: str | None = None) -> None:
        manager.reload_config(config)
        app.config = config

    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None, extensions={"api_token": "tok"}, guard=None,
                          create_session=manager.create_session, save_config=save_config)
    state = await manager.create_session("pictures")
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test", headers={"X-Daedalus-Token": "tok"}) as http:
        answer = await http.post(f"/api/sessions/{state.session.id}/vision", json={"effort": "low"})
        assert answer.status_code == 200 and answer.json()["vision_effort"] == "low"
        assert manager.vision_preset_for(state.session.id)[0] == VISION_PRESET, "the effort alone leaves the model be"
        assert (await http.post(f"/api/sessions/{state.session.id}/vision", json={"effort": "loud"})).status_code == 400
        saved = await http.put("/api/settings", json={"base_revision": (await http.get("/api/settings")).json()["revision"], "translation": {"effort": "high"}})
        assert saved.status_code == 200 and saved.json()["translation"]["effort"] == "high"
        refused = await http.put("/api/settings", json={"base_revision": saved.json()["revision"], "translation": {"effort": "turbo"}})
        assert refused.status_code == 400


# -- the runs -------------------------------------------------------------------------------------------


async def test_an_effort_hook_decides_what_the_run_thinks_with_and_the_chip_says_the_same(manager: SessionManager) -> None:
    state = await manager.create_session("main")
    state.metadata["heartbeat"] = True
    app = SimpleNamespace(config=manager.config)
    manager.effort_hooks.append(lambda s: heartbeat_extension.effort_for(app, s))
    engine = await manager._build_engine(state, "run-1")
    preset = manager.config.presets[DEFAULT_PRESET]
    assert (engine.config.thinking_enabled, engine.config.reasoning_effort) == (preset.thinking, preset.reasoning_effort), "empty keeps the preset's own"

    manager.config.heartbeat.effort = "xhigh"
    engine = await manager._build_engine(state, "run-2")
    assert (engine.config.thinking_enabled, engine.config.reasoning_effort) == (True, "xhigh")
    manager.config.heartbeat.effort = "none"
    engine = await manager._build_engine(state, "run-3")
    assert engine.config.thinking_enabled is False
    other = await manager.create_session("not a heartbeat")
    assert manager.task_overrides(other, {}) == {}, "the hook speaks for its own sessions only"


def test_the_orchestrators_and_the_companion_name_their_efforts() -> None:
    config = model_config()
    dispatcher = dispatcher_extension.Dispatcher.__new__(dispatcher_extension.Dispatcher)
    dispatcher.manager = SimpleNamespace(config=config)  # type: ignore[assignment]
    config.dispatcher.effort = "low"
    assert dispatcher.effort_for(SimpleNamespace(metadata={"dispatcher": True})) == "low"
    assert dispatcher.effort_for(SimpleNamespace(metadata={})) is None

    from daedalus.extensions.orchestrator import Orchestrators

    orchestrators = Orchestrators.__new__(Orchestrators)
    orchestrators.manager = SimpleNamespace(config=config)  # type: ignore[assignment]
    orchestrators._models = {"p1": "", "p2": ""}
    orchestrators._efforts = {"p1": "high", "p2": ""}
    config.orchestrator.effort = "medium"
    assert orchestrators.effort_for(SimpleNamespace(metadata={"orchestrator_of": "p1"})) == "high", "the project's own wins"
    assert orchestrators.effort_for(SimpleNamespace(metadata={"orchestrator_of": "p2"})) == "medium", "else the Settings default"
    assert orchestrators.effort_for(SimpleNamespace(metadata={})) is None
    assert OrchestratorSettings.load({"effort": "low"}).effort == "low"
    assert OrchestratorSettings.load({"effort": "loud"}).effort == "", "a word no task knows reads as the default"

    companion = Companion.__new__(Companion)
    companion.app = SimpleNamespace(config=config)  # type: ignore[assignment]
    companion._efforts = {}
    companion._calls, companion._listeners = 0, set()
    config.companion.effort, config.companion.background_effort = "none", "high"
    assert companion._effort_for("voice") == "none" and companion._effort_for("text") == "high"
    talking = SimpleNamespace(metadata={"companion": True}, session=SimpleNamespace(id="c"))
    assert companion.effort_for(talking) == "high", "a turn not yet pointed at a model is a background one"
    companion._efforts["c"] = "none"
    assert companion.effort_for(talking) == "none"


async def test_a_subagent_and_a_quick_agent_take_the_settings_effort_unless_the_leader_names_one() -> None:
    assert subagents_extension.settings_effort("") is None
    assert subagents_extension.settings_effort("none") == "off"
    assert subagents_extension.settings_effort("low") == "low"
    calls: list[dict[str, Any]] = []

    async def set_model(_session_id: str, **kwargs: Any) -> None:
        calls.append(kwargs)

    manager = SimpleNamespace(set_model=set_model)
    await subagents_extension.set_effort(manager, "c", subagents_extension.settings_effort("none") or "")
    await subagents_extension.set_effort(manager, "c", subagents_extension.settings_effort("high") or "")
    assert calls == [{"thinking": False}, {"thinking": True, "reasoning_effort": "high"}]
