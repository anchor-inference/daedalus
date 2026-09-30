"""Dispatches: the main orchestrator's hand-overs, the reports that close them, and the questions shown under them."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.extensions.dispatches import Dispatches
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.host.events import AppEvent
from daedalus.stores.database import Database
from daedalus.stores.dispatches import DispatchError
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import Rig, _idle, events, events_messages, rig
from tests.unit.test_orchestrator_team import fake, office, working


def install(r: Rig) -> Dispatches:
    dispatches = Dispatches(r.team.app)
    r.team.app.extensions["dispatches"] = dispatches
    r.manager.asks.default_dispatch = dispatches.default_dispatch
    r.orch.state_sections.append(dispatches.state_section)
    return dispatches


def event(r: Rig, kind: str, payload: dict[str, object], **ids: str) -> AppEvent:
    return AppEvent(seq=1, at="2026-09-25T10:00:00+00:00", type=kind, payload=payload, project_id=ids.get("project_id", r.project.id), session_id=ids.get("session_id"), staff_id=None, terminal_id=None)


# -- handing over and reporting ---------------------------------------------------------------------------


async def test_a_dispatch_wakes_the_project_orchestrator_at_once_and_its_state_lists_it(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "On it."}])
    try:
        dispatches = install(r)
        sid = await office(r)
        dispatch = await dispatches.create(await r.refreshed(), text="Add a gluten-free section to the menu", title="Gluten-free menu", from_session="main")
        assert dispatch.id.startswith("d") and len(dispatch.id) == 6 and dispatch.seq == 1 and dispatch.open

        async def woken() -> bool:
            return bool(await events_messages(r.manager, sid)) and await _idle(r.manager, sid)

        await until_await(woken, "the dispatch woke the orchestrator")
        [batch] = await events_messages(r.manager, sid)
        assert f"[from the main orchestrator] dispatch {dispatch.id} #1: Gluten-free menu: Add a gluten-free section" in batch
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert f"[{dispatch.id}] #1 open" in state and "exactly one ProjectReport(dispatch_id" in state
        second = await dispatches.create(await r.refreshed(), text="And a vegan one")
        assert second.seq == 2
    finally:
        await r.manager.close()


async def test_a_long_dispatch_reaches_the_orchestrator_whole_and_can_be_read_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    """A dispatch was cut at 600 characters in the wake-up and in the journal, and nothing gave the rest."""
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        sid = await office(r)
        text = "\n".join(f"{n}. The section needs item {n} checked against the supplier's list, with the price." for n in range(1, 16))
        assert len(text) > 1000
        dispatch = await dispatches.create(await r.refreshed(), text=text, title="Menu audit", from_session="main")
        [created] = await events(r.manager, "dispatch.created")
        line = await r.orch.line(await r.refreshed(), created)
        assert "15. The section needs item 15 checked" in line and "…" not in line

        [entry] = [e for e in await r.manager.projects.journal(r.project.id) if e.kind == "dispatch"]
        assert f"{len(text)} characters; Peek(op='dispatch', ref='{dispatch.id}') reads it whole" in entry.text and "1. The section" not in entry.text
        await r.call(sid, "project_report", text="Ten items checked", kind="progress", dispatch_id=dispatch.id)
        said = await r.call(sid, "peek", op="dispatch", ref=dispatch.id)
        assert said.startswith(f"dispatch {dispatch.id} (#1), open, from ") and text in said and "orchestrator (progress): Ten items checked" in said
        with pytest.raises(Refused, match="has no dispatch 'dzzzzz'"):
            await r.call(sid, "peek", op="dispatch", ref="dzzzzz")
    finally:
        await r.manager.close()


async def test_a_progress_report_is_a_message_and_a_done_report_closes_the_dispatch_once(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        sid = await office(r)
        dispatch = await dispatches.create(await r.refreshed(), text="Add a gluten-free section")
        said = await r.call(sid, "project_report", text="Recipes collected", kind="progress", dispatch_id=dispatch.id)
        assert f"added to dispatch {dispatch.id}" in said
        current = await r.manager.dispatches.get(dispatch.id)
        assert current is not None and current.open
        [message] = await r.manager.dispatches.messages(dispatch.id)
        assert (message.author, message.kind, message.text) == ("orchestrator", "progress", "Recipes collected")
        [progress] = await events(r.manager, "dispatch.message")
        assert await r.orch.classify(r.project.id, progress) is None, "its own progress report never wakes the orchestrator"

        said = await r.call(sid, "project_report", text="The section is live", title="Gluten-free menu", kind="done", dispatch_id=dispatch.id)
        assert f"dispatch {dispatch.id} is done" in said
        closed = await r.manager.dispatches.get(dispatch.id)
        assert closed is not None and closed.status == "done" and closed.closed_at and "The section is live" in closed.result
        [event_closed] = await events(r.manager, "dispatch.closed")
        assert event_closed.payload["status"] == "done" and event_closed.payload["by"] == "orchestrator" and event_closed.project_id == r.project.id
        with pytest.raises(Refused, match="already done"):
            await r.call(sid, "project_report", text="Again", kind="done", dispatch_id=dispatch.id)
        # The report is in the journal and its notification is quiet: the main orchestrator speaks for it.
        [entry] = [e for e in await r.manager.projects.journal(r.project.id, limit=20) if e.kind == "report" and "live" in e.text]
        assert entry.refs["dispatch_id"] == dispatch.id
    finally:
        await r.manager.close()


async def test_a_report_on_another_projects_dispatch_is_refused(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        sid = await office(r)
        (tmp_path / "garden").mkdir()
        other = await r.manager.projects.create("Garden", [str(tmp_path / "garden")])
        foreign = await dispatches.create(other, text="Plant tomatoes")
        with pytest.raises(Refused, match="no dispatch"):
            await r.call(sid, "project_report", text="done", kind="done", dispatch_id=foreign.id)
        with pytest.raises(Refused, match="no dispatch"):
            await r.call(sid, "ask_operator", title="Which tomatoes?", text="Which tomatoes?", dispatch_id=foreign.id)
        still = await r.manager.dispatches.get(foreign.id)
        assert still is not None and still.open
    finally:
        await r.manager.close()


async def test_a_follow_up_reopens_a_blocked_dispatch_and_a_done_one_takes_none(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        sid = await office(r)
        dispatch = await dispatches.create(await r.refreshed(), text="Deploy the site")
        await r.call(sid, "project_report", text="Needs the hosting password", kind="blocked", dispatch_id=dispatch.id)
        blocked = await r.manager.dispatches.get(dispatch.id)
        assert blocked is not None and blocked.status == "blocked"
        again = await dispatches.follow_up(blocked, "The password is in the vault, entry 'hosting'")
        assert again.status == "open"
        [message] = [e for e in await events(r.manager, "dispatch.message") if e.payload["author"] == "dispatcher"]
        assert (await r.orch.classify(r.project.id, message)) is not None, "a follow-up from the main orchestrator wakes the project"
        await r.call(sid, "project_report", text="Deployed", kind="done", dispatch_id=dispatch.id)
        done = await r.manager.dispatches.get(dispatch.id)
        assert done is not None
        with pytest.raises(DispatchError, match="new dispatch"):
            await dispatches.follow_up(done, "one more thing")
    finally:
        await r.manager.close()


async def test_cancel_tells_the_project_and_closes_the_dispatch(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        await office(r)
        dispatch = await dispatches.create(await r.refreshed(), text="Rewrite the menu in French")
        cancelled = await dispatches.cancel(dispatch, reason="the operator changed their mind")
        assert cancelled.status == "cancelled" and cancelled.result == "the operator changed their mind"
        [told] = [e for e in await events(r.manager, "dispatch.message") if e.payload["kind"] == "cancelled"]
        wake = await r.orch.classify(r.project.id, told)
        assert wake is not None and wake.urgent
        line = await r.orch.line(await r.refreshed(), told)
        assert f"dispatch {dispatch.id} is cancelled" in line
        [closed] = await events(r.manager, "dispatch.closed")
        assert closed.payload["by"] == "dispatcher"
        with pytest.raises(DispatchError, match="already cancelled"):
            await dispatches.cancel(cancelled)
    finally:
        await r.manager.close()


# -- the questions under a dispatch ------------------------------------------------------------------------


async def test_a_linked_question_outlives_a_done_dispatch_and_is_withdrawn_with_a_cancelled_one(settings: Settings, db: Database, tmp_path: Path) -> None:
    # An orchestrator that finished a survey and asked what only the operator knows is still waiting on
    # that answer. Closing the dispatch as done once withdrew such a question before the operator saw it.
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        sid = await office(r)
        dispatch = await dispatches.create(await r.refreshed(), text="Survey the folders")
        said = await r.call(sid, "ask_operator", title="What matters most now?", text="What matters most now?", dispatch_id=dispatch.id)
        assert "shown in the main orchestrator's chat too" in said
        [ask] = await r.manager.asks.open_for(r.project.id)
        assert ask.dispatch_id == dispatch.id
        [pending] = await events(r.manager, "ask.pending")
        assert pending.payload["dispatch_id"] == dispatch.id, "the link is in the row before anyone announces it"
        await r.call(sid, "project_report", text="Brief written; the priorities are asked", kind="done", dispatch_id=dispatch.id)
        kept = await r.manager.asks.get(ask.id)
        assert kept is not None and kept.open, "a done dispatch leaves its question for the operator"
        assert await events(r.manager, "ask.answered") == []

        second = await dispatches.create(await r.refreshed(), text="Choose a database")
        await r.call(sid, "ask_operator", title="Postgres or SQLite?", text="Postgres or SQLite?", options=["Postgres", "SQLite"], dispatch_id=second.id)
        [asked] = [a for a in await r.manager.asks.open_for(r.project.id) if a.dispatch_id == second.id]
        await dispatches.cancel(second, reason="not needed any more")
        withdrawn = await r.manager.asks.get(asked.id)
        assert withdrawn is not None and not withdrawn.open and withdrawn.resolved_by == "system" and "cancelled" in withdrawn.resolution["closed"]
        [answered] = await events(r.manager, "ask.answered")
        assert answered.payload["via"] == "withdrawn"
        assert await r.orch.classify(r.project.id, answered) is None, "a withdrawn question brings the orchestrator no answer"
        assert (await r.manager.asks.get(ask.id)).open, "cancelling one dispatch leaves another's question alone"
    finally:
        await r.manager.close()


async def test_during_the_setup_every_request_of_the_project_belongs_to_dispatch_one(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        fake(r)
        sid = await office(r)
        await r.manager.projects.set_setup(r.project.id, "dispatcher")
        survey = await dispatches.create(await r.refreshed(), text="Survey and write the brief", kind="setup")
        _member, live = await working(r)
        ask_id = await r.team.ingress.question(live, "req-1", "Which branch is production?", ["main", "release"])
        staff_ask = await r.manager.asks.get(ask_id)
        assert staff_ask is not None and staff_ask.dispatch_id == survey.id
        await r.call(sid, "ask_operator", title="What is the deadline?", text="What is the deadline?")
        own = [a for a in await r.manager.asks.open_for(r.project.id) if a.origin == "orchestrator"]
        assert own and own[0].dispatch_id == survey.id, "linked without the orchestrator naming it"
        state = await r.orch.project_state(await r.refreshed(), session_id=sid)
        assert "Setup: the main orchestrator is setting this project up" in state

        await r.call(sid, "project_report", text="The brief is written", kind="done", dispatch_id=survey.id)
        project = await r.refreshed()
        assert project.setup_by == "", "dispatch #1 closing as done ends the setup"
        await r.call(sid, "ask_operator", title="Anything else?", text="Anything else?")
        later = [a for a in await r.manager.asks.open_for(r.project.id) if a.text.startswith("Anything else")]
        assert later and later[0].dispatch_id is None
        assert not await dispatches.finish_setup(r.project.id, by="operator"), "finishing a finished setup changes nothing"
    finally:
        await r.manager.close()


async def test_finish_setup_by_hand_unlinks_the_next_requests(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        sid = await office(r)
        await r.manager.projects.set_setup(r.project.id, "dispatcher")
        await dispatches.create(await r.refreshed(), text="Survey", kind="setup")
        assert await dispatches.finish_setup(r.project.id, by="operator")
        await r.call(sid, "ask_operator", title="What is the deadline?", text="What is the deadline?")
        [ask] = await r.manager.asks.open_for(r.project.id)
        assert ask.dispatch_id is None
        [changed] = [e for e in await events(r.manager, "project.changed") if e.payload.get("change") == "setup.finished"]
        assert changed.payload["actor"] == "operator"
    finally:
        await r.manager.close()


async def test_a_replaced_orchestrators_linked_questions_are_withdrawn(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        sid = await office(r)
        dispatch = await dispatches.create(await r.refreshed(), text="Choose a database")
        await r.call(sid, "ask_operator", title="Postgres or SQLite?", text="Postgres or SQLite?", dispatch_id=dispatch.id)
        await r.call(sid, "ask_operator", title="Unrelated: new logo?", text="Unrelated: new logo?")
        await r.orch.replace(r.project.id, "testing")
        assert await dispatches.withdraw_project(r.project.id, why="the orchestrator that asked was replaced") == 1
        left = await r.manager.asks.open_for(r.project.id)
        assert [a.text for a in left] == ["Unrelated: new logo?"], "only the question shown under a dispatch is withdrawn"
    finally:
        await r.manager.close()


# -- the watchdog -------------------------------------------------------------------------------------------


async def test_a_quiet_dispatch_is_stalled_once_and_only_while_nobody_works(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install(r)
        fake(r)
        await office(r)
        dispatch = await dispatches.create(await r.refreshed(), text="Tidy the photos")
        base = dispatches.clock()
        dispatches.clock = lambda: base + timedelta(minutes=10)
        assert await dispatches.tick() == 0, "ten minutes is not quiet yet"
        _member, live = await working(r)
        dispatches.clock = lambda: base + timedelta(minutes=45)
        assert await dispatches.tick() == 0, "a member at work means the dispatch is being worked on"
        await r.team.ingress.status(live, "idle")
        assert await dispatches.tick() == 1
        assert await dispatches.tick() == 0, "said once per silence"
        [stalled] = await events(r.manager, "dispatch.stalled")
        assert stalled.payload["dispatch_id"] == dispatch.id and stalled.payload["minutes"] >= 30
        await r.manager.dispatches.add_message(dispatch.id, author="orchestrator", kind="progress", text="Half done")
        dispatches.clock = lambda: base + timedelta(minutes=20)
        assert await dispatches.tick() == 0, "a message is activity: the silence starts again from it"
        dispatches.clock = lambda: base + timedelta(minutes=40)
        assert await dispatches.tick() == 1, "and a new silence is told again"
    finally:
        await r.manager.close()
