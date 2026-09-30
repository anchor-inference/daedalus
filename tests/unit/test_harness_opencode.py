"""The OpenCode adapter: its launch plan, its reading of OpenCode's screens, events and messages —
checked against what the real OpenCode showed and published (``tests/support/fake_cli/recorded/opencode``)
— and whole sessions through the staff runtime against the fake OpenCode, its server reached on the
launch's port through the daemon.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.harness import ADAPTERS
from daedalus.harness.contract import LAUNCH_DIR, EventKind, LaunchSpec, ScreenClass, StaffEvent
from daedalus.harness.opencode import SKILL_FILE, SYSTEM_FILE, OpenCodeAdapter, _Launch, parse_messages, split_model
from daedalus.harness.runtime import RuntimeEnvironment
from daedalus.harness.selfcheck import session_check
from daedalus.staff_runtime import ReadRequest
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from tests.support.fake_cli.tui import read_log
from tests.unit.test_cli_staff_runtime import Stand, eventually, stand

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

RECORDED = Path(__file__).resolve().parents[1] / "support" / "fake_cli" / "recorded" / "opencode"


def screen(name: str) -> str:
    return (RECORDED / "screens" / f"{name}.txt").read_text(encoding="utf-8")


def spec(**changes: Any) -> LaunchSpec:
    values: dict[str, Any] = {
        "harness": "opencode", "env": "container", "cwd": "/work/bakery", "launch_id": "l1", "first_prompt": "[team] line\n\n[task t1] Menu",
        "title": "Ada · Menu page", "team_block": "You are Ada.", "team_skill": "---\nname: daedalus-team\n---\n", "model": "openrouter/cohere/north-mini-code:free",
        "port_range": (18300, 18300),
    }
    values.update(changes)
    return LaunchSpec(**values)


# -- the plan -------------------------------------------------------------------------------------


def test_the_adapter_is_registered_and_its_plan_puts_the_launch_in_its_configuration() -> None:
    assert ADAPTERS["opencode"] is OpenCodeAdapter
    plan = OpenCodeAdapter().launch_plan(spec())
    assert plan.argv == ("opencode", "/work/bakery", "--port", "18300", "--hostname", "127.0.0.1", "-m", "openrouter/cohere/north-mini-code:free")
    assert plan.ports == (18300,) and plan.first_prompt_via == "channel"
    assert len(plan.env["OPENCODE_SERVER_PASSWORD"]) >= 24
    config = json.loads(plan.env["OPENCODE_CONFIG_CONTENT"])
    team = config["mcp"]["daedalus_team"]
    assert team["type"] == "local" and team["command"] == ["sh", "-c", 'exec "$DAEDALUS_PTYD_BIN" team-mcp'] and team["timeout"] > 300_000
    assert config["instructions"] == [f"{LAUNCH_DIR}/{SYSTEM_FILE}"] and config["skills"] == {"paths": [f"{LAUNCH_DIR}/skills"]}
    assert config["permission"]["external_directory"] == {f"{LAUNCH_DIR}/**": "allow"} and config["permission"]["bash"] == "ask"
    assert plan.files[SYSTEM_FILE] == b"You are Ada." and plan.files[SKILL_FILE].startswith(b"---\nname: daedalus-team")
    everything = json.loads(OpenCodeAdapter().launch_plan(spec(permission_level="all")).env["OPENCODE_CONFIG_CONTENT"])["permission"]
    assert (everything["bash"], everything["edit"]) == ("allow", "allow")
    resumed = OpenCodeAdapter().resume_plan(spec(first_prompt=None, agent="plan"), "ses_f2a5df6a3ffes2ehVip5aC7peL")
    assert "--agent" in resumed.argv and resumed.argv[resumed.argv.index("--agent") + 1] == "plan"
    assert resumed.argv[resumed.argv.index("-s") + 1] == "ses_f2a5df6a3ffes2ehVip5aC7peL"
    assert split_model("openrouter/cohere/north-mini-code:free") == ("openrouter", "cohere/north-mini-code:free")


# -- the recorded screens, events and messages ------------------------------------------------------


def test_the_real_screens_are_classified() -> None:
    adapter = OpenCodeAdapter()
    assert adapter.classify_screen(screen("idle")) is ScreenClass.IDLE_COMPOSER
    assert adapter.classify_screen(screen("permission")) is ScreenClass.DIALOG
    assert adapter.classify_screen(screen("after-permission")) is ScreenClass.IDLE_COMPOSER
    assert adapter.readiness(screen("idle")).action == "wait"
    assert adapter.readiness("Error: Failed to start server on port 18300: address in use").action == "fail"


def drain(state: _Launch) -> list[StaffEvent]:
    events = []
    while not state.queue.empty():
        event = state.queue.get_nowait()
        assert event is not None
        events.append(event)
    return events


def test_the_real_event_stream_becomes_events() -> None:
    recorded = [json.loads(line)["event"] for line in (RECORDED / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    session = next(e["properties"]["sessionID"] for e in recorded if e["type"] == "session.created")
    adapter = OpenCodeAdapter()
    state = _Launch(session=session)
    # The third prompt was the one sent with a model the server knew.
    state.sent["msg_2793b66ef06c15941868077d"] = ("message-3", "Call the Report tool")
    for event in recorded:
        adapter._event(state, event["type"], event.get("properties") or {})
    events = drain(state)
    kinds = [e.kind for e in events]
    # Two prompts failed at the provider, before any tool: each an error in OpenCode's words.
    failures = [e.payload["failure"] for e in events if e.kind is EventKind.TURN_FAILED]
    assert failures[0].startswith("No endpoints found that support tool use") and "Model not found" in failures[1]
    [ack] = [e for e in events if e.kind is EventKind.PROMPT_ACKNOWLEDGED]
    assert ack.payload["message_id"] == "message-3"
    started = [e.payload["tool"] for e in events if e.kind is EventKind.TOOL_STARTED]
    assert started[:2] == ["daedalus_team_Report", "bash"]
    completed = [e for e in events if e.kind is EventKind.TURN_COMPLETED]
    assert [c.payload["last_message"] for c in completed] == ["PELICAN, yes", "done"]
    [permission] = [e for e in events if e.kind is EventKind.PERMISSION_REQUESTED]
    assert (permission.payload["tool"], permission.payload["summary"]) == ("bash", "ls") and permission.native_id.startswith("per_")
    # Replied to over HTTP by someone else than this adapter: the request is resolved for the team.
    assert any(e.kind is EventKind.REQUEST_RESOLVED and e.native_id == permission.native_id for e in events)
    assert kinds.index(EventKind.PERMISSION_REQUESTED) < kinds.index(EventKind.REQUEST_RESOLVED)


def test_the_real_messages_are_read_as_turns() -> None:
    turns = parse_messages(json.loads((RECORDED / "messages.json").read_text(encoding="utf-8")))
    assert [t.role for t in turns].count("user") == 4
    report = next(t for t in turns if t.tools and t.tools[0].name == "daedalus_team_Report")
    assert report.tools[0].ok is True and report.usage is not None and report.usage.input_tokens > 0
    assert [t.text for t in turns if t.role == "assistant" and t.text] == ["PELICAN, yes", "done"]
    bash = next(t for t in turns if t.tools and t.tools[0].name == "bash")
    assert bash.tools[0].summary == "ls"
    exported = json.loads((RECORDED / "export.json").read_text(encoding="utf-8"))
    assert [t.text for t in parse_messages(exported["messages"])] == [t.text for t in turns]


# -- whole sessions through the runtime -------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def opencode(**config: Any) -> dict[str, Any]:
    port = free_port()
    return {"adapter": OpenCodeAdapter(), "opencode_port_range": f"{port}-{port}", **config}


def log(s: Stand, what: str) -> list[dict[str, Any]]:
    return [e for e in read_log(s.log) if e["event"] == what]


async def started(s: Stand, script: str, *, name: str = "Ada") -> Any:
    member = await s.hire(name)
    task_id = await s.task(f"Menu page;{script};")
    assert (await s.team.assign(member, task_id))["state"] == "started"
    return member


async def message(s: Stand, message_id: str, state: str, *, timeout: float = 30.0) -> Any:
    async def reached() -> bool:
        found = await s.manager.staff.message(message_id)
        return found is not None and found.state == state

    await eventually(reached, f"message {message_id} {state}", timeout=timeout)
    return await s.manager.staff.message(message_id)


async def test_an_opencode_session_from_its_start_to_its_release(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **opencode()) as s:
        ada = await started(s, "echo:the menu is written")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        assert row.cli_session_id and row.cli_session_id.startswith("ses_")
        [system] = log(s, "system_prompt")
        assert system["text"].startswith("You are Ada, a staff member of the project Bakery")
        assert log(s, "skills")[0]["names"] == ["daedalus-team"]
        # The team's tools connect beside the turn, not before it ends: the channel says "waiting"
        # until the plugin has called in, which on a loaded machine can be after the turn is done.
        async def tools_connected() -> bool:
            return s.runtime.channel(await s.team.live(row.id))["team_tools"] == "connected"  # type: ignore[arg-type]

        await eventually(tools_connected, "the team's tools connected")
        assert [m.state for m in await s.manager.staff.messages(ada.id)] == ["acknowledged"]
        # The first message went by the server, under the id the host chose, into the session the TUI shows.
        [submitted] = [e for e in log(s, "submitted") if e.get("via") == "prompt_async"]
        assert submitted["text"].startswith("[team] You are Ada, staff of Bakery")
        writes = await db.fetchall("SELECT detail_json FROM terminal_audit WHERE terminal_id = ? AND action = 'write'", (row.terminal_id,))
        assert writes == []
        [report] = await s.events("staff.report", staff_id=ada.id)
        assert report.payload["implicit"] is True and "the menu is written" in report.payload["text"]
        last = await s.runtime.read(await s.team.live(row.id), ReadRequest("last"))  # type: ignore[arg-type]
        assert last.text == "the menu is written"
        assert await s.team.release(ada)
        assert (await s.terminals.get(row.terminal_id))["exit_code"] == 0
        assert await HarnessStore(db).open_launches() == []
        # After the TUI has gone, the conversation is still read, through its export.
        live_turns = await s.adapter.transcript(RuntimeEnvironment(s.terminals, "container", home=str(s.home)), row.cli_session_id)
        assert [t.text for t in live_turns if t.role == "assistant"] == ["the menu is written"]


async def test_a_permission_is_answered_through_the_server(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **opencode()) as s:
        ada = await started(s, "perm:npm install grammy")
        await s.status_event(ada, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert ask.request_ref.startswith("per_") and "npm install grammy" in ask.text
        answered = await s.team.answer(ask.short_id, allow=True, by="operator")
        assert answered["delivered"] is True
        await s.status_event(ada, "turn_done_unseen")
        assert await s.statuses(ada) == ["starting", "working", "permission", "working", "turn_done_unseen"]
        assert not log(s, "dialog_answered")


async def test_a_permission_answered_in_the_tui_closes_its_request(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **opencode()) as s:
        ada = await started(s, "perm:rm -rf build")
        await s.status_event(ada, "permission")
        row = await s.session_row(ada)
        assert (await s.terminals.wait_for(row.terminal_id, regex="Permission required", timeout=10))["matched"] == "regex"
        await s.ptyd.type_as_human(row.terminal_id, "3")
        await s.status_event(ada, "turn_done_unseen")
        [resolved] = await s.events("permission.resolved", staff_id=ada.id)
        assert (resolved.payload["by"], resolved.payload["via"]) == ("operator", "terminal")


async def test_a_question_is_answered_through_the_server(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **opencode()) as s:
        ada = await started(s, "ask:Tea or coffee?|Tea|Coffee")
        await s.status_event(ada, "question")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert ask.text == "Tea or coffee?" and ask.detail["options"] == ["Tea", "Coffee"]
        await s.team.answer(ask.short_id, selected=["Coffee"], by="orchestrator")
        await s.status_event(ada, "turn_done_unseen")


async def test_a_message_for_now_waits_for_the_turn_and_says_so_and_an_interrupt_ends_it(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **opencode()) as s:
        ada = await started(s, "slow:1000")
        await s.status_event(ada, "working")
        steered = await s.team.tell(ada, "echo:after the turn", when="now", by="orchestrator")
        assert steered["degraded_to"] == "after_turn"
        await asyncio.sleep(0.5)
        assert (await s.manager.staff.message(steered["message_id"])).state == "queued"  # type: ignore[union-attr]
        await s.team.interrupt(ada)
        await s.status_event(ada, "idle")
        await message(s, steered["message_id"], "acknowledged")
        # The fact row keeps what became of the timing, so a later look can tell it was asked for now.
        assert (await HarnessStore(db).delivery(steered["message_id"])).degraded_to == "after_turn"  # type: ignore[union-attr]
        await s.status_event(ada, "turn_done_unseen")


async def test_after_a_host_restart_the_member_is_taken_up_on_its_own_server(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **opencode()) as s:
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        launch = await HarnessStore(db).open_launch_for(row.id)
        assert launch is not None
        # The port and the password outlive the host in the launch row, and never show in its repr.
        stored = json.loads(launch.adapter_state)
        assert stored["port"] and stored["password"] and stored["password"] not in repr(launch)
        # A new adapter knows nothing of the launch: it reads the server back and takes up the session.
        runtime = s.restart_runtime(OpenCodeAdapter())
        assert await runtime.reconcile(wait=5) == 1
        told = await s.team.tell(ada, "echo:after the restart", by="operator")
        await message(s, told["message_id"], "acknowledged")
        await s.status_event(ada, "turn_done_unseen")
        assert (await s.session_row(ada)).cli_session_id == row.cli_session_id
        assert not [e for e in await s.events("staff.status", staff_id=ada.id) if e.payload["status"] == "error"]
        # Ended, the launch forgets the password.
        assert await s.team.release(ada)
        ended = await HarnessStore(db).launch(launch.launch_id)
        assert ended is not None and ended.ended_at and ended.adapter_state == ""


async def test_a_taken_port_is_planned_again(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    taken, free = free_port(), free_port()
    squatter = socket.socket()
    squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    squatter.bind(("127.0.0.1", taken))
    squatter.listen()
    chosen = iter([taken, free])
    monkeypatch.setattr("daedalus.harness.opencode.random.randint", lambda low, high: next(chosen))
    try:
        async with stand(settings, db, adapter=OpenCodeAdapter(), opencode_port_range=f"{min(taken, free)}-{max(taken, free)}") as s:
            ada = await started(s, "echo:on the second port")
            await s.status_event(ada, "turn_done_unseen")
            launches = await db.fetchall("SELECT launch_id, ended_at, adapter_state FROM harness_launches ORDER BY started_at")
            # The first plan was undone before its terminal was made; the second runs.
            assert len(launches) == 2 and launches[0]["ended_at"] and launches[0]["adapter_state"] == ""
            assert json.loads(launches[1]["adapter_state"])["port"] == free
    finally:
        squatter.close()


async def test_the_self_check_runs_an_opencode_session_through_every_channel(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **opencode()) as s:
        env = RuntimeEnvironment(s.terminals, "container", home=str(s.home))
        adapter = OpenCodeAdapter()
        result = await session_check(adapter, s.terminals, lambda: s.harness, env, "", True)
        assert result.ok, result.steps
        assert [step.name for step in result.steps] == ["launch", "ready", "team", "deliver", "reply", "exit"]
