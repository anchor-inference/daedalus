"""A refusal that one setting answers carries the way to that setting, and an auxiliary model that is
not set or refuses falls back to the session's own model unless the operator said otherwise.

A manual /compact once answered "Server unavailable (500)" because the summary model's provider was
past its monthly limit; the operator had to guess that the summary model was the story, and then find
its row. These tests hold the backend to naming the row (``{page, key}``) on the HTTP error, on the
session event and on the notification, and to the ``model.fallback_to_session`` switch: on, the
session's model does the work and the operator is warned; off, the refusal is an error with the row.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from protocore.contracts.llm import LLMRateLimitError, LLMResponse
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.config import ModelPresetConfig, RuntimeConfig, Settings
from daedalus.extensions import commands as slash
from daedalus.extensions.api import build_app
from daedalus.extensions.notifications import setting_draft
from daedalus.host.session_runner import CompactionFailed, HostEventType
from daedalus.host.setting_refs import (
    COMPACTION_MODEL,
    VISION_MODEL,
    SettingProblem,
    SettingRef,
    setting_from_link,
    setting_of,
)
from daedalus.providers.registry import ProviderUnavailable
from daedalus.stores.database import Database
from daedalus.tools import vision
from tests.unit.test_components import HEAD, FakeApp
from tests.unit.test_session_runner import SECTIONED, ScriptedProvider, _manager, _wait_finished


def test_a_reference_survives_being_re_raised_and_rides_a_link_both_ways() -> None:
    try:
        try:
            raise SettingProblem("the summary model is over its limit", COMPACTION_MODEL)
        except SettingProblem as exc:
            raise RuntimeError("compaction failed") from exc
    except RuntimeError as outer:
        assert setting_of(outer) == COMPACTION_MODEL
    assert setting_of(ValueError("nothing named")) is None
    link = COMPACTION_MODEL.link()
    assert link == "/app/settings/limits?setting=compaction.preset"
    assert setting_from_link(link) == COMPACTION_MODEL
    assert setting_from_link("/app/agents/abc") is None
    missing = ProviderUnavailable("opencode", ["local"])
    assert setting_of(missing) == SettingRef("models", "providers.opencode.api_key")
    assert "'opencode' is not configured" in str(missing) and isinstance(missing, KeyError)


def test_the_notification_is_a_warning_when_the_session_model_stood_in_and_an_error_when_nothing_did() -> None:
    payload = {"kind": "compaction", "outcome": "fallback", "detail": "cheap/flash: usage limit exceeded", "model": "scripted/scripted-model", "setting": COMPACTION_MODEL.as_dict()}
    warning = setting_draft(payload, "en")
    assert warning.tone == "warning" and warning.link == COMPACTION_MODEL.link()
    assert warning.session_id is None  # the router would count it seen in the session on screen and raise no toast
    assert "usage limit exceeded" in warning.body and "scripted-model" in warning.body
    error = setting_draft({**payload, "outcome": "failed"}, "ru")
    assert error.tone == "error" and error.title.startswith("Не удалось") and error.dedupe_key != warning.dedupe_key


class _Cheap:
    """A summary model whose provider is past its monthly limit."""

    endpoint = SimpleNamespace(id="cheap")

    def __init__(self) -> None:
        self.asked = 0

    async def complete_text(self, request: Any) -> LLMResponse:
        self.asked += 1
        raise LLMRateLimitError("cheap: rate limited: Go usage limit exceeded")


async def _session_with_history(settings: Settings, db: Database) -> tuple[Any, Any, ScriptedProvider, list[dict[str, Any]], list[Any]]:
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("quota")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello there")
    await waiter
    asked: list[Any] = []

    async def answers(request: Any) -> LLMResponse:
        asked.append(request)
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=SECTIONED)]), stop_reason=StopReason.end_turn)

    provider.complete_text = answers  # type: ignore[method-assign]
    provider.summaries = asked  # type: ignore[attr-defined]
    posted: list[dict[str, Any]] = []

    async def post(session_id: str | None, payload: dict[str, Any]) -> None:
        posted.append({"session_id": session_id, **payload})

    manager.service_hooks["setting_notice"] = post
    events: list[Any] = []

    async def sink(session_id: str, event: Any) -> None:
        if getattr(event, "type", None) == HostEventType.SETTING_NOTICE:
            events.append(event)

    manager.add_sink(sink)
    return manager, state, provider, posted, events


async def test_with_the_fallback_on_a_refusing_summary_model_is_replaced_and_the_operator_warned(settings: Settings, db: Database) -> None:
    manager, state, _provider, posted, events = await _session_with_history(settings, db)
    cheap = _Cheap()
    manager.config.compaction.preset = "cheap"
    manager._compaction_preset_rung = lambda: ((cheap, "flash"), "")  # type: ignore[method-assign]
    assert manager.config.model.fallback_to_session is True  # on unless the operator turns it off
    summary = await manager.compact(state.session.id)
    assert summary.startswith("## Goal") and cheap.asked == 1
    assert [(p["kind"], p["outcome"], p["setting"]) for p in posted] == [("compaction", "fallback", COMPACTION_MODEL.as_dict())]
    assert "usage limit exceeded" in posted[0]["detail"] and posted[0]["model"] == "scripted/scripted-model"
    # The conversation hears it too, drawn inline with the same row.
    assert len(events) == 1 and events[0].payload["setting"] == COMPACTION_MODEL.as_dict()
    await manager.close()


async def test_with_the_fallback_off_a_refusing_summary_model_is_an_error_naming_its_row(settings: Settings, db: Database) -> None:
    manager, state, provider, posted, _events = await _session_with_history(settings, db)
    cheap = _Cheap()
    manager.config.compaction.preset = "cheap"
    manager.config.model.fallback_to_session = False
    manager._compaction_preset_rung = lambda: ((cheap, "flash"), "")  # type: ignore[method-assign]
    with pytest.raises(CompactionFailed, match="usage limit exceeded") as failure:
        await manager.compact(state.session.id)
    assert failure.value.setting == COMPACTION_MODEL and cheap.asked == 1
    assert [(p["outcome"], p["setting"]["key"]) for p in posted] == [("failed", "compaction.preset")]
    assert provider.summaries == []  # type: ignore[attr-defined] — the session's model was not asked
    await manager.close()


async def test_with_the_fallback_off_a_deleted_summary_model_refuses_before_any_call(settings: Settings, db: Database) -> None:
    manager, state, provider, posted, _events = await _session_with_history(settings, db)
    manager.config.compaction.preset = "gone"
    manager.config.model.fallback_to_session = False
    calls: list[Any] = []
    original = provider.complete_text

    async def counted(request: Any) -> LLMResponse:
        calls.append(request)
        return await original(request)

    provider.complete_text = counted  # type: ignore[method-assign]
    with pytest.raises(CompactionFailed, match="no longer among the models") as failure:
        await manager.compact(state.session.id)
    assert failure.value.setting == COMPACTION_MODEL and calls == []
    assert posted and posted[0]["outcome"] == "failed"
    # Turned back on, the same deleted preset is skipped for the session's model, with a warning.
    manager.config.model.fallback_to_session = True
    posted.clear()
    assert (await manager.compact(state.session.id)).startswith("## Goal")
    assert [p["outcome"] for p in posted] == ["fallback"] and "no longer among the models" in posted[0]["detail"]
    await manager.close()


def test_a_command_that_fails_on_a_setting_answers_409_with_the_row_and_a_provider_refusal_is_never_a_bare_500(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FakeApp(tmp_path, native=True)

    async def compaction_refused(_app: Any, _session_id: str, _line: str) -> str:
        raise CompactionFailed("no model could summarise the history: cheap/flash: Go usage limit exceeded")

    monkeypatch.setattr(slash, "run_command", compaction_refused)
    with TestClient(build_app(app, "tok"), raise_server_exceptions=False) as client:
        answer = client.post("/api/sessions/s1/command", headers=HEAD, json={"line": "/compact"})
        assert answer.status_code == 409
        assert answer.json() == {"detail": "no model could summarise the history: cheap/flash: Go usage limit exceeded", "setting": {"page": "limits", "key": "compaction.preset"}}

        async def quota(_app: Any, _session_id: str, _line: str) -> str:
            raise LLMRateLimitError("rate limited: insufficient balance")

        monkeypatch.setattr(slash, "run_command", quota)
        answer = client.post("/api/sessions/s1/command", headers=HEAD, json={"line": "/compact"})
        assert answer.status_code == 429 and "insufficient balance" in answer.json()["detail"]

        async def no_key(_app: Any, _session_id: str, _line: str) -> str:
            raise ProviderUnavailable("opencode", [])

        monkeypatch.setattr(slash, "run_command", no_key)
        answer = client.post("/api/sessions/s1/command", headers=HEAD, json={"line": "/compact"})
        assert answer.status_code == 409 and answer.json()["setting"] == {"page": "models", "key": "providers.opencode.api_key"}


def test_the_fallback_switch_is_a_setting_on_by_default(tmp_path) -> None:
    assert RuntimeConfig().model.fallback_to_session is True
    assert RuntimeConfig.model_validate({"model": {"preset": ""}}).model.fallback_to_session is True  # a file written before it
    app = FakeApp(tmp_path, native=True)
    with TestClient(build_app(app, "tok")) as client:
        view = client.get("/api/settings", headers=HEAD).json()
        assert view["model"]["fallback_to_session"] is True
        saved = client.put("/api/settings", headers=HEAD, json={"base_revision": view["revision"], "model": {"fallback_to_session": False}})
        assert saved.status_code == 200 and saved.json()["model"]["fallback_to_session"] is False
        assert app.config.model.fallback_to_session is False


class _Blobs:
    async def put(self, tenant: str, data: bytes, *, content_type: str) -> Any:
        return SimpleNamespace(ref="blob-1")


class _Eyes:
    """A provider: ``images`` says whether its model takes pictures, ``answer`` what it sees."""

    def __init__(self, images: bool, answer: str = "a red button labelled Deploy") -> None:
        self.images = images
        self.answer = answer
        self.asked = 0

    def accepts_images(self, model: str) -> bool:
        return self.images

    async def complete_text(self, request: Any) -> LLMResponse:
        self.asked += 1
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=self.answer)]), stop_reason=StopReason.end_turn)


class _Manager:
    """What ``look`` reads of the session manager: the configuration, the session's model, the notice."""

    def __init__(self, session_model: _Eyes, *, fallback: bool) -> None:
        self.config = RuntimeConfig()
        self.config.model.fallback_to_session = fallback
        self.session_model = session_model
        self.notices: list[dict[str, Any]] = []

    async def session_vision(self, session_id: str | None) -> tuple[Any, str]:
        if not self.session_model.accepts_images("main"):
            return None, "the session's model main does not take images"
        return (self.session_model, "main", _Blobs(), "daedalus"), ""

    async def setting_notice(self, session_id: str | None, **notice: Any) -> None:
        self.notices.append({"session_id": session_id, **notice})


async def test_with_no_vision_model_set_the_session_model_looks_when_it_takes_images() -> None:
    session_model = _Eyes(images=True)
    manager = _Manager(session_model, fallback=True)
    text, model = await vision.look(None, manager, b"\x89PNG", "image/png", "what is on the button?", session_id="s1")
    assert (text, model) == ("a red button labelled Deploy", "main") and session_model.asked == 1
    assert [(n["kind"], n["outcome"], n["setting"], n["session_id"]) for n in manager.notices] == [("vision", "fallback", VISION_MODEL, "s1")]
    assert "no vision model is configured" in manager.notices[0]["detail"]


async def test_with_no_vision_model_set_a_session_model_that_cannot_see_is_a_clear_error_with_the_row() -> None:
    manager = _Manager(_Eyes(images=False), fallback=True)
    with pytest.raises(vision.VisionUnavailable, match="does not take images") as failure:
        await vision.look(None, manager, b"\x89PNG", "image/png", "read it", session_id="s1")
    assert failure.value.setting == VISION_MODEL
    assert [n["outcome"] for n in manager.notices] == ["failed"]


async def test_with_the_fallback_off_no_vision_model_is_an_error_and_the_session_model_is_not_asked() -> None:
    session_model = _Eyes(images=True)
    manager = _Manager(session_model, fallback=False)
    with pytest.raises(vision.VisionUnavailable, match="no vision model is configured") as failure:
        await vision.look(None, manager, b"\x89PNG", "image/png", "read it", session_id="s1")
    assert setting_of(failure.value) == VISION_MODEL and session_model.asked == 0
    assert [(n["outcome"], n["setting"]) for n in manager.notices] == [("failed", VISION_MODEL)]


async def test_a_failing_vision_model_falls_back_and_a_working_one_raises_no_notice() -> None:
    class Down(_Eyes):
        async def complete_text(self, request: Any) -> LLMResponse:
            raise LLMRateLimitError("vision: insufficient balance")

    manager = _Manager(_Eyes(images=True), fallback=True)
    text, model = await vision.look((Down(images=True), "small-vision", _Blobs(), "daedalus"), manager, b"\x89PNG", "image/png", "read it")
    assert model == "main" and "insufficient balance" in manager.notices[0]["detail"]
    manager.notices.clear()
    text, model = await vision.look((_Eyes(images=True, answer="fine"), "small-vision", _Blobs(), "daedalus"), manager, b"\x89PNG", "image/png", "read it")
    assert (text, model) == ("fine", "small-vision") and manager.notices == []


async def test_the_session_model_is_the_vision_fallback_only_when_it_takes_images(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("eyes")
    route, why = await manager.session_vision(state.session.id)
    assert route is None and "does not take images" in why
    provider.accepts_images = lambda model: True  # type: ignore[attr-defined]
    route, why = await manager.session_vision(state.session.id)
    assert route is not None and route[0] is provider and route[1] == "scripted-model" and why == ""
    await manager.close()


def test_a_preset_marked_for_images_is_what_the_vision_row_names() -> None:
    # The row the reference points at edits ``vision.preset``; a reference that drifted from the
    # configuration's own path would open a page with nothing to highlight.
    config = RuntimeConfig()
    config.presets["eyes"] = ModelPresetConfig(provider="local", model="eyes", images=True)
    config.vision.preset = "eyes"
    assert VISION_MODEL.key == "vision.preset" and config.vision_preset() == ("eyes", config.presets["eyes"])
    assert COMPACTION_MODEL.key == "compaction.preset" and hasattr(config.compaction, "preset")
