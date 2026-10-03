"""The Grok Build adapter: its launch plan and agent file, its reading of Grok's screens, hooks and
session files — checked against what Grok Build 1.0.41 showed, posted and wrote
(``tests/support/fake_cli/recorded/grok``) — and whole sessions through the staff runtime against the
fake Grok in a real terminal, with the permission relay in the loop.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.harness import ADAPTERS
from daedalus.harness.contract import LAUNCH_DIR, EventKind, HookPost, Launch, LaunchSpec, ScreenClass, StaffEvent
from daedalus.harness.grok import AGENT_FILE, HOOK_EVENTS, GrokAdapter, composer, parse_transcript
from daedalus.harness.runtime import RuntimeEnvironment
from daedalus.harness.selfcheck import session_check
from daedalus.harness.tools import GrokTooling, parse_grok_login, parse_grok_models
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from tests.support.authorized_launch import operator_assignment
from tests.support.fake_cli.fake_grok import front_matter
from tests.support.fake_cli.tui import read_log
from tests.unit.test_cli_staff_runtime import Stand, eventually, stand

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

RECORDED = Path(__file__).resolve().parents[1] / "support" / "fake_cli" / "recorded" / "grok"


def screen(name: str) -> str:
    return (RECORDED / "screens" / f"{name}.txt").read_text(encoding="utf-8")


def spec(**changes: Any) -> LaunchSpec:
    values: dict[str, Any] = {
        "harness": "grok", "env": "container", "cwd": "/work/bakery", "launch_id": "l1", "first_prompt": "[team] line\n\n[task t1] Menu",
        "title": "Ada · Menu page", "team_block": "You are Ada.", "team_skill": "---\nname: daedalus-team\ndescription: x\n---\n\n# Working as a staff member\n", "model": "grok-code-fast",
        "effort": "high",
    }
    values.update(changes)
    return LaunchSpec(**values)


# -- the plan and the agent file --------------------------------------------------------------------


def test_the_adapter_is_registered_and_its_plan_puts_hooks_and_tools_in_an_agent_file(tmp_path: Path) -> None:
    assert ADAPTERS["grok"] is GrokAdapter
    plan = GrokAdapter().launch_plan(spec())
    argv = list(plan.argv)
    assert argv[:5] == ["grok", "--cwd", "/work/bakery", "-s", plan.session_ref] and "--trust" in argv
    assert argv[argv.index("--agent") + 1] == f"{LAUNCH_DIR}/{AGENT_FILE}" and argv[argv.index("--rules") + 1] == "You are Ada."
    assert argv[argv.index("--allow") + 1] == "MCPTool(daedalus_team__*)" and argv[argv.index("--disallowed-tools") + 1] == "ask_user_question"
    assert argv[argv.index("-m") + 1] == "grok-code-fast" and argv[argv.index("--permission-mode") + 1] == "default" and argv[argv.index("--effort") + 1] == "high"
    # The prompt after "--": a first word that starts with a dash is still the prompt.
    assert argv[-2:] == ["--", "[team] line\n\n[task t1] Menu"]
    assert plan.env["GROK_DEFAULT_SELECTED_PERMISSION"] == "allow_once" and plan.env["GROK_CLAUDE_HOOKS_ENABLED"] == "false"
    assert plan.env["DAEDALUS_ASK_HOLD_MS"] == "300000"
    # The agent file is YAML written as JSON values, one key per line: the fake reads it as Grok does.
    path = tmp_path / AGENT_FILE
    path.write_bytes(plan.files[AGENT_FILE])
    meta, body = front_matter(path)
    assert meta["name"] == "daedalus-staff" and sorted(meta["hooks"]) == sorted(HOOK_EVENTS)
    assert meta["hooks"]["Stop"][0]["hooks"][0]["command"] == '"$DAEDALUS_PTYD_BIN" hook Stop'
    [server] = meta["mcpServers"]  # a list: Grok ignores the whole file when it is a map
    assert server["name"] == "daedalus_team" and server["args"] == ["-c", 'exec "$DAEDALUS_PTYD_BIN" team-mcp']
    assert {e["name"]: e["value"] for e in server["env"]} == {"DAEDALUS_ASK_HOLD_MS": "300000", "DAEDALUS_REPORT_HOLD_MS": "15000"}
    assert body.startswith("# Working as a staff member")  # the skill, without its own front matter
    resumed = list(GrokAdapter().resume_plan(spec(first_prompt=None, permission_mode="", permission_level="all"), "11111111-1111-4111-8111-111111111111").argv)
    assert resumed[3:5] == ["-r", "11111111-1111-4111-8111-111111111111"] and "--" not in resumed
    assert resumed[resumed.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--effort" not in GrokAdapter().launch_plan(spec(effort="max")).argv


# -- the recorded screens, hooks and files ----------------------------------------------------------


def test_the_readiness_gate_and_the_screen_reader_on_the_real_screens() -> None:
    adapter = GrokAdapter()
    sign_in = adapter.readiness(screen("sign-in"))
    assert sign_in.action == "fail" and "not signed in" in sign_in.reason
    assert adapter.readiness(screen("welcome")).action == "wait"
    assert adapter.classify_screen(screen("sign-in")) is ScreenClass.DIALOG
    for idle in ("welcome", "after-first-turn", "after-ctrl-c", "send-now", "cleared", "permission-after-digit-3"):
        assert adapter.classify_screen(screen(idle)) is ScreenClass.IDLE_COMPOSER, idle
    for busy in ("busy", "after-esc", "typed-while-busy", "queued-while-busy"):
        assert adapter.classify_screen(screen(busy)) is ScreenClass.BUSY, busy
    assert adapter.classify_screen(screen("permission-dialog-2")) is ScreenClass.DIALOG
    # A suggestion Grok offers in its composer is nobody's text.
    assert composer(screen("after-first-turn")) == "" and composer(screen("welcome")) == ""
    assert composer(screen("typed-while-busy")) == "SPIKE-QUESTION instead"
    assert composer(screen("paste-2-lines")) == "one\ntwo"
    assert composer(screen("long-paste")) == "[Pasted: 30 lines]"
    assert adapter.composer_holds(screen("long-paste"), "line\n" * 30) and adapter.composer_holds(screen("paste-2-lines"), "one\ntwo")
    assert not adapter.composer_holds(screen("after-first-turn"), "no such tool among []")


class Replay:
    def __init__(self, posts: list[HookPost]) -> None:
        self.posts = posts

    @property
    def id(self) -> str:
        return "t-replay"

    async def hooks(self) -> AsyncIterator[HookPost]:
        for post in self.posts:
            yield post


async def replay(name: str) -> list[StaffEvent]:
    posts = [HookPost(entry["name"], entry["body"]) for entry in map(json.loads, (RECORDED / name).read_text().splitlines()) if entry["name"] != "team"]
    launch = Launch("l1", "s1", "grok", "container", "t-replay", None, "/run/l1", "", "1.0.41", "")
    return [e async for e in GrokAdapter().events(Replay(posts), launch)]  # type: ignore[arg-type]


async def test_the_real_hooks_become_events() -> None:
    events = await replay("hooks.jsonl")
    kinds = [e.kind for e in events]
    assert kinds[:2] == [EventKind.TRANSCRIPT, EventKind.READY] and events[0].payload["session_ref"] == "66666666-2222-4333-8444-555555555556"
    located = [e.payload["ref"] for e in events if e.kind is EventKind.TRANSCRIPT and e.payload["ref"]]
    assert located and located[0].endswith("/66666666-2222-4333-8444-555555555556/updates.jsonl")
    prompts = [e.payload["prompt"] for e in events if e.kind is EventKind.PROMPT_ACKNOWLEDGED]
    assert prompts == ["SPIKE-REPORT please", "SPIKE-BASH go", "SPIKE-SLOW work on it", "SPIKE-SLOW again", "SPIKE-QUESTION instead"]
    ends = [(e.kind, e.payload.get("last_message") or e.payload.get("via")) for e in events if e.kind in (EventKind.TURN_COMPLETED, EventKind.TURN_CANCELLED)]
    # The turn a "send now" superseded reports nothing; the shutdown's Stop after SessionEnd is no turn.
    assert ends == [(EventKind.TURN_COMPLETED, "done after tool"), (EventKind.TURN_COMPLETED, "done after tool"), (EventKind.TURN_CANCELLED, "user_interrupt"), (EventKind.TURN_COMPLETED, "I looked at it. Should I proceed?")]
    assert kinds[-1] is EventKind.SESSION_ENDED
    assert [e.payload["tool"] for e in events if e.kind is EventKind.TOOL_STARTED] == ["search_tool", "daedalus_team__Report", "run_terminal_command"]
    # The shell command asked (answered by Enter on the pre-selected "Yes, proceed"); the team tools did not.
    [asked] = [e for e in events if e.kind is EventKind.PERMISSION_REQUESTED]
    assert asked.payload["tool"] == "run_terminal_command"


async def test_a_real_permission_prompt_is_a_request_settled_by_the_tool_or_the_turn() -> None:
    events = await replay("hooks-permission.jsonl")
    asked = [e for e in events if e.kind is EventKind.PERMISSION_REQUESTED]
    assert len(asked) == 3 and all(e.native_id == "call_1" for e in asked)
    assert asked[0].payload["tool"] == "run_terminal_command" and asked[0].payload["summary"].startswith("rm -rf spike-dir && curl")
    order = [e.kind for e in events if e.kind in (EventKind.PERMISSION_REQUESTED, EventKind.REQUEST_RESOLVED, EventKind.TURN_COMPLETED, EventKind.TURN_CANCELLED)]
    # Allowed: the tool ran. Rejected: PermissionDenied, then the turn cancelled. Dismissed: the turn cancelled.
    assert order == [
        EventKind.PERMISSION_REQUESTED, EventKind.REQUEST_RESOLVED, EventKind.TURN_COMPLETED,
        EventKind.PERMISSION_REQUESTED, EventKind.REQUEST_RESOLVED, EventKind.TURN_CANCELLED,
        EventKind.PERMISSION_REQUESTED, EventKind.REQUEST_RESOLVED, EventKind.TURN_CANCELLED,
    ]
    assert [e.payload["via"] for e in events if e.kind is EventKind.TURN_CANCELLED] == ["permission_rejected", "permission_cancelled"]
    notes = [e for e in events if e.kind is EventKind.NOTIFICATION]
    assert notes == []  # every permission prompt became the request; nothing is left as a bare notification


def test_the_real_session_file_is_read_as_turns() -> None:
    usage = json.loads((RECORDED / "usage.json").read_text())["turns"]
    turns = parse_transcript((RECORDED / "updates.jsonl").read_text(encoding="utf-8"), usage)
    assert [t.role for t in turns[:2]] == ["user", "assistant"] and turns[0].text == "SPIKE-REPORT please"
    assert [tool.name for tool in turns[1].tools] == ["search_tool", "use_tool"] and turns[1].text == "done after tool"
    assert any(t.role == "system" and t.text == "[interrupted]" for t in turns)
    assert turns[1].usage is not None
    permission = parse_transcript((RECORDED / "updates-permission.jsonl").read_text(encoding="utf-8"))
    ran = [tool for t in permission for tool in t.tools]
    assert ran[0].summary.startswith("rm -rf spike-dir") and ran[0].ok is True


def test_sign_in_models_and_agents_are_read_from_what_grok_prints() -> None:
    assert parse_grok_login((RECORDED / "models-signed-in.txt").read_text()).state == "yes"
    assert parse_grok_login((RECORDED / "models-signed-in.txt").read_text()).detail == "grok.com"
    assert parse_grok_login((RECORDED / "models-signed-out.txt").read_text()).state == "no"
    assert (parse_grok_login((RECORDED / "models-api-key.txt").read_text()).state, parse_grok_login((RECORDED / "models-api-key.txt").read_text()).detail) == ("yes", "API key")
    assert parse_grok_models((RECORDED / "models-signed-in.txt").read_text()) == ("grok-4.7", "grok-4.7-build-fast", "grok-4.6", "grok-4.5")
    agents = json.loads((RECORDED / "inspect.json").read_text())["agents"]
    assert all(isinstance(a["source"], dict) for a in agents)  # "source": {"type": "builtin"}
    assert GrokTooling.cheap_markers[0] == "fast"


# -- whole sessions through the runtime -------------------------------------------------------------


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


async def test_a_grok_session_from_its_start_to_its_release(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=GrokAdapter()) as s:
        (s.home / ".claude").mkdir()
        (s.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}))
        ada = await started(s, "report:checkpoint:halfway;echo:the menu is written")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        assert row.transcript_ref.endswith(f"/{row.cli_session_id}/updates.jsonl") and (await s.terminals.get(row.terminal_id))["profile"] == "harness:grok"
        # The protocol reached Grok as its rules and the agent file's body, and led the first prompt.
        [prompt] = log(s, "system_prompt")
        assert prompt["rules"].startswith("You are Ada, a staff member of the project Bakery") and "# Working as a staff member" in prompt["text"]
        assert log(s, "submitted")[0]["text"].startswith("[team] You are Ada, staff of Bakery")
        assert s.runtime.channel(await s.team.live(row.id))["team_tools"] == "connected"  # type: ignore[arg-type]
        assert [m.state for m in await s.manager.staff.messages(ada.id)] == ["acknowledged"]
        reports = await s.events("staff.report", staff_id=ada.id)
        assert [(r.payload["kind"], bool(r.payload.get("implicit"))) for r in reports] == [("checkpoint", False)]
        assert not log(s, "claude_hooks_ran")  # the operator's own Claude hooks stay out of a staff session
        turns = await s.runtime.turns(await s.team.live(row.id))  # type: ignore[arg-type]
        assert turns[-1].text.endswith("the menu is written") and turns[-1].usage is not None
        assert await s.team.release(ada)
        assert (await s.terminals.get(row.terminal_id))["exit_code"] == 0


async def test_a_permission_is_answered_with_the_dialogs_digit(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=GrokAdapter()) as s:
        ada = await started(s, "perm:npm install grammy")
        await s.status_event(ada, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert ask.text == "run_terminal_command: npm install grammy"
        answered = await s.team.answer(ask.short_id, allow=True, by="operator")
        assert answered["delivered"] is True
        await s.status_event(ada, "turn_done_unseen")
        assert [(e["kind"], e["label"]) for e in log(s, "dialog_answered")] == [("permission", "Yes, proceed")]
        bo = await started(s, "perm:rm -rf dist", name="Bo")
        await s.status_event(bo, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert (await s.team.answer(ask.short_id, allow=False, by="operator"))["delivered"] is True
        # Rejecting ends Grok's turn: the member is idle again, with nothing left open.
        await s.status_event(bo, "idle")
        assert log(s, "dialog_answered")[-1]["label"] == "No, reject (type to add feedback)"
        assert await s.manager.asks.open_for(s.project.id) == []


async def test_a_permission_dismissed_in_the_terminal_settles_the_request(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=GrokAdapter()) as s:
        ada = await started(s, "perm:make build")
        await s.status_event(ada, "permission")
        row = await s.session_row(ada)
        assert (await s.terminals.wait_for(row.terminal_id, regex="Yes, proceed", timeout=10))["matched"] == "regex"
        await s.ptyd.type_as_human(row.terminal_id, "\x03")  # Ctrl+C dismisses the prompt, and the turn with it
        await s.status_event(ada, "idle")
        [resolved] = await s.events("permission.resolved", staff_id=ada.id)
        assert (resolved.payload["by"], resolved.payload["via"]) == ("operator", "terminal")

        async def settled() -> bool:
            return await s.manager.asks.open_for(s.project.id) == []

        await eventually(settled, "the request was settled by the turn's end")


async def test_a_message_for_now_interrupts_first_and_one_for_after_the_turn_waits(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=GrokAdapter()) as s:
        ada = await started(s, "slow:1000")
        await s.status_event(ada, "working")
        queued = await s.team.tell(ada, "echo:after the turn", when="after_turn", by="orchestrator")
        steered = await s.team.tell(ada, "echo:instead", when="now", by="orchestrator")
        # Enter while Grok is busy would only queue it, so now is an interrupt and then the message.
        assert steered["degraded_to"] == "interrupt"
        await message(s, steered["message_id"], "acknowledged")
        await message(s, queued["message_id"], "acknowledged")
        assert "idle" in await s.statuses(ada) or "turn_done_unseen" in await s.statuses(ada)
        assert log(s, "interrupt")[0]["key"] == "ctrl_c"
        texts = [e["text"] for e in log(s, "submitted")]
        assert texts.count("[orchestrator] echo:instead") == 1 and texts.count("[orchestrator] echo:after the turn") == 1
        assert not log(s, "queued")  # nothing was typed into the busy TUI


async def test_the_self_check_runs_one_session_through_the_agent_file(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=GrokAdapter()) as s:
        env = RuntimeEnvironment(s.terminals, "container", home=str(s.home))
        result = await session_check(GrokAdapter(), s.terminals, lambda: s.harness, env, "grok-code-fast", True)
        assert result.ok, result.steps
        assert [step.name for step in result.steps] == ["launch", "ready", "team", "deliver", "reply", "exit"]
        assert result.steps[5].detail == "exit code 0"
        quiet = await session_check(GrokAdapter(), s.terminals, lambda: s.harness, env, "", False)
        assert quiet.ok and [step.name for step in quiet.steps] == ["launch", "ready", "team", "exit"]
        assert await HarnessStore(db).open_launches() == []
