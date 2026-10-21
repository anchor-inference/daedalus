"""The main orchestrator: its office, its own tools, what wakes it, what it sees, and Answer."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.tool_registry import policy_admits
from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import DISPATCHER_TOOLS, ModelPresetConfig, RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.dispatcher import Dispatcher, NotCurrent
from daedalus.extensions.dispatches import Dispatches
from daedalus.extensions.notifications import NotificationService
from daedalus.host import prompts
from daedalus.host.events import AppEvent
from daedalus.stores.database import Database
from tests.support.models import FALLBACK_PRESET
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import Rig, _idle, events, events_messages, rig
from tests.unit.test_orchestrator_team import fake, office, working


@dataclass
class Main:
    r: Rig
    dispatches: Dispatches
    main: Dispatcher

    async def call(self, session_id: str, operation: str, /, **kwargs: Any) -> str:
        return await self.main.service(operation, session_id=session_id, **kwargs)


async def main_rig(settings: Settings, db: Database, tmp_path: Path, script: list[dict[str, Any]] | None = None) -> Main:
    r = await rig(settings, db, tmp_path, script)
    app = r.team.app
    dispatches = Dispatches(app)
    app.extensions["dispatches"] = dispatches
    r.manager.asks.default_dispatch = dispatches.default_dispatch
    r.orch.state_sections.append(dispatches.state_section)
    main = Dispatcher(app)
    app.extensions["dispatcher"] = main
    main.attach()
    return Main(r, dispatches, main)


def text_of(message: Any) -> str:
    return "".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))


# -- the office and its tools ---------------------------------------------------------------------------


async def test_the_main_orchestrator_sees_its_own_tools_and_nobody_else_sees_them(settings: Settings, db: Database, tmp_path: Path) -> None:
    m = await main_rig(settings, db, tmp_path, [{"text": "Hello."}])
    try:
        sid = await m.main.ensure()
        state = await m.r.manager.get_state(sid)
        assert state is not None and m.r.manager.is_dispatcher(state)
        own = {t.name for t in m.r.manager.dispatcher_tools.list_all()}
        assert own == set(DISPATCHER_TOOLS) - {"CreateProject"} | ({"CreateProject"} & own)
        assert "CreateProject" in own and "AskUser" not in own and "Exec" not in own
        policy = m.r.manager.tool_policy_for(state)
        assert {name for name in own if policy_admits(policy, name)} == own and not policy.pinned

        await m.r.manager.submit(sid, "What is going on?")
        await until_await(lambda: _idle(m.r.manager, sid), "the main orchestrator answered")
        request = m.r.provider.requests[0]
        advertised = sorted(t.name for t in request.tools or [])
        assert advertised == sorted(own)
        delegate = next(t for t in request.tools or [] if t.name == "Delegate")
        assert "enable_orchestrator" in str(delegate.parameters), "its Delegate, not the voice concierge's"
        system = "".join(text_of(msg) for msg in request.messages if msg.role is MessageRole.system)
        assert "You are the main orchestrator" in system and "You are Daedalus" not in system
        assert "Open dispatches: none" in text_of([msg for msg in request.messages if msg.role is MessageRole.user][-1])

        ordinary = await m.r.manager.create_session("work", project_id=m.r.project.id)
        ordinary_policy = m.r.manager.tool_policy_for(ordinary)
        ordinary_tools = {t.name for t in m.r.manager.tools.list_all() if policy_admits(ordinary_policy, t.name)}
        assert "Progress" not in ordinary_tools and "Cancel" not in ordinary_tools and "CreateProject" not in ordinary_tools
        with pytest.raises(NotCurrent, match="not the main orchestrator"):
            await m.call(ordinary.session.id, "projects")
        bounds = m.r.manager.run_bounds(state)
        assert bounds is not None and bounds.max_iterations == m.r.manager.config.dispatcher.max_iterations
        assert bounds.setting == "dispatcher.usd_per_run"
        assert m.r.manager.run_bounds(ordinary) is None, "an operator's chat has no bounds of its own"
    finally:
        await m.r.manager.close()


async def test_there_is_one_main_orchestrator_and_a_replaced_one_is_refused(settings: Settings, db: Database, tmp_path: Path) -> None:
    m = await main_rig(settings, db, tmp_path)
    try:
        first, second = await asyncio.gather(m.main.ensure(), m.main.ensure())
        assert first == second
        rows = await m.r.manager.db.fetchall("SELECT id FROM sessions WHERE json_extract(metadata, '$.dispatcher') = 1")
        assert [r["id"] for r in rows] == [first]
        successor = await m.main.replace("a fresh start")
        assert successor != first
        with pytest.raises(NotCurrent, match=f"replaced by {successor}"):
            await m.call(first, "projects")
        old = await m.r.manager.get_state(first)
        assert old is not None and old.metadata.get("dispatcher_retired") and old.metadata.get("successor") == successor
        new = await m.r.manager.get_state(successor)
        assert new is not None
        notes = await m.main.turn_notes(new)
        assert notes is not None and f"You replace the main orchestrator session {first}: a fresh start" in notes
        again = await m.main.turn_notes(new)
        assert again is not None and "You replace" not in again, "the predecessor is named once"
        retired_notes = await m.main.turn_notes(old)
        assert retired_notes is not None and "replaced by" in retired_notes
    finally:
        await m.r.manager.close()


# -- routing --------------------------------------------------------------------------------------------


async def test_delegate_refuses_a_project_whose_orchestrator_is_off_unless_asked_to_switch_it_on(settings: Settings, db: Database, tmp_path: Path) -> None:
    m = await main_rig(settings, db, tmp_path)
    try:
        sid = await m.main.ensure()
        with pytest.raises(ValueError, match="orchestrator of Bakery is off"):
            await m.call(sid, "delegate", project="bakery", text="Add a gluten-free menu")
        assert await m.r.manager.dispatches.open_for() == []
        said = await m.call(sid, "delegate", project="Bakery", text="Add a gluten-free menu", title="Gluten-free", enable_orchestrator=True)
        [dispatch] = await m.r.manager.dispatches.open_for()
        assert f"dispatch {dispatch.id} (#1) handed to Bakery" in said and dispatch.from_session == sid and dispatch.title == "Gluten-free"
        project = await m.r.refreshed()
        assert project.settings.orchestrator.enabled
        followed = await m.call(sid, "delegate", project="", text="Also mark the vegan dishes", dispatch_id=dispatch.id)
        assert f"added to dispatch {dispatch.id}" in followed
        with pytest.raises(ValueError, match="no project called"):
            await m.call(sid, "delegate", project="Garden", text="x")
        progress = await m.call(sid, "progress", dispatch_id=dispatch.id)
        assert "Bakery #1 — open" in progress and "Also mark the vegan dishes" in progress
        listing = await m.call(sid, "projects")
        assert "Bakery [" in listing and "1 open dispatches" in listing and "orchestrator on" in listing
        detail = await m.call(sid, "projects", project="Bakery")
        assert "Open dispatches: [" in detail
    finally:
        await m.r.manager.close()


async def test_what_wakes_the_main_orchestrator_and_what_never_does(settings: Settings, db: Database, tmp_path: Path) -> None:
    m = await main_rig(settings, db, tmp_path)
    try:
        await office(m.r)
        project = await m.r.refreshed()
        dispatch = await m.dispatches.create(project, text="Photos")

        def ev(event_type: str, **payload: Any) -> AppEvent:
            return AppEvent(seq=7, at="2026-09-25T10:00:00+00:00", type=event_type, payload={"dispatch_id": dispatch.id, **payload}, project_id=project.id, session_id=None, staff_id=None, terminal_id=None)

        closed = await m.main.classify(ev("dispatch.closed", status="done", result="ok", by="orchestrator"))
        assert closed is not None and closed.urgent
        stalled = await m.main.classify(ev("dispatch.stalled", minutes=31, actor="system"))
        assert stalled is not None and stalled.urgent
        progress = await m.main.classify(ev("dispatch.message", author="orchestrator", kind="progress", text="half"))
        assert progress is not None and not progress.urgent
        assert await m.main.classify(ev("dispatch.message", author="dispatcher", kind="message", text="more", actor="dispatcher")) is None
        assert await m.main.classify(ev("dispatch.closed", status="cancelled", result="", by="dispatcher", actor="dispatcher")) is None
        assert await m.main.classify(ev("dispatch.closed", status="cancelled", result="", by="operator", actor="operator")) is None
        assert await m.main.classify(ev("dispatch.created", seq=1, title="", text="x", kind="work")) is None
        line = await m.main.line(ev("dispatch.closed", status="done", result="The photos are tidy", by="orchestrator"))
        assert f"Bakery closed dispatch {dispatch.id} as done: The photos are tidy" in line
    finally:
        await m.r.manager.close()


async def test_a_report_wakes_the_main_orchestrator_with_one_message_and_its_answer_is_news(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The acceptance of the ping contract: the main orchestrator hands work over, the project's orchestrator
    closes it with a report, and the main orchestrator is woken once with the report — nothing else."""
    script = [
        {"tool": "Delegate", "args": {"project": "Bakery", "text": "Add a gluten-free section to the menu", "title": "Gluten-free"}},
        {"text": "Handed to Bakery."},
    ]
    m = await main_rig(settings, db, tmp_path, script)
    try:
        m.r.manager.config.dispatcher.batch_seconds = 1
        orchestrator = await office(m.r)
        # The project's orchestrator is woken too; it has nothing scripted and answers "nothing left".
        sid = await m.main.ensure()
        await m.r.manager.submit(sid, "In Bakery, add a gluten-free section to the menu")
        await until_await(lambda: _idle(m.r.manager, sid), "the main orchestrator delegated")
        [dispatch] = await m.r.manager.dispatches.open_for()
        await until_await(lambda: _idle(m.r.manager, orchestrator), "the project's orchestrator took the dispatch")
        m.r.provider.script.append({"text": "Bakery finished the gluten-free section."})
        await m.r.call(orchestrator, "project_report", text="The section is live with 6 dishes", kind="done", dispatch_id=dispatch.id)

        async def told() -> bool:
            return bool(await events_messages(m.r.manager, sid)) and await _idle(m.r.manager, sid)

        await until_await(told, "the report woke the main orchestrator")
        [batch] = await events_messages(m.r.manager, sid)
        assert batch.startswith("[reports · 1 since ") and f"Bakery closed dispatch {dispatch.id}" in batch and "6 dishes" in batch

        async def announced() -> list[AppEvent]:
            return [e for e in await events(m.r.manager, "run.finished", session_id=sid) if e.payload.get("origin") == "events"]

        await until_await(lambda: _truthy(announced()), "the run's end was announced")
        finished = await announced()
        assert finished and finished[-1].payload["operator_facing"] and finished[-1].payload.get("news") and finished[-1].payload.get("link") == "/app/main"
        assert len(await events_messages(m.r.manager, orchestrator)) == 1, "the report did not wake the project's orchestrator again"
    finally:
        await m.r.manager.close()


async def _truthy(value: Any) -> bool:
    return bool(await value)


# -- the questions in its chat ---------------------------------------------------------------------------


async def test_a_linked_question_shows_in_the_main_chat_and_answer_needs_the_operators_words(settings: Settings, db: Database, tmp_path: Path) -> None:
    m = await main_rig(settings, db, tmp_path)
    try:
        orchestrator = await office(m.r)
        sid = await m.main.ensure()
        dispatch = await m.dispatches.create(await m.r.refreshed(), text="Choose a database", from_session=sid)
        await m.r.call(orchestrator, "ask_operator", title="Postgres or SQLite?", text="Postgres or SQLite?", options=["Postgres", "SQLite"], dispatch_id=dispatch.id)
        await m.r.call(orchestrator, "ask_operator", title="Unrelated: new logo?", text="Unrelated: new logo?")
        view = await m.main.view()
        [card] = view["asks"]
        assert card["text"].startswith("Postgres or SQLite?") and card["project_name"] == "Bakery" and card["asker"] == "orchestrator" and view["questions"] == 1
        state = await m.r.manager.get_state(sid)
        assert state is not None
        notes = await m.main.state_text(state)
        assert f"[{card['short_id']}] Bakery (question): Postgres or SQLite?" in notes

        await _operator_says(m, sid, "tell bakery: Postgres, please")
        with pytest.raises(ValueError, match="operator's own words"):
            await m.call(sid, "answer", ask_id=card["short_id"], quote="SQLite")
        [unlinked] = [a for a in await m.r.manager.asks.open_for(m.r.project.id) if a.text.startswith("Unrelated")]
        with pytest.raises(ValueError, match="not shown in this chat"):
            await m.call(sid, "answer", ask_id=unlinked.short_id, quote="Postgres")
        said = await m.call(sid, "answer", ask_id=card["short_id"], quote="Postgres")
        assert "answered" in said
        answered = await m.r.manager.asks.get(card["id"])
        assert answered is not None and not answered.open and answered.resolved_by == "operator"
        assert answered.resolution["via"] == "dispatcher" and answered.resolution["quote_message_id"] > 0 and answered.resolution["text"] == "Postgres"
        with pytest.raises(ValueError, match="already answered"):
            await m.call(sid, "answer", ask_id=card["id"], quote="Postgres")
        collapsed = [a for a in (await m.main.view())["asks"] if a["id"] == card["id"]]
        assert collapsed and collapsed[0]["resolved_at"], "an answered card stays as its one line"
    finally:
        await m.r.manager.close()


async def _operator_says(m: Main, session_id: str, text: str) -> None:
    """The operator's message in the main chat, as the transcript keeps it, without starting a run."""
    await m.r.manager.sessions.append_transcript(session_id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator"})])


async def test_answer_never_passes_on_a_permission(settings: Settings, db: Database, tmp_path: Path) -> None:
    m = await main_rig(settings, db, tmp_path)
    try:
        fake(m.r)
        await office(m.r, autonomy="ask")
        sid = await m.main.ensure()
        dispatch = await m.dispatches.create(await m.r.refreshed(), text="Install the dependencies")
        _member, live = await working(m.r)
        ask_id = await m.r.team.ingress.permission(live, "req-9", "Exec", "npm install")
        await m.r.manager.asks.link(ask_id, dispatch.id)
        await _operator_says(m, sid, "yes, allow the npm install")
        ask = await m.r.manager.asks.get(ask_id)
        assert ask is not None and await m.main.is_mirrored(ask)
        with pytest.raises(ValueError, match="only a question is answered in words"):
            await m.call(sid, "answer", ask_id=ask.short_id, quote="allow the npm install")
    finally:
        await m.r.manager.close()


# -- the model ---------------------------------------------------------------------------------------------


def test_the_main_orchestrator_runs_a_mid_tier_preset_unless_one_is_chosen() -> None:
    def preset(effort: str, window: int) -> ModelPresetConfig:
        return ModelPresetConfig(provider="p", model=f"m-{effort}-{window}", thinking=True, reasoning_effort=effort, context_window=window)

    config = RuntimeConfig(presets={"small": preset("low", 64_000), "mid": preset("medium", 128_000), "big": preset("high", 200_000)})
    assert config.strongest_preset() == "big"
    assert config.middle_preset() == "mid" and config.dispatcher_preset() == "mid"
    config.dispatcher.preset = "small"
    assert config.dispatcher_preset() == "small"
    two = RuntimeConfig(presets={"mid": preset("medium", 128_000), "big": preset("high", 200_000)})
    assert two.middle_preset() == "mid", "with two, the weaker one"
    assert RuntimeConfig(presets={"only": preset("low", 8000)}).middle_preset() == "only"
    assert RuntimeConfig().middle_preset() is None


async def test_the_prompt_names_no_project_and_the_model_is_the_settings_choice(settings: Settings, db: Database, tmp_path: Path) -> None:
    assert "Bakery" not in prompts.DISPATCHER and "Answer" in prompts.DISPATCHER
    m = await main_rig(settings, db, tmp_path)
    try:
        sid = await m.main.ensure()
        state = await m.r.manager.get_state(sid)
        assert state is not None
        chosen = m.main.preset_for(state)
        assert chosen == m.r.manager.config.dispatcher_preset() and chosen in m.r.manager.config.presets
        ordinary = await m.r.manager.create_session("work", project_id=m.r.project.id)
        assert m.main.preset_for(ordinary) is None
    finally:
        await m.r.manager.close()


# -- the routes ----------------------------------------------------------------------------------------------


async def test_the_main_routes_the_cancel_card_the_setup_button_and_the_model_chip(settings: Settings, db: Database, tmp_path: Path) -> None:
    m = await main_rig(settings, db, tmp_path)
    try:
        orchestrator = await office(m.r)
        app = SimpleNamespace(settings=settings, config=m.r.manager.config, db=db, manager=m.r.manager, front=None, extensions=m.r.team.app.extensions, guard=None, notifications=None)

        async def save_config(config: Any, expected_revision: str | None = None) -> None:
            app.config = m.r.manager.config = config

        app.save_config = save_config
        m.r.team.app.save_config = save_config
        m.r.team.app.config = m.r.manager.config
        api = build_app(app, "tok")  # type: ignore[arg-type]
        headers = {"X-Daedalus-Token": "tok"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            empty = await client.get("/api/main", headers=headers)
            assert empty.status_code == 200 and empty.json()["session_id"] == "" and empty.json()["dispatches"] == []
            opened = await client.post("/api/main", headers=headers)
            sid = opened.json()["session_id"]
            assert sid and (await client.post("/api/main", headers=headers)).json()["session_id"] == sid

            await m.r.manager.projects.set_setup(m.r.project.id, "dispatcher")
            dispatch = await m.dispatches.create(await m.r.refreshed(), text="Survey", kind="setup", from_session=sid)
            await m.r.call(orchestrator, "ask_operator", title="Deadline?", text="Deadline?", options=["Friday", "Monday"])
            view = (await client.get("/api/main", headers=headers)).json()
            assert [d["id"] for d in view["dispatches"]] == [dispatch.id] and view["dispatches"][0]["project_name"] == "Bakery"
            assert view["questions"] == 1 and view["setup"] == [{"project_id": m.r.project.id, "name": "Bakery"}]
            [card] = view["asks"]
            answered = await client.post(f"/api/asks/{card['id']}/answer", json={"selected": ["Friday"], "window": "main"}, headers=headers)
            assert answered.status_code == 200, answered.text
            assert answered.json()["ask"]["resolution"]["via"] == "main"
            late = await client.post(f"/api/asks/{card['id']}/answer", json={"selected": ["Monday"], "window": "project"}, headers=headers)
            assert late.status_code == 409 and "already answered" in late.text

            detail = await client.get(f"/api/dispatches/{dispatch.id}", headers=headers)
            assert detail.status_code == 200 and detail.json()["asks"][0]["id"] == card["id"]
            finished = await client.post(f"/api/projects/{m.r.project.id}/setup/finish", headers=headers)
            assert finished.json() == {"finished": True} and (await m.r.refreshed()).setup_by == ""
            cancelled = await client.post(f"/api/dispatches/{dispatch.id}/cancel", json={"reason": "not needed"}, headers=headers)
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
            assert (await client.post(f"/api/dispatches/{dispatch.id}/cancel", json={}, headers=headers)).status_code == 409
            assert (await client.get("/api/dispatches/dnope1", headers=headers)).status_code == 404

            settings_view = (await client.get("/api/settings", headers=headers)).json()
            assert settings_view["dispatcher"]["middle"] == m.r.manager.config.middle_preset()
            chip = await client.post(f"/api/sessions/{sid}/model", json={"preset": FALLBACK_PRESET}, headers=headers)
            assert chip.status_code == 200, chip.text
            assert m.r.manager.config.dispatcher.preset == FALLBACK_PRESET, "the chip on the main chat writes the Settings choice"
            replaced = await client.post("/api/main/replace", json={"reason": "fresh"}, headers=headers)
            assert replaced.json()["session_id"] not in ("", sid)
    finally:
        await m.r.manager.close()


async def test_a_mirrored_requests_notification_opens_the_main_chat(settings: Settings, db: Database, tmp_path: Path) -> None:
    m = await main_rig(settings, db, tmp_path)
    try:
        orchestrator = await office(m.r)
        service = NotificationService(db)
        service.register_link(m.main.request_link)
        dispatch = await m.dispatches.create(await m.r.refreshed(), text="Choose")
        await m.r.call(orchestrator, "ask_operator", title="Linked?", text="Linked?", dispatch_id=dispatch.id)
        await m.r.call(orchestrator, "ask_operator", title="Not linked?", text="Not linked?")
        linked, unlinked = sorted(await m.r.manager.asks.open_for(m.r.project.id), key=lambda a: a.text)
        assert await service.link_for(str(linked.detail["event_ref"]), "/app/agents/x") == "/app/main"
        assert await service.link_for(str(unlinked.detail["event_ref"]), "/app/agents/x") == "/app/agents/x"
    finally:
        await m.r.manager.close()
