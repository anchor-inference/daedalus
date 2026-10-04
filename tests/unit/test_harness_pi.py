"""The pi adapter: its launch plan and bridge extension, its reading of pi's screens, bridge posts and
session file — checked against what pi 0.84.2 running the real bridge showed and posted
(``tests/support/fake_cli/recorded/pi``) — and whole sessions through the staff runtime against the
fake pi in a real terminal.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.harness import ADAPTERS
from daedalus.harness.contract import LAUNCH_DIR, EventKind, HookPost, Launch, LaunchSpec, ScreenClass, StaffEvent
from daedalus.harness.pi import BRIDGE_FILE, SKILL_FILE, PiAdapter, bridge_source, composer, parse_transcript
from daedalus.harness.runtime import RuntimeEnvironment
from daedalus.harness.selfcheck import session_check
from daedalus.harness.tools import PiTooling
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from tests.support.authorized_launch import operator_assignment
from tests.support.fake_cli.tui import read_log
from tests.unit.test_cli_staff_runtime import Stand, eventually, stand

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

RECORDED = Path(__file__).resolve().parents[1] / "support" / "fake_cli" / "recorded" / "pi"


def screen(name: str) -> str:
    return (RECORDED / "screens" / f"{name}.txt").read_text(encoding="utf-8")


def spec(**changes: Any) -> LaunchSpec:
    values: dict[str, Any] = {
        "harness": "pi", "env": "container", "cwd": "/work/bakery", "launch_id": "l1", "first_prompt": "[team] line\n\n[task t1] Menu",
        "title": "Ada · Menu page", "team_block": "You are Ada.", "team_skill": "---\nname: daedalus-team\n---\n", "model": "anthropic/claude-haiku-4", "effort": "low",
    }
    values.update(changes)
    return LaunchSpec(**values)


# -- the plan and the bridge ------------------------------------------------------------------------


def test_the_adapter_is_registered_and_its_plan_loads_the_bridge_from_the_launch() -> None:
    assert ADAPTERS["pi"] is PiAdapter
    plan = PiAdapter().launch_plan(spec())
    argv = list(plan.argv)
    assert argv[:5] == ["pi", "--session-id", plan.session_ref, "-e", f"{LAUNCH_DIR}/{BRIDGE_FILE}"]
    assert argv[argv.index("--append-system-prompt") + 1] == f"{LAUNCH_DIR}/system.md" and argv[argv.index("--skill") + 1] == f"{LAUNCH_DIR}/{SKILL_FILE}"
    assert argv[argv.index("--name") + 1] == "Ada · Menu page" and argv[argv.index("--model") + 1] == "anthropic/claude-haiku-4"
    assert argv[argv.index("--thinking") + 1] == "low" and argv[-1] == "[team] line\n\n[task t1] Menu"
    assert plan.files[BRIDGE_FILE] == bridge_source() and plan.files["system.md"] == b"You are Ada." and plan.files[SKILL_FILE].startswith(b"---\nname: daedalus-team")
    assert plan.env == {"DAEDALUS_ASK_HOLD_MS": "300000", "DAEDALUS_REPORT_HOLD_MS": "15000"}
    assert plan.hooks.sources == ("pi",) and plan.first_prompt_via == "argv"
    # pi has no agents and no permission modes; an effort it does not know is left out, not refused.
    bare = list(PiAdapter().launch_plan(spec(effort="ultra", agent="reviewer", team_block="", team_skill="", first_prompt=None)).argv)
    assert "--thinking" not in bare and "--append-system-prompt" not in bare and "--skill" not in bare and not any("reviewer" in a for a in bare)
    # A resume is the same launch with the old id: --session-id takes a session up when it exists.
    assert list(PiAdapter().resume_plan(spec(first_prompt=None), "11111111-1111-4111-8111-111111111111").argv[1:3]) == ["--session-id", "11111111-1111-4111-8111-111111111111"]


def test_the_bridge_speaks_what_the_adapter_reads() -> None:
    """Node is not run here: the bridge's source names every event the adapter maps and every
    operation it sends, and the recording shows the real bridge posting them."""
    source = bridge_source().decode()
    for event in ("ready", "input", "agent_start", "tool_start", "tool_end", "agent_end", "agent_settled", "session_end", "bridge_error"):
        assert f'"{event}"' in source, event
    for op in ("send", "abort", "state", "shutdown"):
        assert f'request.op === "{op}"' in source, op
    # The team contract of `ptyd team-mcp`: the same tool names, the held post, the call id, the texts.
    assert 'name: "Report"' in source and 'name: "AskOrchestrator"' in source and "wait_ms=" in source and "call_id" in source
    assert "Pending: nobody has answered yet." in source and "no longer connected to its team" in source
    assert 'join(DIAL_DIR, "pi.sock")' in source
    recorded = {json.loads(line)["body"].get("event") for line in (RECORDED / "bridge.jsonl").read_text().splitlines()}
    assert {"ready", "input", "agent_start", "tool_start", "tool_end", "agent_end", "agent_settled", "session_end"} <= recorded


# -- the recorded screens, posts and session ------------------------------------------------------


def test_the_readiness_gate_and_the_screen_reader_on_the_real_screens() -> None:
    adapter = PiAdapter()
    trust = adapter.readiness(screen("trust"))
    assert trust.action == "fail" and "bridge did not load" in trust.reason
    no_model = adapter.readiness(screen("no-model"))
    assert no_model.action == "fail" and "no provider is signed in" in no_model.reason
    assert adapter.readiness(screen("ready-after-first-turn")).action == "wait"
    assert adapter.classify_screen(screen("trust")) is ScreenClass.DIALOG
    assert adapter.classify_screen(screen("busy")) is ScreenClass.BUSY and adapter.classify_screen(screen("busy-with-steer")) is ScreenClass.BUSY
    for idle in ("ready-after-first-turn", "after-abort", "after-follow-up", "after-error"):
        assert adapter.classify_screen(screen(idle)) is ScreenClass.IDLE_COMPOSER, idle
    # An abort puts a steer that had not gone in back into the editor, unsent.
    assert composer(screen("after-abort")) == "[orchestrator] steer: focus on tests"
    assert composer(screen("ready-after-first-turn")) == ""
    assert adapter.composer_holds(screen("after-abort"), "[orchestrator] steer: focus on tests")


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
    launch = Launch("l1", "s1", "pi", "container", "t-replay", None, "/run/l1", "", "0.84.2", "")
    return [e async for e in PiAdapter().events(Replay(posts), launch)]  # type: ignore[arg-type]


async def test_the_real_bridge_posts_become_events() -> None:
    events = await replay("bridge.jsonl")
    kinds = [e.kind for e in events]
    assert kinds[:2] == [EventKind.TRANSCRIPT, EventKind.READY] and events[0].payload["session_ref"] == "33333333-2222-4333-8444-555555555555"
    assert events[0].payload["ref"].endswith("_33333333-2222-4333-8444-555555555555.jsonl")
    acks = [(e.payload["message_id"], e.payload["source"]) for e in events if e.kind is EventKind.PROMPT_ACKNOWLEDGED]
    assert acks == [("", "interactive"), ("m1", "extension"), ("m2", "extension"), ("m3", "extension"), ("m4", "extension"), ("m5", "extension"), ("m6", "extension")]
    ends = [e for e in events if e.kind in (EventKind.TURN_COMPLETED, EventKind.TURN_CANCELLED, EventKind.TURN_FAILED)]
    assert [e.kind for e in ends] == [EventKind.TURN_COMPLETED, EventKind.TURN_CANCELLED, EventKind.TURN_COMPLETED, EventKind.TURN_COMPLETED, EventKind.TURN_FAILED]
    # A follow-up sent while busy runs inside the same run: one end for m4 and m5, with m5's last words.
    assert ends[3].payload["last_message"] == "I looked at it. Should I proceed?" and "spike: invalid request" in ends[4].payload["failure"]
    assert kinds.count(EventKind.TURN_STARTED) == 5 and kinds[-1] is EventKind.SESSION_ENDED
    assert [e.payload["tool"] for e in events if e.kind is EventKind.TOOL_STARTED] == ["Report", "AskOrchestrator"]


async def test_pi_without_a_model_fails_every_prompt_instead_of_hanging() -> None:
    events = await replay("bridge-no-model.jsonl")
    kinds = [e.kind for e in events]
    assert kinds == [EventKind.TRANSCRIPT, EventKind.READY, EventKind.TURN_FAILED, EventKind.PROMPT_ACKNOWLEDGED, EventKind.TURN_FAILED, EventKind.SESSION_ENDED]
    assert "no provider is signed in" in events[2].payload["failure"]


def test_the_real_session_file_is_read_as_turns() -> None:
    turns = parse_transcript((RECORDED / "session.jsonl").read_text(encoding="utf-8"))
    assert [t.role for t in turns[:2]] == ["user", "assistant"] and turns[0].text == "[team] You are Ada. SPIKE-REPORT please"
    assert turns[1].tools[0].name == "Report" and turns[1].tools[0].summary == "spike report" and turns[1].tools[0].ok is True and turns[1].text == "done after tool"
    assert any(t.role == "system" and t.text == "[interrupted]" for t in turns)
    orchestrator = [t.text for t in turns if t.role == "orchestrator"]
    # The steer never went in (the abort came first); the follow-up did, as its own prompt.
    assert "[orchestrator] steer: focus on tests" not in orchestrator and "[orchestrator] SPIKE-QUESTION after" in orchestrator
    assert turns[-1].role == "assistant" or turns[-1].role == "orchestrator"


def test_signed_in_is_read_from_the_status_pi_reports() -> None:
    class Env:
        name = "container"
        home = "/home/someone"

        def __init__(self, stdout: str, code: int) -> None:
            self.stdout, self.code = stdout, code

        async def read(self, path: str, **_: Any) -> bytes:
            return b'{"defaultProvider": "anthropic"}'

        async def run(self, argv: list[str], **_: Any) -> Any:
            assert "--credentials" not in argv and "--no-refresh" in argv
            return type("R", (), {"stdout": self.stdout, "exit_code": self.code, "stderr": "", "timed_out": False, "path": ""})()

    ready = asyncio.run(PiTooling().login_state(Env('{"status":"ready","provider":"anthropic","authType":"oauth"}', 0)))  # type: ignore[arg-type]
    assert (ready.state, ready.detail) == ("yes", "anthropic · oauth")
    missing = asyncio.run(PiTooling().login_state(Env('{"status":"not_ready","provider":"anthropic","reason":"credentials_not_configured"}', 1)))  # type: ignore[arg-type]
    assert (missing.state, missing.detail) == ("no", "anthropic · credentials_not_configured")
    assert PiTooling.efforts[-1] == "max"


# -- whole sessions through the runtime -------------------------------------------------------------


def log(s: Stand, what: str) -> list[dict[str, Any]]:
    return [e for e in read_log(s.log) if e["event"] == what]


async def started(s: Stand, script: str, *, name: str = "Ada") -> Any:
    member = await s.hire(name)
    task_id = await s.task(f"Menu page;{script};")
    # Pi's built-in tools cannot enforce the project's ask or edits permission level.
    # These bridge lifecycle tests explicitly exercise an unrestricted Pi launch.
    s.team.permission_level = lambda _project: "all"
    assigned = await operator_assignment(s.team, member, task_id)
    assert assigned["state"] == "queued" and assigned["effect_id"]
    return member


async def message(s: Stand, message_id: str, state: str, *, timeout: float = 30.0) -> Any:
    async def reached() -> bool:
        found = await s.manager.staff.message(message_id)
        return found is not None and found.state == state

    await eventually(reached, f"message {message_id} {state}", timeout=timeout)
    return await s.manager.staff.message(message_id)


async def test_a_pi_session_from_its_start_to_its_release(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=PiAdapter()) as s:
        (s.work / ".pi").mkdir()
        (s.work / ".pi" / "settings.json").write_text("{}")  # a folder pi would ask to trust: the bridge answers
        ada = await started(s, "report:checkpoint:halfway;echo:the menu is written")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        assert row.transcript_ref.endswith(f"_{row.cli_session_id}.jsonl") and (await s.terminals.get(row.terminal_id))["profile"] == "harness:pi"
        # The protocol reached pi three ways: the appended system prompt, the skill, the first prompt.
        [appended] = log(s, "system_prompt")
        assert appended["text"].startswith("You are Ada, a staff member of the project Bakery")
        assert log(s, "skills")[0]["paths"][0].endswith(SKILL_FILE)
        assert log(s, "submitted")[0]["text"].startswith("[team] You are Ada, staff of Bakery")
        assert s.runtime.channel(await s.team.live(row.id))["team_tools"] == "connected"  # type: ignore[arg-type]
        assert [m.state for m in await s.manager.staff.messages(ada.id)] == ["acknowledged"]
        # The Report went through the bridge, held for the team's answer; the turn needed no implicit one.
        reports = await s.events("staff.report", staff_id=ada.id)
        assert [(r.payload["kind"], bool(r.payload.get("implicit"))) for r in reports] == [("checkpoint", False)]
        team_posts = [e["data"]["body"] for e in s.ptyd.events if e["type"] == "hook" and e["data"].get("name") == "team" and e["data"]["body"].get("tool") == "report"]
        assert team_posts[0]["call_id"].startswith(f"{(await HarnessStore(db).open_launch_for(row.id)).launch_id}:")  # type: ignore[union-attr]
        assert any(e["name"] == "tool_end" for e in log(s, "bridge_event"))
        turns = await s.runtime.turns(await s.team.live(row.id))  # type: ignore[arg-type]
        assert turns[-1].text.endswith("the menu is written") and turns[-1].usage is not None and turns[-1].usage.cost_usd
        assert await s.team.release(ada)
        # Asked to leave through the bridge, pi went by itself with its goodbye.
        assert (await s.terminals.get(row.terminal_id))["exit_code"] == 0
        assert log(s, "bridge_event")[-1]["name"] == "session_end"


async def test_messages_go_through_the_bridge_and_are_acknowledged_by_their_id(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=PiAdapter()) as s:
        ada = await started(s, "slow:1000")
        await s.status_event(ada, "working")
        queued = await s.team.tell(ada, "echo:after the turn", when="after_turn", by="orchestrator")
        steered = await s.team.tell(ada, "echo:steered in", when="now", by="orchestrator")
        assert steered["degraded_to"] is None
        # pi takes a steer into the running turn and says so at once, by the id it was sent with.
        await message(s, steered["message_id"], "acknowledged")
        await asyncio.sleep(0.5)
        assert (await s.manager.staff.message(queued["message_id"])).state == "queued"  # type: ignore[union-attr]
        await s.team.interrupt(ada)
        await s.status_event(ada, "idle")
        await message(s, queued["message_id"], "acknowledged")
        sent = [(e["text"], e["deliverAs"], e["via"]) for e in log(s, "submitted") if e.get("via") == "bridge"]
        assert sent == [("[orchestrator] echo:steered in", "steer", "bridge"), ("[orchestrator] echo:after the turn", None, "bridge")]
        # Nothing was typed into pi's terminal: the bridge carries every message.
        writes = await db.fetchall("SELECT COUNT(*) AS n FROM terminal_audit WHERE terminal_id = ? AND action = 'write'", ((await s.session_row(ada)).terminal_id,))
        assert writes[0]["n"] == 0


async def test_a_question_to_the_orchestrator_is_held_and_answered(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=PiAdapter()) as s:
        ada = await started(s, "askorch:Which oven?|left|right;echo:using the oven")
        await s.status_event(ada, "question")
        [ask] = await s.manager.asks.open_for(s.project.id)
        await s.team.answer(ask.short_id, text="the left one", by="orchestrator")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        turns = await s.runtime.turns(await s.team.live(row.id))  # type: ignore[arg-type]
        assert any("AskOrchestrator: the left one" in t.text for t in turns if t.role == "assistant"), turns


async def test_a_turn_ending_on_a_question_is_reported_as_needing_input(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=PiAdapter()) as s:
        ada = await started(s, "echo:Should I price it in euros or dollars?")
        await s.status_event(ada, "turn_done_unseen")
        [report] = await s.events("staff.report", staff_id=ada.id)
        assert (report.payload["kind"], report.payload["implicit"]) == ("needs_input", True) and "euros" in report.payload["text"]


async def test_pi_signed_out_is_an_error_not_a_turn_that_never_ends(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=PiAdapter(), extra_env={"FAKE_PI_LOGGED_IN": "0"}) as s:
        ada = await started(s, "echo:never")
        event = await s.status_event(ada, "error")
        assert "no provider is signed in" in str(event.payload.get("detail") or event.payload.get("waiting_for"))


async def test_the_self_check_runs_one_session_through_the_bridge(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=PiAdapter()) as s:
        env = RuntimeEnvironment(s.terminals, "container", home=str(s.home))
        result = await session_check(PiAdapter(), s.terminals, lambda: s.harness, env, "anthropic/claude-haiku-4", True)
        assert result.ok, result.steps
        assert [step.name for step in result.steps] == ["launch", "ready", "team", "deliver", "reply", "exit"]
        assert result.steps[3].detail.startswith("bridge: ") and result.steps[5].detail == "exit code 0"
        quiet = await session_check(PiAdapter(), s.terminals, lambda: s.harness, env, "", False)
        assert quiet.ok and [step.name for step in quiet.steps] == ["launch", "ready", "team", "exit"]
        assert await HarnessStore(db).open_launches() == []
        assert not re.search(r"credentials_printed", s.log.read_text() if s.log.exists() else "")
