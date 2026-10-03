"""The Claude Code adapter: its launch plan, its reading of Claude's screens, hooks and transcript —
checked against what the real Claude Code showed and posted (``tests/support/fake_cli/recorded``) —
and whole sessions through the staff runtime against the fake Claude in a real terminal, with the
delivery pipeline and the permission and question relay in the loop.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import urllib.error
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.harness import install as install_harness
from daedalus.extensions.questions import always_server
from daedalus.harness import ADAPTERS
from daedalus.harness.capabilities import CAPABILITIES
from daedalus.harness.claude import ClaudeCodeAdapter, composer, parse_transcript, unpasted
from daedalus.harness.contract import (
    LAUNCH_DIR,
    EventKind,
    HookPost,
    Launch,
    LaunchSpec,
    ScreenClass,
    StaffEvent,
    mcp_server,
    standing_rule,
)
from daedalus.harness.runtime import CliStaffRuntime, RuntimeEnvironment
from daedalus.harness.selfcheck import session_check
from daedalus.harness.team import SKILL_PATH, looks_like_question
from daedalus.staff_runtime import FakeStaffRuntime, ReadRequest
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from daedalus.stores.staff import StaffError
from tests.support.fake_cli.tui import read_log
from tests.unit.test_cli_staff_runtime import Stand, eventually, stand, terminals_service, trust

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

RECORDED = Path(__file__).resolve().parents[1] / "support" / "fake_cli" / "recorded" / "claude"


def screen(name: str) -> str:
    return (RECORDED / "screens" / f"{name}.txt").read_text(encoding="utf-8")


def spec(**changes: Any) -> LaunchSpec:
    values: dict[str, Any] = {
        "harness": "claude", "env": "container", "cwd": "/work/bakery", "launch_id": "l1", "first_prompt": "[team] line\n\n[task t1] Menu",
        "title": "Ada · Menu page", "team_block": "You are Ada.", "team_skill": "---\nname: daedalus-team\n---\n", "model": "haiku",
    }
    values.update(changes)
    return LaunchSpec(**values)


# -- the plan -------------------------------------------------------------------------------------


def test_the_adapter_is_registered_and_its_plan_puts_everything_in_the_launch() -> None:
    assert ADAPTERS["claude"] is ClaudeCodeAdapter
    plan = ClaudeCodeAdapter().launch_plan(spec())
    argv = list(plan.argv)
    # The prompt after "--": --mcp-config and --add-dir take every word after them otherwise.
    assert argv[-2:] == ["--", "[team] line\n\n[task t1] Menu"] and argv[-6:-2] == ["--mcp-config", f"{LAUNCH_DIR}/mcp.json", "--add-dir", LAUNCH_DIR]
    assert argv[1] == "--session-id" and argv[2] == plan.session_ref
    assert argv[argv.index("--permission-mode") + 1] == "manual" and argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--append-system-prompt-file") + 1] == f"{LAUNCH_DIR}/system.md" and argv[argv.index("--name") + 1] == "Ada · Menu page"
    assert plan.files["system.md"] == b"You are Ada." and plan.files[SKILL_PATH].startswith(b"---\nname: daedalus-team")
    settings = json.loads(plan.files["settings.json"])
    assert settings["permissions"]["allow"] == ["mcp__daedalus_team__Report", "mcp__daedalus_team__AskOrchestrator"]
    held = settings["hooks"]["PermissionRequest"][0]["hooks"][0]
    assert held["command"] == '"$DAEDALUS_PTYD_BIN" hook PermissionRequest --wait-ms 300000' and held["timeout"] > 300
    [plain, question] = settings["hooks"]["PreToolUse"]
    assert "matcher" not in plain and "--wait-ms" not in plain["hooks"][0]["command"]
    assert question["matcher"] == "AskUserQuestion" and "--wait-ms 300000" in question["hooks"][0]["command"]
    assert "skipDangerousModePermissionPrompt" not in settings
    mcp = json.loads(plan.files["mcp.json"])["mcpServers"]["daedalus_team"]
    assert mcp["command"] == "sh" and mcp["args"] == ["-c", 'exec "$DAEDALUS_PTYD_BIN" team-mcp']
    assert mcp["env"] == {"DAEDALUS_ASK_HOLD_MS": "300000", "DAEDALUS_REPORT_HOLD_MS": "15000"}
    # Claude's own timeout on a team call is above the longest hold, so it never cuts a question short.
    assert int(plan.env["MCP_TOOL_TIMEOUT"]) > 300_000


def test_an_always_leaves_a_rule_for_the_one_mcp_tool_or_its_whole_server_and_none_for_a_built_in_tool() -> None:
    tool = "mcp__daedalus_browser__BrowserNavigate"
    assert mcp_server(tool) == "daedalus_browser"
    assert standing_rule(tool, "tool") == tool and standing_rule(tool, "server") == "mcp__daedalus_browser"
    # The server is what lies between the first two "__", as Claude Code reads it; the tool may hold more.
    assert standing_rule("mcp__shop__cart__add", "server") == "mcp__shop" and standing_rule("mcp__shop__cart__add", "tool") == "mcp__shop__cart__add"
    # A built-in tool's only rule would be all of it (every command, every file): it gets none.
    for built_in in ("Bash", "Write", "WebFetch", "mcp__", "mcp__shop", "mcp__shop__", "mcpx__shop__cart"):
        assert standing_rule(built_in, "tool") is None and standing_rule(built_in, "server") is None and mcp_server(built_in) == "", built_in
    # The list offers the server-wide answer only for an MCP tool of a CLI that keeps rules.
    browser = SimpleNamespace(kind="permission", detail={"tool": tool})
    assert always_server(browser, CAPABILITIES["claude"]) == "daedalus_browser"  # type: ignore[arg-type]
    assert always_server(SimpleNamespace(kind="permission", detail={"tool": "Bash"}), CAPABILITIES["claude"]) == ""  # type: ignore[arg-type]
    assert always_server(SimpleNamespace(kind="question", detail={"tool": tool}), CAPABILITIES["claude"]) == ""  # type: ignore[arg-type]
    assert all(always_server(browser, CAPABILITIES[h]) == "" for h in CAPABILITIES if h != "claude")  # type: ignore[arg-type]


def test_the_standing_rules_go_into_the_launchs_allow_list_once() -> None:
    plan = ClaudeCodeAdapter().launch_plan(spec(allow_rules=("mcp__daedalus_browser", "mcp__daedalus_team__Report", "mcp__shop__cart_add")))
    allow = json.loads(plan.files["settings.json"])["permissions"]["allow"]
    assert allow == ["mcp__daedalus_team__Report", "mcp__daedalus_team__AskOrchestrator", "mcp__daedalus_browser", "mcp__shop__cart_add"]


def test_modes_holds_and_resume() -> None:
    adapter = ClaudeCodeAdapter()

    def mode(**changes: Any) -> str:
        argv = list(adapter.launch_plan(spec(**changes)).argv)
        return argv[argv.index("--permission-mode") + 1]

    assert mode(permission_mode="default") == "manual" and mode(permission_mode="plan") == "plan"
    assert mode(permission_level="edits") == "acceptEdits" and mode(permission_level="ask") == "manual"
    bypass = adapter.launch_plan(spec(permission_level="all"))
    assert json.loads(bypass.files["settings.json"])["skipDangerousModePermissionPrompt"] is True
    unheld = json.loads(adapter.launch_plan(spec(permission_hold_ms=0)).files["settings.json"])
    assert "--wait-ms" not in unheld["hooks"]["PermissionRequest"][0]["hooks"][0]["command"] and len(unheld["hooks"]["PreToolUse"]) == 1
    resumed = adapter.resume_plan(spec(first_prompt=None), "11111111-1111-4111-8111-111111111111")
    assert list(resumed.argv[1:3]) == ["--resume", "11111111-1111-4111-8111-111111111111"] and "--" not in resumed.argv


# -- the recorded screens -------------------------------------------------------------------------


def test_the_readiness_gate_reads_the_real_screens() -> None:
    adapter = ClaudeCodeAdapter()
    trust_screen = screen("trust")
    assert adapter.readiness(trust_screen).keys == ("Down",)  # "No, exit" is highlighted at first
    chosen = trust_screen.replace(" ❯ No, exit", "   No, exit").replace("   Yes, I trust this folder", " ❯ Yes, I trust this folder")
    step = adapter.readiness(chosen)
    assert (step.action, step.keys, step.reason) == ("keys", ("Enter",), "folder trust")
    assert adapter.readiness(screen("first-run-theme")).action == "fail"
    login = adapter.readiness(screen("first-run-login"))
    assert login.action == "fail" and "not signed in" in login.reason
    assert adapter.readiness(screen("ready-idle")).action == "wait"


def test_the_screen_is_classified_and_the_composer_found() -> None:
    adapter = ClaudeCodeAdapter()
    idle = screen("ready-idle")
    assert adapter.classify_screen(idle) is ScreenClass.IDLE_COMPOSER and composer(idle) == ""
    assert adapter.classify_screen(screen("permission-dialog")) is ScreenClass.DIALOG
    assert adapter.classify_screen(screen("trust")) is ScreenClass.DIALOG
    assert composer(screen("permission-dialog")) is None
    typed = idle.replace("\n❯\n", "\n❯ [orchestrator] please write the menu\n")
    assert adapter.composer_holds(typed, "[orchestrator] please write the menu") and not adapter.composer_holds(typed, "something else")
    pasted = idle.replace("\n❯\n", "\n❯ [Pasted text #3 +11 lines]\n")
    assert adapter.composer_holds(pasted, "a long message\n" * 12)


def test_the_real_transcript_is_read_as_turns() -> None:
    turns = parse_transcript((RECORDED / "transcript.jsonl").read_text(encoding="utf-8"))
    roles = [t.role for t in turns]
    assert roles[0] == "user" and "assistant" in roles and "system" in roles
    assert any(t.role == "system" and t.text.startswith("[Request interrupted by user") for t in turns)
    assert not any(t.text.startswith(("<task-notification>", "<command-name>")) for t in turns)
    queued = [t for t in turns if t.text == "Reply with the single word queued."]
    assert len(queued) == 1  # the queue's own records are not a second prompt
    bash = [tool for t in turns for tool in t.tools if tool.name == "Bash"]
    assert bash and bash[0].summary == "touch spike.txt" and bash[0].ok is True
    # Usage is counted once per message, however many records share it.
    records = [json.loads(line) for line in (RECORDED / "transcript.jsonl").read_text().splitlines()]
    by_message = {r["message"]["id"]: r["message"]["usage"] for r in records if r.get("type") == "assistant"}
    assert sum(t.usage.output_tokens for t in turns if t.usage) == sum(u["output_tokens"] for u in by_message.values())
    # The context's fill is the last call's whole prompt, cache included, not the turn's sum.
    last_turn = [t for t in turns if t.usage][-1]
    last_id = [r["message"]["id"] for r in records if r.get("type") == "assistant" and r["timestamp"] <= last_turn.ended_at][-1]
    last = by_message[last_id]
    assert last_turn.usage is not None and last_turn.usage.context_tokens == last["input_tokens"] + last.get("cache_creation_input_tokens", 0) + last.get("cache_read_input_tokens", 0) > 0
    assert last_turn.usage.context_window == 0  # Claude's transcript does not say it, and it is not guessed


# A brief of four lines as Claude Code 2.1.282 submitted it after collapsing its paste, byte for byte
# in shape: a blank line, the tag with its id, the text, the closing tag with the same id.
WRAPPED_BRIEF = (
    '\n\n<pasted_content id="777e">\n[orchestrator] Your previous task is closed. Here is your next one.\n\n'
    '[task 384042 · assigned by the orchestrator]\nObjective: take the password out of HANDOFF.md\n</pasted_content id="777e">\n'
)


def test_a_collapsed_paste_is_read_as_the_text_that_was_sent() -> None:
    sent = WRAPPED_BRIEF.split(">\n", 1)[1].rsplit("\n</pasted_content", 1)[0]
    assert unpasted(WRAPPED_BRIEF) == sent
    assert unpasted("check this:\n\n<pasted_content id=\"0a1b\">\nline one\nline two\n</pasted_content id=\"0a1b\">\n") == "check this:\n\nline one\nline two"
    assert unpasted("plain words") == "plain words"
    # The hook's prompt is what the delivery matches against, and the transcript is what the Feed shows.
    [event] = ClaudeCodeAdapter()._map(None, None, HookPost("UserPromptSubmit", {"prompt": WRAPPED_BRIEF}))  # type: ignore[arg-type]
    assert event.kind is EventKind.PROMPT_ACKNOWLEDGED and event.payload["prompt"] == sent
    record = {"type": "user", "timestamp": "2026-09-25T14:29:03.002Z", "message": {"role": "user", "content": WRAPPED_BRIEF}}
    [turn] = parse_transcript(json.dumps(record))
    assert (turn.role, turn.text) == ("orchestrator", sent)


class Replay:
    """A terminal port that replays recorded hook posts, for the adapter's reading of them."""

    def __init__(self, posts: list[HookPost]) -> None:
        self.posts = posts
        self.replies: list[tuple[str, Any]] = []

    @property
    def id(self) -> str:
        return "t-replay"

    @property
    def env(self) -> Any:
        return "container"

    async def hooks(self) -> AsyncIterator[HookPost]:
        for post in self.posts:
            yield post

    async def reply(self, reply_id: str, body: Any) -> bool:
        self.replies.append((reply_id, body))
        return True


async def test_the_real_hooks_become_events() -> None:
    posts = []
    for line in (RECORDED / "hooks.jsonl").read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if "name" in entry and entry["name"] != "team":
            posts.append(HookPost(entry["name"], entry["body"], reply_id=f"r{len(posts)}" if entry["name"] in ("PermissionRequest",) else None))
    adapter = ClaudeCodeAdapter()
    term = Replay(posts)
    events: list[StaffEvent] = [e async for e in adapter.events(term, Launch("l1", "s1", "claude", "container", "t-replay", None, "/run/l1", "", "2.1.282", ""))]  # type: ignore[arg-type]
    kinds = [e.kind for e in events]
    assert kinds[:2] == [EventKind.TRANSCRIPT, EventKind.READY] and events[0].payload["session_ref"] == "11111111-1111-4111-8111-111111111111"
    [permission, *_] = [e for e in events if e.kind is EventKind.PERMISSION_REQUESTED]
    # The request names no tool use; the PreToolUse before it does.
    started = next(e for e in events if e.kind is EventKind.TOOL_STARTED and e.payload["tool"] == "Bash")
    assert permission.native_id == started.native_id.strip() and permission.native_id.startswith("toolu_")
    assert permission.payload["summary"] == "touch spike.txt"
    resolved = [e for e in events if e.kind is EventKind.REQUEST_RESOLVED and e.native_id == permission.native_id]
    assert resolved, "the tool running settles the request"
    assert EventKind.QUESTION_ASKED not in kinds  # AskUserQuestion's unheld PreToolUse is not the question
    assert any(e.kind is EventKind.TURN_STARTED and e.payload.get("source") == "task-notification" for e in events)
    stops = [e for e in events if e.kind is EventKind.TURN_COMPLETED]
    assert stops[0].payload["last_message"] == "Done. Created `spike.txt`."
    assert kinds[-1] is EventKind.SESSION_ENDED
    notifications = [e for e in events if e.kind is EventKind.NOTIFICATION]
    assert notifications and all(e.payload["type"] == "permission_prompt" for e in notifications)


def test_a_question_to_the_terminal_is_recognised() -> None:
    assert looks_like_question("I wrote the draft.\nShould I also translate it?")
    assert looks_like_question("Done with the parser. Let me know which option you prefer.")
    assert not looks_like_question("Wrote menu.md; the question \"euros?\" is settled by the brief.")
    assert not looks_like_question("")


# -- whole sessions through the runtime -------------------------------------------------------------


def claude(**config: Any) -> dict[str, Any]:
    return {"adapter": ClaudeCodeAdapter(), **config}


def log(s: Stand, what: str) -> list[dict[str, Any]]:
    return [e for e in read_log(s.log) if e["event"] == what]


async def started(s: Stand, script: str, *, name: str = "Ada",
                  requirements: list[dict[str, str]] | None = None) -> Any:
    """A member assigned a task whose brief ends with the fake model's script."""
    member = await s.hire(name)
    # The fake model reads ";"-separated directives: closed off, the brief after them is not part of the last one.
    task_id = await s.task(f"Menu page;{script};", requirements=requirements)
    assert (await s.assign(member, task_id))["state"] == "queued"
    async def active() -> bool:
        return await s.team.live_of(member) is not None

    await eventually(active, "the queued worker launch was admitted")
    return member


async def completed_for_resume(s: Stand, member: Any) -> Any:
    """Close one reviewed attempt and observe its terminal exit before resuming its conversation."""
    first = await s.session_row(member)
    live = await s.team.live_of(member)
    assert live is not None
    await s.team.ingress.report(live, "done", "The first task is complete", call_id=f"result:{first.id}")
    assert await s.team.release(member)

    async def exited() -> bool:
        terminal = await s.terminals.get(first.terminal_id)
        return terminal is not None and terminal["exit_code"] is not None

    await eventually(exited, "the prior CLI terminal exited")
    return first


async def test_a_claude_session_from_its_trust_question_to_its_release(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        ada = await started(s, "echo:the menu is written")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        # The gate moved to the row that trusts the folder, then confirmed it: two keys, both audited.
        writes = await db.fetchall("SELECT detail_json FROM terminal_audit WHERE terminal_id = ? AND action = 'write'", (row.terminal_id,))
        assert [json.loads(w["detail_json"]).get("note") for w in writes] == ["readiness: folder trust: to the row that trusts it", "readiness: folder trust"]
        # The protocol reached the CLI three ways: its system prompt, its skill, its first prompt.
        [appended] = log(s, "system_prompt")
        assert appended["text"].startswith("You are Ada, a staff member of the project Bakery") and "Report" in appended["text"]
        assert log(s, "skills")[0]["names"] == ["daedalus-team"]
        submitted = log(s, "submitted")[0]["text"]
        assert submitted.startswith("[team] You are Ada, staff of Bakery") and "[task " in submitted
        # The team tools said they loaded; the first message was acknowledged by the prompt's hook.
        assert s.runtime.channel(await s.team.live(row.id))["team_tools"] == "connected"  # type: ignore[arg-type]
        assert [m.state for m in await s.manager.staff.messages(ada.id)] == ["acknowledged"]
        # The turn ended without a Report: the orchestrator hears of it anyway, from its last words.
        [report] = await s.events("staff.report", staff_id=ada.id)
        assert report.payload["kind"] == "turn_done" and report.payload["implicit"] is True and "the menu is written" in report.payload["text"]
        assert "no report" in report.payload["text"]
        assert row.transcript_ref.endswith(f"{row.cli_session_id}.jsonl")
        last = await s.runtime.read(await s.team.live(row.id), ReadRequest("last"))  # type: ignore[arg-type]
        assert last.text == "the menu is written"
        assert await s.team.release(ada)
        assert (await s.terminals.get(row.terminal_id))["exit_code"] == 0
        assert [e["name"] for e in read_log(s.log) if e["event"] == "hook"][-1] == "SessionEnd"


async def test_a_turn_ending_on_a_question_is_routed_as_needing_input_and_a_reported_turn_is_not_repeated(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:Should I price it in euros or dollars?")
        await s.status_event(ada, "turn_done_unseen")

        async def reported() -> bool:
            return bool(await s.events("staff.report", staff_id=ada.id))

        # The turn's status is published before its implicit report, in the same step: under load
        # the test once read the reports between the two and found none.
        await eventually(reported, "the implicit report")
        [report] = await s.events("staff.report", staff_id=ada.id)
        assert (report.payload["kind"], report.payload["implicit"]) == ("needs_input", True)
        bo = await started(s, "report:checkpoint:menu drafted;echo:done for now", name="Bo")
        await s.status_event(bo, "turn_done_unseen")
        reports = await s.events("staff.report", staff_id=bo.id)
        assert [(r.payload["kind"], bool(r.payload.get("implicit"))) for r in reports] == [("checkpoint", False)]


async def test_a_permission_is_answered_through_its_held_hook(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "perm:npm install grammy")
        await s.status_event(ada, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert ask.request_ref.startswith("toolu_") and ask.text == "Bash: npm install grammy"
        answered = await s.team.answer(ask.short_id, allow=True, by="operator")
        assert answered["delivered"] is True
        await s.status_event(ada, "turn_done_unseen")
        assert await s.statuses(ada) == ["starting", "working", "permission", "working", "turn_done_unseen"]
        row = await s.session_row(ada)
        screen = "\n".join((await s.terminals.read_screen(row.terminal_id, scrollback=100))["lines"])
        assert "Allowed by PermissionRequest hook" in screen and "Ran npm install grammy." in screen
        # Answered by the held hook's reply: not one key was typed into the dialog.
        writes = await db.fetchall("SELECT detail_json FROM terminal_audit WHERE terminal_id = ? AND action = 'write'", (row.terminal_id,))
        assert all("readiness" in json.loads(w["detail_json"]).get("note", "") or "message" in json.loads(w["detail_json"]).get("note", "") or "submit" in json.loads(w["detail_json"]).get("note", "") for w in writes)
        assert not log(s, "dialog_answered")
        decision = next(r["body"] for r in s.ptyd.replies if isinstance(r.get("body"), dict) and "hookSpecificOutput" in r["body"])
        assert decision["hookSpecificOutput"]["decision"] == {"behavior": "allow"}


async def test_without_a_hold_the_answer_waits_for_the_dialog_before_its_key(settings: Settings, db: Database) -> None:
    # The hook comes well before the dialog here: a key typed at once would land in the composer.
    async with stand(settings, db, extra_env={"FAKE_CLAUDE_DIALOG_DELAY_MS": "1500"}, permission_hold_s=0, **claude()) as s:
        trust(s)
        ada = await started(s, "perm:make build")
        await s.status_event(ada, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        answered = await s.team.answer(ask.short_id, allow=False, by="operator")
        assert answered["delivered"] is True
        await s.status_event(ada, "turn_done_unseen")
        assert [(e["kind"], e["label"]) for e in log(s, "dialog_answered")] == [("permission", "No")]
        assert not [e for e in log(s, "submitted") if e["text"].strip() in ("1", "3")]
        row = await s.session_row(ada)
        screen = "\n".join((await s.terminals.read_screen(row.terminal_id, scrollback=100))["lines"])
        assert "I did not run make build" in screen


async def test_always_is_the_dialogs_do_not_ask_again_row(settings: Settings, db: Database) -> None:
    async with stand(settings, db, permission_hold_s=0, **claude()) as s:
        trust(s)
        ada = await started(s, "perm:make build")
        await s.status_event(ada, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        answered = await s.team.answer(ask.short_id, allow=True, always=True, by="operator")
        assert answered["delivered"] is True and answered["ask"]["resolution"]["always"] is True
        await s.status_event(ada, "turn_done_unseen")
        [(kind, label)] = [(e["kind"], e["label"]) for e in log(s, "dialog_answered")]
        assert kind == "permission" and label.startswith("Yes, and don't ask again"), label


async def test_always_for_a_whole_mcp_server_is_held_by_the_session_and_kept_for_the_next_launch(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "mcpperm:daedalus_browser:BrowserNavigate;mcpperm:daedalus_browser:BrowserClick;mcpperm:shop:CartAdd;echo:done")
        await s.status_event(ada, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert ask.detail["tool"] == "mcp__daedalus_browser__BrowserNavigate"
        answered = await s.team.answer(ask.short_id, allow=True, always=True, server=True, by="operator")
        assert answered["delivered"] is True and answered["ask"]["resolution"]["rule"] == "mcp__daedalus_browser"
        decision = next(r["body"] for r in s.ptyd.replies if isinstance(r.get("body"), dict) and "hookSpecificOutput" in r["body"])["hookSpecificOutput"]["decision"]
        # The session holds the rule; the project folder, where "localSettings" would write it, is left alone.
        assert decision == {"behavior": "allow", "updatedPermissions": [{"type": "addRules", "rules": [{"toolName": "mcp__daedalus_browser"}], "behavior": "allow", "destination": "session"}]}
        [kept] = await s.manager.staff.allow_rules(ada.id)
        assert (kept["rule"], kept["created_by"]) == ("mcp__daedalus_browser", "operator") and kept["created_at"]

        # Another tool of the same server runs unasked; another server's tool still asks.
        async def next_ask() -> bool:
            return len(await s.manager.asks.open_for(s.project.id)) == 1

        await eventually(next_ask, "the other server's permission")
        [other] = await s.manager.asks.open_for(s.project.id)
        assert other.detail["tool"] == "mcp__shop__CartAdd"
        # Only the one tool this time: its rule names it, not its server.
        assert (await s.team.answer(other.short_id, allow=True, always=True, by="operator"))["ask"]["resolution"]["rule"] == "mcp__shop__CartAdd"
        await s.status_event(ada, "turn_done_unseen")
        asked = [(e["tool"], e["decision"]) for e in log(s, "mcp_permission")]
        assert asked == [("mcp__daedalus_browser__BrowserNavigate", "allow_once"), ("mcp__daedalus_browser__BrowserClick", "allow_once"), ("mcp__shop__CartAdd", "allow_once")]
        assert [r["rule"] for r in await s.manager.staff.allow_rules(ada.id)] == ["mcp__daedalus_browser", "mcp__shop__CartAdd"]
        # A revoked grant leaves the list the next launch reads; one never given is not there to revoke.
        assert await s.manager.staff.revoke_rule(ada.id, "mcp__shop__CartAdd") is True
        assert await s.manager.staff.revoke_rule(ada.id, "mcp__shop__CartAdd") is False
        assert [r["rule"] for r in await s.manager.staff.allow_rules(ada.id)] == ["mcp__daedalus_browser"]


async def test_a_built_in_tools_always_keeps_no_rule_and_cannot_be_given_for_a_server(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "perm:npm install grammy")
        await s.status_event(ada, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        with pytest.raises(StaffError, match="not for a tool of an MCP server"):
            await s.team.answer(ask.short_id, allow=True, always=True, server=True, by="operator")
        assert (await s.manager.asks.get(ask.id)).open, "a refused shape answers nothing"  # type: ignore[union-attr]
        answered = await s.team.answer(ask.short_id, allow=True, always=True, by="operator")
        assert answered["delivered"] is True and "rule" not in answered["ask"]["resolution"]
        decision = next(r["body"] for r in s.ptyd.replies if isinstance(r.get("body"), dict) and "hookSpecificOutput" in r["body"])["hookSpecificOutput"]["decision"]
        assert decision == {"behavior": "allow"}
        assert await s.manager.staff.allow_rules(ada.id) == []


async def test_a_permission_answered_in_the_terminal_lets_its_held_hook_go(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "perm:rm -rf build")
        await s.status_event(ada, "permission")
        row = await s.session_row(ada)
        assert (await s.terminals.wait_for(row.terminal_id, regex="Do you want to proceed", timeout=10))["matched"] == "regex"
        await s.ptyd.type_as_human(row.terminal_id, "1")
        await s.status_event(ada, "turn_done_unseen")
        [resolved] = await s.events("permission.resolved", staff_id=ada.id)
        assert (resolved.payload["by"], resolved.payload["via"]) == ("operator", "terminal")
        # The hook still held at the listener is let go with an empty answer, not left to its timeout.
        assert any(r.get("body") in (None, "") for r in s.ptyd.replies)
        assert await s.manager.asks.open_for(s.project.id) == []


async def test_a_question_of_claudes_own_is_answered_without_its_dialog(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "ask:Tea or coffee?|Tea|Coffee")
        await s.status_event(ada, "question")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert ask.kind == "question" and ask.text == "Tea or coffee?" and ask.detail["options"] == ["Tea", "Coffee"]
        await s.team.answer(ask.short_id, selected=["Coffee"], by="orchestrator")
        await s.status_event(ada, "turn_done_unseen")
        assert not [e for e in log(s, "dialog_opened") if e["kind"] == "question"]
        row = await s.session_row(ada)
        assert "You chose: Coffee" in "\n".join((await s.terminals.read_screen(row.terminal_id, scrollback=100))["lines"])


# -- delivery ---------------------------------------------------------------------------------------


async def message(s: Stand, message_id: str, state: str, *, timeout: float = 30.0) -> Any:
    async def reached() -> bool:
        found = await s.manager.staff.message(message_id)
        return found is not None and found.state == state

    await eventually(reached, f"message {message_id} {state}", timeout=timeout)
    return await s.manager.staff.message(message_id)


async def test_a_message_for_after_the_turn_waits_for_it_and_one_for_now_goes_into_it(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "slow:1000")
        await s.status_event(ada, "working")
        queued = await s.team.tell(ada, "echo:after the turn", when="after_turn", by="orchestrator")
        steered = await s.team.tell(ada, "echo:steered in", when="now", by="orchestrator")
        assert (queued["state"], steered["state"], steered["degraded_to"]) == ("queued", "queued", None)
        # The message for now goes into the running turn (Claude queues it and says so at once); the
        # other waits behind it for the turn to end, and nothing ends the turn but an Esc.
        await message(s, steered["message_id"], "acknowledged")
        await asyncio.sleep(0.5)
        assert (await s.manager.staff.message(queued["message_id"])).state == "queued"  # type: ignore[union-attr]
        await s.team.interrupt(ada)
        await message(s, queued["message_id"], "acknowledged")
        texts = [e["text"] for e in log(s, "submitted")]
        assert texts.count("[orchestrator] echo:steered in") == 1 and texts.count("[orchestrator] echo:after the turn") == 1
        assert "idle" in await s.statuses(ada)  # the interrupt was seen on screen


async def test_the_orchestrators_message_is_typed_into_the_busy_turn_at_once_and_acknowledged_within_it(settings: Settings, db: Database) -> None:
    """What the operator saw fail: an orchestrator's correction to a Claude Code member at work reached
    it only when the turn had ended, minutes late. Without a timing it now goes in at once."""
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "slow:1000")
        await s.status_event(ada, "working")
        told = await s.team.tell(ada, "echo:use the owner's sheet", by="orchestrator")
        assert (told["state"], told["degraded_to"]) == ("queued", None)
        await message(s, told["message_id"], "acknowledged", timeout=15)
        # Acknowledged by the prompt hook Claude fires as it queues a message in a busy TUI, while
        # the turn it is meant for still runs: nothing ended it.
        assert (await s.session_row(ada)).status == "working"
        [typed] = [e for e in log(s, "submitted") if e["text"] == "[orchestrator] echo:use the owner's sheet"]
        assert typed["busy"] is True
        assert (await s.manager.staff.message(told["message_id"])).mode == "now"  # type: ignore[union-attr]
        delivery = await HarnessStore(db).delivery(told["message_id"])
        assert delivery is not None and (delivery.via, delivery.degraded_to, delivery.acknowledged_at is not None) == ("paste", "", True)


async def test_messages_never_merge_and_a_swallowed_enter_is_recovered_once(settings: Settings, db: Database) -> None:
    async with stand(settings, db, extra_env={"FAKE_CLI_FAULTS": "swallow_enter_once"}, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        one, two = await asyncio.gather(s.team.tell(ada, "echo:one", by="operator"), s.team.tell(ada, "echo:two", by="operator"))
        await message(s, one["message_id"], "acknowledged")
        await message(s, two["message_id"], "acknowledged")
        texts = [e["text"] for e in log(s, "submitted")][1:]
        assert sorted(texts) == ["echo:one", "echo:two"]  # one submission each, never "echo:oneecho:two"
        assert [e["reason"] for e in log(s, "enter_swallowed")] == ["fault"]
        deliveries = await HarnessStore(db).deliveries([one["message_id"], two["message_id"]])
        assert sorted(d.enters for d in deliveries.values()) == [1, 2]


async def test_a_long_message_goes_as_a_file_in_the_launch(settings: Settings, db: Database) -> None:
    async with stand(settings, db, pointer_threshold_bytes=1024, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        long = "echo:the long one " + "x" * 2000
        told = await s.team.tell(ada, long, by="operator")
        await message(s, told["message_id"], "acknowledged")
        [pointer] = [e["text"] for e in log(s, "submitted") if e["text"].startswith("Read the message in")]
        path = Path(pointer.removeprefix("Read the message in ").removesuffix(" and act on it."))
        assert path.read_text() == long and path.name == f"message-{told['message_id']}.md"
        launch = await HarnessStore(db).open_launch_for((await s.session_row(ada)).id)
        assert launch is not None and path.parent == Path(launch.launch_dir)
        assert (await HarnessStore(db).delivery(told["message_id"])).via == "pointer"  # type: ignore[union-attr]


async def test_a_dialog_that_takes_a_paste_gets_no_enter_and_the_message_waits(settings: Settings, db: Database) -> None:
    async with stand(settings, db, extra_env={"FAKE_CLI_FAULTS": "dialog_during_paste"}, answer_confirm_s=1.0, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        told = await s.team.tell(ada, "echo:after the notice", by="orchestrator")
        await s.terminals.wait_for(row.terminal_id, regex="a notice opened while you were pasting", timeout=10)
        await asyncio.sleep(1.5)
        assert not log(s, "enter_into_dialog")
        await s.ptyd.type_as_human(row.terminal_id, "\x1b")  # the person dismisses the notice
        await message(s, told["message_id"], "acknowledged")
        assert not log(s, "enter_into_dialog")
        assert [e["text"] for e in log(s, "submitted")].count("[orchestrator] echo:after the notice") == 1


async def test_a_person_typing_holds_the_orchestrators_message_but_not_the_operators(settings: Settings, db: Database) -> None:
    async with stand(settings, db, input_idle_ms=1500, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)

        async def typing(seconds: float) -> None:
            for _ in range(int(seconds / 0.25)):
                await s.ptyd.type_as_human(row.terminal_id, "\x1b[A")  # an arrow: holds the keyboard, types nothing
                await asyncio.sleep(0.25)

        # The operator's own message is the person at the keyboard: it does not wait for them.
        person = asyncio.create_task(typing(4))
        await asyncio.sleep(0.3)
        operators = await s.team.tell(ada, "echo:from the operator", by="operator")
        await message(s, operators["message_id"], "acknowledged")
        assert not person.done()
        await person
        await s.status_event(ada, "turn_done_unseen")
        # Anyone else's words wait while a person types, and go once they have stopped.
        person = asyncio.create_task(typing(3))
        await asyncio.sleep(0.3)
        orchestrators = await s.team.tell(ada, "echo:from the orchestrator", by="orchestrator")
        await asyncio.sleep(1.5)
        assert (await s.manager.staff.message(orchestrators["message_id"])).state == "queued"  # type: ignore[union-attr]
        await person
        await message(s, orchestrators["message_id"], "acknowledged")


async def test_after_a_restart_a_submitted_message_found_in_the_transcript_is_not_sent_again(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        told = await s.team.tell(ada, "echo:once only", by="operator")
        await message(s, told["message_id"], "acknowledged")
        await s.status_event(ada, "turn_done_unseen")
        # The host went away after the CLI took the message and before it heard so.
        await db.execute("UPDATE staff_messages SET state = 'submitted' WHERE id = ?", (told["message_id"],))
        pending = await s.manager.staff.add_message(ada.id, "echo:still to go", origin="operator", mode="after_turn", staff_session_id=(await s.session_row(ada)).id)
        runtime = s.restart_runtime(ClaudeCodeAdapter())
        assert await runtime.reconcile(wait=5) == 1
        await message(s, told["message_id"], "acknowledged")
        await message(s, pending.id, "acknowledged")
        texts = [e["text"] for e in log(s, "submitted")]
        assert texts.count("echo:once only") == 1 and texts.count("echo:still to go") == 1


# -- the team channel --------------------------------------------------------------------------------


def post(s: Stand, launch_id: str, name: str, body: dict[str, Any], *, wait_ms: int = 0) -> tuple[int, Any]:
    launch = s.ptyd.launches[launch_id]
    url = f"http://127.0.0.1:{s.ptyd.hook_port}/hook/{launch_id}/{name}" + (f"?wait_ms={wait_ms}" if wait_ms else "")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={"Authorization": f"Bearer {launch.token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        return exc.code, None


async def test_a_team_call_seen_twice_is_acted_on_once(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        launch = await HarnessStore(db).open_launch_for((await s.session_row(ada)).id)
        assert launch is not None
        body = {"tool": "report", "kind": "checkpoint", "note": "halfway", "artifacts": [], "call_id": f"{launch.launch_id}:cafe:1"}
        first = await asyncio.to_thread(post, s, launch.launch_id, "team", body, wait_ms=5000)
        again = await asyncio.to_thread(post, s, launch.launch_id, "team", body, wait_ms=5000)
        assert first == again and first[0] == 200
        assert first[1] is not None and first[1]["text"].startswith("reported checkpoint; report ")
        assert [r.payload["kind"] for r in await s.events("staff.report", staff_id=ada.id) if not r.payload.get("implicit")] == ["checkpoint"]
        # The same question asked twice while the first is open is one request, answered to the newest post.
        ask = {"tool": "ask", "question": "Which oven?", "options": ["left", "right"]}
        asks = [asyncio.create_task(asyncio.to_thread(post, s, launch.launch_id, "team", {**ask, "call_id": f"{launch.launch_id}:cafe:{n}"}, wait_ms=20_000)) for n in (2, 3)]
        await s.status_event(ada, "question")

        async def both_held() -> bool:
            return sum(1 for e in s.ptyd.events if e["type"] == "hook" and e["data"].get("name") == "team" and e["data"]["body"].get("tool") == "ask") == 2

        await eventually(both_held, "both questions reached the host")
        await asyncio.sleep(0.3)
        [open_ask] = await s.manager.asks.open_for(s.project.id)
        await s.team.answer(open_ask.short_id, text="the left one", by="orchestrator")
        answers = sorted([await t for t in asks], key=lambda r: str(r[1]))
        assert answers[-1] == (200, {"text": "the left one"})


async def test_a_team_report_carries_the_members_confirmations_and_evidence_to_the_host(settings: Settings, db: Database) -> None:
    """A command-line member confirms a requirement and gives evidence through its team tool; both
    reach the report as a Daedalus member's do, rather than being dropped on the way."""
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready", requirements=[{
            "text": "English only for now", "kind": "scope", "source": "operator"}])
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        launch = await HarnessStore(db).open_launch_for(row.id)
        assert launch is not None and row.task_id
        body = {"tool": "report", "kind": "checkpoint", "note": "noted", "artifacts": [], "acknowledged": ["R1", "R7"], "call_id": f"{launch.launch_id}:cafe:1"}
        status, said = await asyncio.to_thread(post, s, launch.launch_id, "team", body, wait_ms=5000)
        assert status == 200 and said is not None and "confirmed R1" in said["text"] and "requirements not confirmed: R7" in said["text"]
        [report] = [e for e in await s.events("staff.report", staff_id=ada.id) if not e.payload.get("implicit")]
        assert report.payload["acknowledged"] == ["R1"]


async def test_a_cli_whose_team_tools_never_load_is_shown_so(settings: Settings, db: Database) -> None:
    async with stand(settings, db, extra_env={"FAKE_TEAM_MCP_SILENT": "1"}, team_hello_s=0.5, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")

        async def warned() -> bool:
            return bool(await s.events("staff.channel", staff_id=ada.id))

        await eventually(warned, "the missing team tools were reported")
        [event] = await s.events("staff.channel", staff_id=ada.id)
        assert event.payload["team_tools"] == "missing" and "no team tools" in event.payload["detail"]
        assert s.runtime.channel(await s.team.live((await s.session_row(ada)).id))["team_tools"] == "missing"  # type: ignore[arg-type]


async def test_a_real_team_call_proves_the_tools_that_never_said_hello(settings: Settings, db: Database) -> None:
    async with stand(settings, db, extra_env={"FAKE_TEAM_MCP_SILENT": "1"}, team_hello_s=0.5, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:ready")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)

        async def warned() -> bool:
            return bool(await s.events("staff.channel", staff_id=ada.id))

        await eventually(warned, "the missing team tools were reported")
        launch = await HarnessStore(db).open_launch_for(row.id)
        assert launch is not None
        body = {"tool": "report", "kind": "checkpoint", "note": "halfway", "artifacts": [], "call_id": f"{launch.launch_id}:cafe:1"}
        status, reply = await asyncio.to_thread(post, s, launch.launch_id, "team", body, wait_ms=5000)
        assert status == 200 and reply is not None and reply["text"].startswith("reported checkpoint; report ")
        live = await s.team.live(row.id)
        assert s.runtime.channel(live)["team_tools"] == "connected"  # type: ignore[arg-type]
        assert [e.payload["team_tools"] for e in await s.events("staff.channel", staff_id=ada.id)] == ["missing", "connected"]
        health = await s.team.health(live)  # type: ignore[arg-type]
        assert health.team_tools == "connected" and "team_tools_missing" not in health.problems


async def test_after_a_restart_the_team_tools_are_not_called_missing_for_want_of_a_second_hello(settings: Settings, db: Database) -> None:
    """The tools say hello once, when the CLI starts. A host that takes the launch up after a restart
    never hears it; it took the silence for missing tools thirty seconds later, and said so on the
    card of a member that went on calling them. Its reports on record are the proof instead, and
    without one the next call is."""
    async with stand(settings, db, team_hello_s=0.5, **claude()) as s:
        trust(s)
        ada, bea = await started(s, "echo:ready"), await started(s, "echo:ready", name="Bea")
        for member in (ada, bea):
            await s.status_event(member, "turn_done_unseen")
        ada_row, bea_row = await s.session_row(ada), await s.session_row(bea)
        ada_launch, bea_launch = await HarnessStore(db).open_launch_for(ada_row.id), await HarnessStore(db).open_launch_for(bea_row.id)
        assert ada_launch is not None and bea_launch is not None
        body = {"tool": "report", "kind": "checkpoint", "note": "halfway", "artifacts": [], "call_id": f"{ada_launch.launch_id}:cafe:1"}
        assert (await asyncio.to_thread(post, s, ada_launch.launch_id, "team", body, wait_ms=5000))[0] == 200

        runtime = s.restart_runtime(ClaudeCodeAdapter())
        assert await runtime.reconcile(wait=5) == 2
        # A message after the restart is what made the session "ready" again, and started the clock.
        for member in (ada, bea):
            told = await s.team.tell(member, "echo:after the restart", by="operator")
            await message(s, told["message_id"], "acknowledged")
        await asyncio.sleep(1.5)  # three times the hello's allowance
        assert not await s.events("staff.channel", staff_id=ada.id) and not await s.events("staff.channel", staff_id=bea.id)
        ada_channel = runtime.channel(await s.team.live(ada_row.id))  # type: ignore[arg-type]
        assert ada_channel["team_tools"] == "connected" and ada_channel["last_team_call_at"]
        assert runtime.channel(await s.team.live(bea_row.id))["team_tools"] == "waiting"  # type: ignore[arg-type]
        # Bea's next call settles it.
        body = {"tool": "report", "kind": "checkpoint", "note": "still here", "artifacts": [], "call_id": f"{bea_launch.launch_id}:beef:1"}
        assert (await asyncio.to_thread(post, s, bea_launch.launch_id, "team", body, wait_ms=5000))[0] == 200
        assert runtime.channel(await s.team.live(bea_row.id))["team_tools"] == "connected"  # type: ignore[arg-type]


# -- the team's side of a command-line member ---------------------------------------------------------


async def test_a_pause_asked_for_during_a_turn_takes_effect_when_it_ends(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "slow:40")
        await s.status_event(ada, "working")
        assert (await s.team.pause(ada))["paused"] is False
        await s.status_event(ada, "turn_done_unseen")

        async def settled() -> bool:
            last = (await s.events("staff.status", staff_id=ada.id))[-1].payload
            return last["status"] == "idle" and "paused" in str(last.get("detail"))

        await eventually(settled, "the pause took effect at the turn's end")
        assert (await s.team.queue._free(SimpleNamespace(staff_id=ada.id, staff_name="Ada", task_id="other"))).endswith("a message or a new assignment resumes it")  # type: ignore[arg-type]


async def test_the_next_task_goes_into_the_idle_session_as_its_next_message(settings: Settings, db: Database) -> None:
    """An explicit resume keeps the CLI conversation after an exact result and physical exit."""
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:the scouting is done")
        await s.status_event(ada, "turn_done_unseen")
        first = await completed_for_resume(s, ada)

        next_id = await s.task("Second page;echo:the second page is done;")
        assert (await s.assign(ada, next_id, resume_from=first.id))["state"] == "queued"

        async def resumed() -> bool:
            row = await s.manager.staff.live(ada.id)
            return (row is not None and row.id != first.id and row.task_id == next_id
                    and row.cli_session_id == first.cli_session_id and bool(row.terminal_id))

        await eventually(resumed, "the selected CLI conversation resumed for the next task")
        row = await s.session_row(ada)
        assert row.id != first.id and row.terminal_id != first.terminal_id
        assert row.cli_session_id == first.cli_session_id
        attempts = await db.fetchall("SELECT id,task_id FROM execution_attempts WHERE task_id IN (?,?)",
                                     (first.task_id, next_id))
        assert len(attempts) == 2 and attempts[0]["id"] != attempts[1]["id"]
        assert (await s.team.task(next_id)).status == "doing"  # type: ignore[union-attr]


async def test_a_next_task_brief_claude_collapsed_is_acknowledged_by_its_hook(settings: Settings, db: Database) -> None:
    """A resumed CLI receives a fresh task brief and its hook acknowledges the delivery."""
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:the scouting is done")
        await s.status_event(ada, "turn_done_unseen")
        first = await completed_for_resume(s, ada)
        next_id = await s.task("Second page;echo:the second page is done;")
        assert (await s.assign(ada, next_id, resume_from=first.id))["state"] == "queued"

        async def new_session() -> bool:
            current = await s.manager.staff.live(ada.id)
            return current is not None and current.id != first.id and current.task_id == next_id

        await eventually(new_session, "the new task's resumed session")
        brief = (await s.manager.staff.messages(ada.id))[0]
        assert brief.origin == "operator" and f"[task {next_id} · assigned by the operator]" in brief.text
        assert all(f"{part}:" in brief.text for part in ("Objective", "Deliverable", "Boundaries", "Done when"))
        await message(s, brief.id, "acknowledged")
        prompts = [str(e["data"]["body"].get("prompt") or "") for e in s.ptyd.events if e["type"] == "hook" and e["data"].get("name") == "UserPromptSubmit"]
        assert any(f"[task {next_id}" in prompt for prompt in prompts)
        assert (await s.manager.staff.message(brief.id)).state == "acknowledged"  # type: ignore[union-attr]


async def test_after_a_restart_a_live_session_is_reconciled_without_inferred_assignments(
        settings: Settings, db: Database) -> None:
    """Rebuilding a runtime cannot treat another card's assignee label as a launch command."""
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:the scouting is done")
        await s.status_event(ada, "turn_done_unseen")
        first = await s.session_row(ada)
        await db.execute("UPDATE staff_sessions SET status = 'no_signal' WHERE id = ?", (first.id,))
        one, two = await s.task("One;echo:one is done;"), await s.task("Two;echo:two is done;")
        runtime = s.restart_runtime(ClaudeCodeAdapter())
        assert await runtime.reconcile(wait=5) == 1
        current = await s.session_row(ada)
        assert (current.id, current.task_id, current.terminal_id) == (first.id, first.task_id, first.terminal_id)
        assert await db.fetchall("SELECT id FROM effect_outbox WHERE kind = 'task.launch'"
                                  " AND json_extract(payload_json,'$.control.task_id') IN (?,?)", (one, two)) == []
        assert (await s.statuses(ada)).count("starting") == 1


async def test_a_session_left_grey_is_settled_by_its_task_or_its_idle_screen(settings: Settings, db: Database) -> None:
    """Rows an earlier host left ``no_signal`` in front of an idle CLI are settled on the next tick:
    to idle when the task is over (the member reported long ago; nobody needs waking), to a finished
    turn when the screen shows the prompt and the task is still being worked. The health line then
    stops calling the member silent."""
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "echo:the scouting is done")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        await db.execute("UPDATE staff_sessions SET status = 'no_signal', last_signal_at = '2026-09-25T10:58:21+00:00' WHERE id = ?", (row.id,))
        assert (await s.team.health(await s.team.live(row.id))).silent is True  # type: ignore[arg-type]

        # The task is still in doing and Claude sits at its prompt: a finished turn, read from the screen.
        await s.team.tick()
        assert (await s.session_row(ada)).status == "turn_done_unseen"
        assert (await s.events("staff.status", staff_id=ada.id))[-1].payload["detail"] == "read from the screen"

        # The task is done: the grey row is idle, whatever the screen, and the health line is quiet.
        await db.execute("UPDATE staff_sessions SET status = 'no_signal' WHERE id = ?", (row.id,))
        await db.execute("UPDATE board_tasks SET status = 'done' WHERE id = ?", (row.task_id,))
        await s.team.tick()
        settled = await s.session_row(ada)
        assert settled.status == "idle"
        last = (await s.events("staff.status", staff_id=ada.id))[-1].payload
        assert (last["previous"], last["status"]) == ("no_signal", "idle") and f"its task {row.task_id} is done" in last["detail"]
        health = await s.team.health(await s.team.live(row.id))  # type: ignore[arg-type]
        assert (health.silent, health.silent_s) == (False, None)


async def test_an_answer_leaves_the_session_waiting_on_what_is_still_open(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        fake = FakeStaffRuntime(kind="claude")
        s.team.runtimes["claude"] = fake
        ada = await started(s, "echo:unused")
        live = await s.team.live_of(ada)
        assert live is not None
        permission = await s.team.ingress.permission(live, "p1", "Bash", "npm test")
        await s.team.ingress.question(live, "q1", "Which branch?", ["main", "dev"])
        await s.team.answer(permission, allow=True, by="operator")
        after = (await s.events("staff.status", staff_id=ada.id))[-1].payload
        assert after["status"] == "question" and "Which branch?" in after["waiting_for"]
        [question] = await s.manager.asks.open_for(s.project.id)
        await s.team.answer(question.id, text="dev", by="operator")
        assert (await s.events("staff.status", staff_id=ada.id))[-1].payload["status"] == "working"
        # A request seen twice — a hook replayed after a restart — is the one already open.
        again = await s.team.ingress.question(live, "q2", "Which oven?", [])
        assert await s.team.ingress.question(live, "q2", "Which oven?", []) == again
        assert len(await s.manager.asks.open_for(s.project.id)) == 1


class ChannelFirst(ClaudeCodeAdapter):
    """Claude driven as a CLI whose first prompt goes through its channel once it is ready (as
    Codex's and OpenCode's do), to prove a restart during the start does not lose that prompt."""

    capabilities = dataclasses.replace(ClaudeCodeAdapter.capabilities, first_prompt="channel")

    def launch_plan(self, spec: LaunchSpec) -> Any:
        plan = super().launch_plan(dataclasses.replace(spec, first_prompt=None))
        return dataclasses.replace(plan, first_prompt=spec.first_prompt, first_prompt_via="channel")

    async def after_spawn(self, term: Any, launch: Launch, plan: Any) -> None:
        await term.write(paste=plan.first_prompt)
        await asyncio.sleep(0.3)
        await term.write(keys=["Enter"])


async def test_a_restart_does_not_resend_an_unproven_first_prompt(settings: Settings, db: Database) -> None:
    ChannelFirst.name = "claude"
    async with stand(settings, db, extra_env={"FAKE_CLI_FAULTS": "slow_ready:2500"}, adapter=ChannelFirst()) as s:
        trust(s)
        ada = await started(s, "echo:first things first")
        await s.status_event(ada, "starting")
        row = await s.session_row(ada)
        async def spawned() -> bool:
            launch = await HarnessStore(db).open_launch_for(row.id)
            return bool(launch and launch.terminal_id)

        # Queued admission claims the staff session before creating a physical terminal. Closing
        # the host at that earlier point tests a refused launch, not recovery of an existing CLI.
        await eventually(spawned, "the physical CLI exists before the host disconnects")
        # The host goes while the CLI is still starting; it gets ready with nobody listening.
        s.runtime.close()
        await s.terminals.close()

        async def ready_meanwhile() -> bool:
            return any(e["type"] == "hook" and e["data"].get("name") == "SessionStart" for e in s.ptyd.events)

        await eventually(ready_meanwhile, "the CLI got ready while the host was away")
        s.terminals = terminals_service(db, s.manager, s.root / "run", s.home)
        s.team.app.extensions["terminals"] = s.terminals
        await s.terminals.start()
        assert await s.terminals.wait_available("container")
        runtime = s.restart_runtime(ChannelFirst())
        assert await runtime.reconcile(wait=5) == 1
        [first] = await s.manager.staff.messages(ada.id)
        async def settled() -> bool:
            [persisted] = await db.fetchall("SELECT state FROM staff_messages WHERE id = ?", (first.id,))
            return persisted["state"] in ("acknowledged", "failed")

        await eventually(settled, "first prompt has a proven or unknown outcome")
        [persisted] = await db.fetchall("SELECT state,error FROM staff_messages WHERE id = ?", (first.id,))
        if persisted["state"] == "failed":
            assert persisted["error"] == "unknown after restart"
        submitted = [e["text"] for e in log(s, "submitted")]
        assert len(submitted) <= 1 and row.id


# -- the self-check, the wiring and the staff view's routes ------------------------------------------


async def test_the_self_check_runs_one_session_through_every_channel(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        env = RuntimeEnvironment(s.terminals, "container", home=str(s.home))
        result = await session_check(ClaudeCodeAdapter(), s.terminals, lambda: s.harness, env, "haiku", True)
        assert result.ok, result.steps
        assert [step.name for step in result.steps] == ["launch", "ready", "team", "deliver", "reply", "exit"]
        assert "Report came through the team channel" in result.steps[4].detail and result.steps[5].detail == "exit code 0"
        # A stable folder of its own, trusted once: a second check is not asked again.
        assert (s.home / ".cache" / "daedalus-selfcheck" / "claude").is_dir()
        quiet = await session_check(ClaudeCodeAdapter(), s.terminals, lambda: s.harness, env, "haiku", False)
        assert quiet.ok and [step.name for step in quiet.steps] == ["launch", "ready", "team", "exit"]
        assert await HarnessStore(db).open_launches() == []


async def test_installing_the_extension_registers_the_claude_runtime_and_its_self_check(settings: Settings, db: Database) -> None:
    async with stand(settings, db, **claude()) as s:
        app = SimpleNamespace(manager=s.manager, extensions={"terminals": s.terminals, "staff": s.team}, config=SimpleNamespace(harness=s.harness), db=db)
        tasks = await install_harness(app)  # type: ignore[arg-type]
        try:
            assert isinstance(s.team.runtimes["claude"], CliStaffRuntime) and isinstance(s.team.runtimes["claude"].adapter, ClaudeCodeAdapter)  # type: ignore[attr-defined]
            assert "claude" in app.extensions["harness"].self_checks
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_the_staff_views_routes(settings: Settings, db: Database, config: RuntimeConfig) -> None:
    async with stand(settings, db, **claude()) as s:
        trust(s)
        ada = await started(s, "report:checkpoint:halfway;echo:the menu is written")
        await s.status_event(ada, "turn_done_unseen")
        app = SimpleNamespace(settings=settings, config=config, db=db, manager=s.manager, front=None, extensions=s.team.app.extensions, guard=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        headers = {"X-Daedalus-Token": "tok"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            view = (await client.get(f"/api/staff/{ada.id}/session", headers=headers)).json()
            assert view["session"]["status"] == "turn_done_unseen" and view["capabilities"]["steer"] == "tui_queue"
            assert view["launch"]["harness"] == "claude" and view["launch"]["launch_id"] and view["channel"]["team_tools"] == "connected"
            # The verdict on the channels, the same on the view, on the team's card and in Team(staff).
            health = view["health"]
            assert (health["team_tools"], health["level"], health["problems"], health["silent"]) == ("connected", "ok", [], False), health
            assert health["last_hook_at"] and health["last_team_call_at"] and health["silence_after_s"] == s.harness.no_signal_after_s
            [card] = (await client.get(f"/api/projects/{s.project.id}/staff", headers=headers)).json()["staff"]
            assert card["health"]["team_tools"] == "connected" and (await client.get(f"/api/staff/{ada.id}", headers=headers)).json()["health"]["level"] == "ok"
            # Its terminal's card on the Terminals screen says what it is doing.
            row = await s.session_row(ada)
            [terminal] = [t for t in (await client.get("/api/terminals", headers=headers)).json()["terminals"] if t["id"] == row.terminal_id]
            assert terminal["activity"]["status"] == "turn_done_unseen" and "action" not in terminal["activity"]
            # "Release" gives the keyboard back; only a person's two choices are offered.
            assert (await client.post(f"/api/terminals/{row.terminal_id}/keyboard", headers=headers, json={"owner": "human"})).json()["owner"] == "human"
            assert (await client.post(f"/api/terminals/{row.terminal_id}/keyboard", headers=headers, json={"owner": "auto"})).json()["owner"] == "auto"
            assert (await client.post(f"/api/terminals/{row.terminal_id}/keyboard", headers=headers, json={"owner": "agent"})).status_code == 422
            told = await client.post(f"/api/staff/{ada.id}/messages", headers=headers, json={"text": "echo:and a footer", "when": "after_turn"})
            assert told.status_code == 200 and told.json()["state"] == "queued"
            await message(s, told.json()["message_id"], "acknowledged")
            [newest] = (await client.get(f"/api/staff/{ada.id}/messages?limit=1", headers=headers)).json()
            assert newest["state"] == "acknowledged" and newest["delivery"]["via"] == "paste" and newest["delivery"]["enters"] == 1
            older = (await client.get(f"/api/staff/{ada.id}/messages?before={newest['id']}", headers=headers)).json()
            assert [m["state"] for m in older] == ["acknowledged"]

            async def second_turn_ended() -> bool:
                return (await s.statuses(ada)).count("turn_done_unseen") == 2

            await eventually(second_turn_ended, "the second turn ended")

            async def usage_read() -> bool:
                spent = (await client.get(f"/api/staff/{ada.id}/session", headers=headers)).json()["usage"] or {}
                return spent.get("replies", 0) >= 2

            # The spend the Details tab shows is read from the transcript after each turn, with how
            # full the context was and nothing Claude does not report (its window, its rate limits).
            await eventually(usage_read, "the usage was read after the turn")
            spent = (await client.get(f"/api/staff/{ada.id}/session", headers=headers)).json()["usage"]
            assert spent["output_tokens"] > 0 and spent["context_tokens"] > 0 and spent["at"], spent
            assert spent["context_window"] is None and spent["windows"] == [] and spent["source"] == "subscription"
            turns = (await client.get(f"/api/staff/{ada.id}/transcript", headers=headers)).json()["turns"]
            assert turns[0]["role"] == "user" and any(t["text"] == "and a footer" for t in turns if t["role"] == "assistant"), turns
            events = (await client.get(f"/api/staff/{ada.id}/events?limit=50", headers=headers)).json()["events"]
            assert {"staff.status", "staff.report", "staff.message"} <= {e["type"] for e in events}
            assert (await client.post(f"/api/staff/{ada.id}/seen", headers=headers)).json() == {"ok": True}
            assert (await s.session_row(ada)).status == "idle"
            changes = (await client.get(f"/api/staff/{ada.id}/changes", headers=headers)).json()
            assert changes["files"] == [] and changes["detail"] == "no worktree of its own"
            assert (await client.get("/api/staff/st-none/session", headers=headers)).status_code == 404
            # The standing grants are listed with the session and revoked one by one.
            assert view["rules"] == [] and view["capabilities"]["standing_rules"] is True
            await s.manager.staff.grant_rule(ada.id, "mcp__daedalus_browser")
            assert [r["rule"] for r in (await client.get(f"/api/staff/{ada.id}/session", headers=headers)).json()["rules"]] == ["mcp__daedalus_browser"]
            assert (await client.delete(f"/api/staff/{ada.id}/rules/mcp__daedalus_browser", headers=headers)).json() == {"rules": []}
            assert (await client.delete(f"/api/staff/{ada.id}/rules/mcp__daedalus_browser", headers=headers)).status_code == 404


def test_an_agent_named_default_is_not_passed_to_the_cli() -> None:
    # Claude Code has no agent called "default": passed on, it ends every launch before the first turn.
    argv = list(ClaudeCodeAdapter().launch_plan(spec(agent="default")).argv)
    assert "--agent" not in argv
    argv = list(ClaudeCodeAdapter().launch_plan(spec(agent="reviewer")).argv)
    assert argv[argv.index("--agent") + 1] == "reviewer"
