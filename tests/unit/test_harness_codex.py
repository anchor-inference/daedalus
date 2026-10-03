"""The Codex adapter: its launch plan, its reading of Codex's screens and of what the app server said —
checked against what the real Codex showed and sent (``tests/support/fake_cli/recorded/codex``) — and
whole sessions through the staff runtime against the fake Codex, its app server a companion terminal
and the host's client on the server's WebSocket through the daemon.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions.harness import install as install_harness
from daedalus.harness import ADAPTERS
from daedalus.harness.codex import SKILL_FILE, THREAD_FILE, CodexAdapter, _Launch, parse_rollout
from daedalus.harness.contract import DIAL_DIR, LAUNCH_DIR, EventKind, LaunchSpec, ScreenClass, StaffEvent
from daedalus.harness.runtime import CliStaffRuntime, RuntimeEnvironment
from daedalus.harness.selfcheck import session_check
from daedalus.harness.tools import parse_codex_models, tooling
from daedalus.staff_runtime import ReadRequest
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from tests.support.authorized_launch import operator_assignment
from tests.support.fake_cli.fake_codex import parse_overrides
from tests.support.fake_cli.tui import read_log
from tests.unit.test_cli_staff_runtime import Stand, eventually, stand

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

RECORDED = Path(__file__).resolve().parents[1] / "support" / "fake_cli" / "recorded" / "codex"


def screen(name: str) -> str:
    return (RECORDED / "screens" / f"{name}.txt").read_text(encoding="utf-8")


def spec(**changes: Any) -> LaunchSpec:
    values: dict[str, Any] = {
        "harness": "codex", "env": "container", "cwd": "/work/bakery", "launch_id": "l1", "first_prompt": "[team] line\n\n[task t1] Menu",
        "title": "Ada · Menu page", "team_block": "You are Ada.", "team_skill": "---\nname: daedalus-team\n---\n", "model": "gpt-6-luna",
    }
    values.update(changes)
    return LaunchSpec(**values)


def overrides(argv: tuple[str, ...]) -> dict[str, Any]:
    """The ``-c`` values of an argv, read the way Codex reads them: TOML under dotted keys."""
    return parse_overrides([argv[i + 1] for i, word in enumerate(argv) if word == "-c"])


# -- the plan -------------------------------------------------------------------------------------


def test_the_adapter_is_registered_and_its_plan_runs_the_server_beside_the_tui() -> None:
    assert ADAPTERS["codex"] is CodexAdapter
    plan = CodexAdapter().launch_plan(spec())
    [server] = plan.companions
    assert server.role == "app-server" and server.argv[:4] == ("codex", "app-server", "--listen", f"unix://{DIAL_DIR}/codex.sock")
    config = overrides(server.argv)
    team = config["mcp_servers"]["daedalus_team"]
    assert team["command"] == "sh" and team["args"] == ["-c", 'exec "$DAEDALUS_PTYD_BIN" team-mcp']
    # Codex filters an MCP server's environment: the bridge's variables are passed by name.
    assert {"DAEDALUS_HOOK_URL", "DAEDALUS_HOOK_TOKEN", "DAEDALUS_PTYD_BIN"} <= set(team["env_vars"])
    assert team["env"] == {"DAEDALUS_ASK_HOLD_MS": "300000", "DAEDALUS_REPORT_HOLD_MS": "15000"}
    assert team["tool_timeout_sec"] > 300 and team["default_tools_approval_mode"] == "approve"
    assert config["projects"]["/work/bakery"]["trust_level"] == "trusted" and config["check_for_update_on_startup"] is False
    # The TUI waits for the thread's name, then attaches with its own settings.
    assert plan.argv[:2] == ("sh", "-c") and plan.argv[4:6] == (f"{LAUNCH_DIR}/{THREAD_FILE}", f"unix://{DIAL_DIR}/codex.sock")
    tui = overrides(plan.argv[6:])
    assert tui == {"check_for_update_on_startup": False, "notice": {"hide_rate_limit_model_nudge": True}}
    assert plan.files[SKILL_FILE].startswith(b"---\nname: daedalus-team") and plan.first_prompt_via == "channel" and plan.session_ref == ""


def test_modes_follow_the_projects_autonomy_and_a_resume_names_the_thread() -> None:
    adapter = CodexAdapter()
    for level, expected in (("ask", ("workspace-write", "untrusted")), ("edits", ("workspace-write", "on-request")), ("all", ("danger-full-access", "never"))):
        adapter.launch_plan(spec(permission_level=level, launch_id=level))
        planned = adapter._planned[level]
        assert (planned.sandbox, planned.approval) == expected
    adapter.launch_plan(spec(permission_mode="read-only", launch_id="ro"))
    assert adapter._planned["ro"].sandbox == "read-only"
    resumed = adapter.resume_plan(spec(first_prompt=None, launch_id="r"), "01a0d59e-aee3-7fa2-81c5-60ccc0b5d05c")
    assert resumed.session_ref == "01a0d59e-aee3-7fa2-81c5-60ccc0b5d05c" and adapter._planned["r"].resume == resumed.session_ref


def test_the_catalog_reads_the_real_model_list() -> None:
    printed = json.dumps({"models": [{"slug": "gpt-6-astra", "visibility": "list"}, {"slug": "gpt-6-luna", "visibility": "list"}, {"slug": "codex-auto-review", "visibility": "hide"}]})
    models = parse_codex_models(printed)
    assert models == ("gpt-6-astra", "gpt-6-luna") and tooling("codex").cheapest_model(models) == "gpt-6-luna"
    assert "ultra" in tooling("codex").efforts and "minimal" not in tooling("codex").efforts


# -- the recorded screens and messages --------------------------------------------------------------


def test_the_readiness_gate_and_the_screen_reading_on_the_real_screens() -> None:
    adapter = CodexAdapter()
    update = screen("update-prompt")
    assert adapter.readiness(update).keys == ("Down",)
    moved = update.replace("› 1. Update now", "  1. Update now").replace("  2. Skip\n", "› 2. Skip\n")
    assert adapter.readiness(moved).keys == ("Enter",)
    failed = adapter.readiness(screen("resume-fresh-thread"))
    assert failed.action == "fail" and "Failed to resume session" in failed.reason
    assert adapter.readiness(screen("idle")).action == "wait"
    assert adapter.classify_screen(screen("idle")) is ScreenClass.IDLE_COMPOSER
    assert adapter.classify_screen(screen("resumed-idle")) is ScreenClass.IDLE_COMPOSER
    assert adapter.classify_screen(update) is ScreenClass.DIALOG
    assert adapter.classify_screen(screen("rate-limit-nudge")) is ScreenClass.DIALOG


def drain(state: _Launch) -> list[StaffEvent]:
    events = []
    while not state.queue.empty():
        event = state.queue.get_nowait()
        assert event is not None
        events.append(event)
    return events


def test_the_real_app_server_messages_become_events() -> None:
    received = [json.loads(line) for line in (RECORDED / "app_server.jsonl").read_text(encoding="utf-8").splitlines()]
    notes = [r["msg"] for r in received if r["client"] == "second" and r["dir"] == "received" and "method" in r["msg"] and "id" not in r["msg"]]
    thread = next(r["msg"]["result"]["thread"]["id"] for r in received if r["client"] == "second" and r["dir"] == "received" and r["msg"].get("id") == 5)
    adapter = CodexAdapter()
    state = _Launch(thread_id=thread)
    state.client_ids["m-spike-3"] = "message-1"
    for note in notes:
        adapter._notification(state, note["method"], note.get("params") or {})
    events = drain(state)
    kinds = [e.kind for e in events]
    assert EventKind.TURN_STARTED in kinds
    [ack] = [e for e in events if e.kind is EventKind.PROMPT_ACKNOWLEDGED]
    assert ack.payload["message_id"] == "message-1" and ack.payload["prompt"].startswith("Call the Report tool")
    # The account was at its usage limit: the turn failed, and says why in Codex's words.
    [failed] = [e for e in events if e.kind is EventKind.TURN_FAILED]
    assert failed.payload["failure"].startswith("You’ve hit your usage limit") and failed.payload["code"] == "usageLimitExceeded"
    # The server's own helper threads (titles, memories) are not the member's.
    assert not any(e.kind is EventKind.TURN_STARTED for e in events[kinds.index(EventKind.TURN_FAILED) + 1 :])


def test_a_rollout_is_read_as_turns() -> None:
    lines = [
        {"type": "session_meta", "payload": {"id": "t1"}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "You are Ada."}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        {"timestamp": "2026-09-25T00:00:01Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "[orchestrator] write the menu"}]}},
        {"timestamp": "2026-09-25T00:00:02Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": json.dumps({"command": ["ls", "-la"]})}},
        {"timestamp": "2026-09-25T00:00:03Z", "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "The menu is written."}]}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 90}}}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1500, "cached_input_tokens": 1100, "output_tokens": 10}, "model_context_window": 258400}, "rate_limits": {"primary": {"used_percent": 12.0, "window_minutes": 300}, "secondary": {"used_percent": 55.5, "window_minutes": 10080}}}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
        "not json",
    ]
    turns = parse_rollout("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines))
    assert [(t.role, t.text) for t in turns] == [("orchestrator", "[orchestrator] write the menu"), ("assistant", "The menu is written.")]
    assert turns[1].tools[0].summary == "ls -la" and turns[1].usage is not None
    assert (turns[1].usage.input_tokens, turns[1].usage.output_tokens, turns[1].usage.cache_read_tokens) == (800, 100, 1900)
    # The fill is the last call's whole prompt, not the sum of the turn's calls; the window and the
    # rate limits are what Codex reported beside it.
    assert (turns[1].usage.context_tokens, turns[1].usage.context_window) == (1500, 258400)
    assert turns[1].usage.windows == ((300, 12.0), (10080, 55.5))


# -- whole sessions through the runtime -------------------------------------------------------------


def codex(**config: Any) -> dict[str, Any]:
    return {"adapter": CodexAdapter(), **config}


def log(s: Stand, what: str) -> list[dict[str, Any]]:
    return [e for e in read_log(s.log) if e["event"] == what]


async def started(s: Stand, script: str, *, name: str = "Ada") -> Any:
    member = await s.hire(name)
    task_id = await s.task(f"Menu page;{script};")
    assigned = await operator_assignment(s.team, member, task_id)
    assert assigned["state"] == "queued" and assigned["effect_id"]
    return member


async def message(s: Stand, message_id: str, state: str, *, timeout: float = 30.0) -> Any:
    async def reached() -> bool:
        found = await s.manager.staff.message(message_id)
        return found is not None and found.state == state

    await eventually(reached, f"message {message_id} {state}", timeout=timeout)
    return await s.manager.staff.message(message_id)


async def test_a_codex_session_from_its_start_to_its_release(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **codex()) as s:
        ada = await started(s, "echo:the menu is written")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        launch = await HarnessStore(db).open_launch_for(row.id)
        assert launch is not None and launch.companion_terminal_id and row.cli_session_id
        companion = await s.terminals.get(launch.companion_terminal_id)
        assert companion["title"].endswith("· app-server")
        # The protocol reached Codex three ways: developer instructions, a skill, the first prompt.
        [instructions] = log(s, "developer_instructions")
        assert instructions["text"].startswith("You are Ada, a staff member of the project Bakery")
        assert log(s, "skills")[0]["names"] == ["daedalus-team"]
        assert s.runtime.channel(await s.team.live(row.id))["team_tools"] == "connected"  # type: ignore[arg-type]
        # The first message went by turn/start and was acknowledged by the id it carried.
        assert [m.state for m in await s.manager.staff.messages(ada.id)] == ["acknowledged"]
        # Nothing was typed into the TUI: every message went by the app server.
        writes = await db.fetchall("SELECT detail_json FROM terminal_audit WHERE terminal_id = ? AND action = 'write'", (row.terminal_id,))
        assert writes == []
        # The TUI attached once the host named the thread, and follows it from there.
        attached = await s.terminals.wait_for(row.terminal_id, regex=f"resumed thread {row.cli_session_id}", timeout=10)
        assert attached["matched"] == "regex", "\n".join((await s.terminals.read_screen(row.terminal_id, scrollback=100))["lines"])
        [report] = await s.events("staff.report", staff_id=ada.id)
        assert report.payload["kind"] == "turn_done" and report.payload["implicit"] is True and "the menu is written" in report.payload["text"]
        last = await s.runtime.read(await s.team.live(row.id), ReadRequest("last"))  # type: ignore[arg-type]
        assert last.text == "the menu is written"
        assert row.transcript_ref.endswith(f"{row.cli_session_id}.jsonl")
        assert await s.team.release(ada)
        assert (await s.terminals.get(row.terminal_id))["exit_code"] == 0
        assert await HarnessStore(db).open_launches() == []


@pytest.mark.parametrize("approvals", ["all", "owner"])
async def test_a_permission_is_answered_through_the_app_server(settings: Settings, db: Database, approvals: str) -> None:
    async with stand(settings, db, extra_env={"FAKE_CODEX_APPROVALS": approvals}, **codex()) as s:
        ada = await started(s, "perm:npm install grammy")
        await s.status_event(ada, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert ask.request_ref.startswith("codex-") and "npm install grammy" in ask.text
        answered = await s.team.answer(ask.short_id, allow=True, by="operator")
        assert answered["delivered"] is True
        await s.status_event(ada, "turn_done_unseen")
        assert await s.statuses(ada) == ["starting", "working", "permission", "working", "turn_done_unseen"]
        [answer] = [e for e in log(s, "server_request_answered")]
        assert answer["by"] == "daedalus" and answer["answer"] == {"decision": "accept"}
        row = await s.session_row(ada)
        screen_now = "\n".join((await s.terminals.read_screen(row.terminal_id, scrollback=100))["lines"])
        assert "Ran npm install grammy." in screen_now and "Would you like to run" not in screen_now


async def test_a_permission_answered_in_the_tui_closes_its_request(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **codex()) as s:
        ada = await started(s, "perm:rm -rf build")
        await s.status_event(ada, "permission")
        row = await s.session_row(ada)
        assert (await s.terminals.wait_for(row.terminal_id, regex="Would you like to run the following command", timeout=10))["matched"] == "regex"
        await s.ptyd.type_as_human(row.terminal_id, "3")
        await s.status_event(ada, "turn_done_unseen")
        [resolved] = await s.events("permission.resolved", staff_id=ada.id)
        assert (resolved.payload["by"], resolved.payload["via"]) == ("operator", "terminal")
        assert await s.manager.asks.open_for(s.project.id) == []


async def test_a_question_of_codexs_own_is_answered_with_its_option(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **codex()) as s:
        ada = await started(s, "ask:Tea or coffee?|Tea|Coffee")
        await s.status_event(ada, "question")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert ask.kind == "question" and ask.text == "Tea or coffee?" and ask.detail["options"] == ["Tea", "Coffee"]
        await s.team.answer(ask.short_id, selected=["Coffee"], by="orchestrator")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        assert "You chose: Coffee" in "\n".join((await s.terminals.read_screen(row.terminal_id, scrollback=100))["lines"])


async def test_a_message_for_now_goes_into_the_running_turn_by_turn_steer_and_one_for_after_waits(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **codex()) as s:
        ada = await started(s, "slow:1000")
        await s.status_event(ada, "working")
        queued = await s.team.tell(ada, "echo:after the turn", when="after_turn", by="orchestrator")
        steered = await s.team.tell(ada, "echo:steered in", when="now", by="orchestrator")
        assert steered["degraded_to"] is None
        await message(s, steered["message_id"], "acknowledged")
        assert (await HarnessStore(db).delivery(steered["message_id"])).via == "turn/steer"  # type: ignore[union-attr]
        await asyncio.sleep(0.5)
        assert (await s.manager.staff.message(queued["message_id"])).state == "queued"  # type: ignore[union-attr]
        await s.team.interrupt(ada)
        await s.status_event(ada, "idle")
        await message(s, queued["message_id"], "acknowledged")
        assert (await HarnessStore(db).delivery(queued["message_id"])).via == "turn/start"  # type: ignore[union-attr]
        live = await s.team.live((await s.session_row(ada)).id)

        async def both_in_the_thread() -> bool:
            texts = [t.text for t in await s.runtime.turns(live) if t.role == "orchestrator"]  # type: ignore[arg-type]
            return texts.count("[orchestrator] echo:steered in") == 1 and texts.count("[orchestrator] echo:after the turn") == 1

        # Acknowledged is Codex taking the turn/start; the thread records the new turn's message a moment
        # later. Read at once on a loaded machine, the thread still ended with the steered message.
        await eventually(both_in_the_thread, "both messages are in the thread, once each")


async def test_after_a_host_restart_the_thread_is_taken_up_again(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **codex()) as s:
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        told = await s.team.tell(ada, "echo:once only", by="operator")
        await message(s, told["message_id"], "acknowledged")
        await s.status_event(ada, "turn_done_unseen")
        # The host went away after Codex took the message and before it heard so; another waits.
        await db.execute("UPDATE staff_messages SET state = 'submitted' WHERE id = ?", (told["message_id"],))
        pending = await s.manager.staff.add_message(ada.id, "echo:still to go", origin="operator", mode="after_turn", staff_session_id=(await s.session_row(ada)).id)
        # A new adapter knows nothing of the launch but its thread: it resumes it on the same server.
        runtime = s.restart_runtime(CodexAdapter())
        assert await runtime.reconcile(wait=5) == 1
        await message(s, told["message_id"], "acknowledged")
        await message(s, pending.id, "acknowledged")
        live = await s.team.live((await s.session_row(ada)).id)
        texts = [t.text for t in await s.runtime.turns(live) if t.role == "user"]  # type: ignore[arg-type]
        assert texts.count("echo:once only") == 1 and texts.count("echo:still to go") == 1


async def test_a_failed_turn_is_an_error_in_codexs_words(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **codex()) as s:
        ada = await started(s, "fail:usageLimitExceeded")
        failed = await s.status_event(ada, "error")
        assert "usageLimitExceeded" in failed.payload["detail"]


async def test_the_self_check_runs_a_codex_session_through_every_channel(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **codex()) as s:
        env = RuntimeEnvironment(s.terminals, "container", home=str(s.home))
        result = await session_check(CodexAdapter(), s.terminals, lambda: s.harness, env, "gpt-6-luna", True)
        assert result.ok, result.steps
        assert [step.name for step in result.steps] == ["launch", "ready", "team", "deliver", "reply", "exit"]
        assert result.steps[5].detail.startswith("exit code 0")
        quiet = await session_check(CodexAdapter(), s.terminals, lambda: s.harness, env, "", False)
        assert quiet.ok and [step.name for step in quiet.steps] == ["launch", "ready", "team", "exit"], quiet.steps
        running = [t for t in await s.terminals.list() if t["status"] == "running"]
        assert running == []  # the app servers went with their checks


async def test_installing_the_extension_registers_the_codex_and_opencode_runtimes(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **codex()) as s:
        app = SimpleNamespace(manager=s.manager, extensions={"terminals": s.terminals, "staff": s.team}, config=SimpleNamespace(harness=s.harness), db=db)
        tasks = await install_harness(app)  # type: ignore[arg-type]
        try:
            for name in ("codex", "opencode"):
                assert isinstance(s.team.runtimes[name], CliStaffRuntime)
                assert name in app.extensions["harness"].self_checks
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
