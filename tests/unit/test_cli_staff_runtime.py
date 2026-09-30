"""The staff runtime of a command-line agent, end to end: the team assigns a task, the runtime starts
a fake Claude in a real terminal of the daemon's test double behind the real terminals service, and
the statuses, requests and team tools arrive through the team's ingress.

The adapter is a stub written here, as small as a Claude adapter can be, because the real one is
built later on top of this; what is under test is the runtime around it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.config import HarnessConfig, Settings, TerminalsConfig
from daedalus.extensions.harness import catalog_roots
from daedalus.extensions.staff import Team
from daedalus.harness.capabilities import capabilities
from daedalus.harness.contract import (
    LAUNCH_DIR,
    Answer,
    Catalog,
    CheckResult,
    CheckStep,
    CompanionSpec,
    Delivery,
    EnvironmentPort,
    EventKind,
    HarnessAdapter,
    InstallInfo,
    Launch,
    LaunchPlan,
    LaunchSpec,
    LoginState,
    ReadyStep,
    ScreenClass,
    SendMode,
    StaffEvent,
    TerminalPort,
    Turn,
    TurnUsage,
    UpdateResult,
)
from daedalus.harness.manager import HarnessManager, Operation
from daedalus.harness.runtime import CliStaffRuntime, install_runtimes
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.staff_runtime import LiveSession, ReadRequest
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from daedalus.stores.projects import FolderSpec, Project
from daedalus.stores.staff import Staff
from daedalus.terminals.model import Owner, TerminalSpec
from daedalus.terminals.owners import ManagerOwners
from daedalus.terminals.service import Terminals
from tests.support import fake_cli
from tests.support.fake_cli.tui import read_log
from tests.support.harness_ports import Rig
from tests.support.live_ptyd import LivePtyd
from tests.unit.test_session_runner import ScriptedProvider, _manager

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

BRIEF = {"objective": "Write the menu", "deliverable": "menu.md", "boundaries": "Touch nothing else", "done_when": "menu.md exists"}
HOOKS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse", "Stop", "StopFailure", "Notification", "SessionEnd")


class StubClaude:
    """A Claude adapter cut down to what the runtime needs: command hooks through the daemon's hook
    command, the trust dialog recognised on screen, a permission answered with its digit."""

    name = "claude"
    capabilities = capabilities("claude")

    def __init__(self, script: str = "echo:hello", *, companion: bool = False, knows_trust: bool = True) -> None:
        self.script = script
        self.companion = companion
        self.knows_trust = knows_trust
        self.attached: list[str] = []
        self.after_spawned: list[str] = []
        self._permissions = 0
        self._waiting = ""

    # The manager's half is not this unit's.
    async def installed(self, env: EnvironmentPort) -> InstallInfo:
        return InstallInfo(True, "claude", "2.1.281", "native")

    async def latest(self, env: EnvironmentPort) -> str:
        return "2.1.281"

    async def update(self, env: EnvironmentPort) -> UpdateResult:
        return UpdateResult(True, "2.1.281", "2.1.281")

    async def self_check(self, env: EnvironmentPort) -> CheckResult:
        return CheckResult(True, (), "2.1.281", 0)

    async def catalog(self, env: EnvironmentPort, cwd: str | None) -> Catalog:
        return Catalog()

    async def login_state(self, env: EnvironmentPort) -> LoginState:
        return LoginState("yes")

    def launch_plan(self, spec: LaunchSpec) -> LaunchPlan:
        return self._plan(spec, ["--session-id", str(uuid.uuid4())])

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan:
        return self._plan(spec, ["--resume", ref])

    def _plan(self, spec: LaunchSpec, session: list[str]) -> LaunchPlan:
        hooks = {event: [{"hooks": [{"type": "command", "command": f'"$DAEDALUS_PTYD_BIN" hook {event}', "timeout": 30}]}] for event in HOOKS}
        settings = {"hooks": hooks, "permissions": {"allow": ["mcp__daedalus_team__Report", "mcp__daedalus_team__AskOrchestrator"]}}
        mcp = {"mcpServers": {"daedalus_team": {"command": "ptyd", "args": ["team-mcp"], "env": {"DAEDALUS_ASK_HOLD_MS": "60000"}}}}
        # The task text, then this test's script as a segment of its own for the fake's scripted model.
        prompt = f"{spec.first_prompt};{self.script}"
        argv = ("claude", *session, "--settings", f"{LAUNCH_DIR}/settings.json", "--mcp-config", f"{LAUNCH_DIR}/mcp.json", "--permission-mode", "manual", prompt)
        companions = (CompanionSpec("app-server", ("claude", "--session-id", str(uuid.uuid4())), ready_pattern="Quick safety check"),) if self.companion else ()
        return LaunchPlan(
            argv=argv,
            env={"DAEDALUS_REPORT_HOLD_MS": "20000"},
            cwd=spec.cwd,
            files={"settings.json": json.dumps(settings).encode(), "mcp.json": json.dumps(mcp).encode()},
            companions=companions,
            session_ref=session[1] if session[0] == "--session-id" else session[1],
            first_prompt=prompt,
        )

    def readiness(self, screen: str) -> ReadyStep:
        if "Select login method" in screen:
            return ReadyStep("fail", reason="Claude Code is not signed in")
        if self.knows_trust and "Quick safety check" in screen and "❯ No, exit" in screen:
            return ReadyStep("keys", ("Down",), "folder trust: down")
        if self.knows_trust and "Quick safety check" in screen and "❯ Yes, I trust this folder" in screen:
            return ReadyStep("keys", ("Enter",), "folder trust")
        return ReadyStep()

    async def after_spawn(self, term: TerminalPort, launch: Launch, plan: LaunchPlan) -> None:
        self.after_spawned.append(launch.launch_id)

    async def attach(self, term: TerminalPort, launch: Launch) -> None:
        self.attached.append(launch.launch_id)

    async def events(self, term: TerminalPort, launch: Launch) -> AsyncIterator[StaffEvent]:
        async for post in term.hooks():
            body = post.body if isinstance(post.body, dict) else {}
            at = post.at
            name = post.name
            if name == "SessionStart":
                yield StaffEvent(EventKind.TRANSCRIPT, at, {"ref": body.get("transcript_path"), "session_ref": body.get("session_id")})
                yield StaffEvent(EventKind.READY, at)
            elif name == "UserPromptSubmit":
                yield StaffEvent(EventKind.PROMPT_ACKNOWLEDGED, at, {"prompt": body.get("prompt")})
            elif name == "PreToolUse":
                yield StaffEvent(EventKind.TOOL_STARTED, at, native_id=str(body.get("tool_use_id") or ""))
            elif name == "PostToolUse":
                if self._waiting:
                    # The tool ran after a permission: whoever answered it, it is settled.
                    yield StaffEvent(EventKind.REQUEST_RESOLVED, at, native_id=self._waiting)
                    self._waiting = ""
                yield StaffEvent(EventKind.TOOL_FINISHED, at, native_id=str(body.get("tool_use_id") or ""))
            elif name == "PermissionRequest":
                self._permissions += 1
                command = str((body.get("tool_input") or {}).get("command") or "")
                self._waiting = f"perm-{self._permissions}"
                yield StaffEvent(EventKind.PERMISSION_REQUESTED, at, {"tool": body.get("tool_name"), "summary": command}, native_id=self._waiting)
            elif name == "Stop":
                yield StaffEvent(EventKind.TURN_COMPLETED, at)
            elif name == "StopFailure":
                yield StaffEvent(EventKind.TURN_FAILED, at, {"failure": body.get("error")})
            elif name == "SessionEnd":
                yield StaffEvent(EventKind.SESSION_ENDED, at)
            else:
                yield StaffEvent(EventKind.NOTIFICATION, at, {"type": body.get("notification_type")})

    async def send(self, term: TerminalPort, message_id: str, text: str, mode: SendMode) -> Delivery:
        await term.write(paste=text)
        await term.write(keys=["Enter"])
        return Delivery(message_id, "submitted", via="paste")

    async def interrupt(self, term: TerminalPort) -> None:
        await term.write(keys=["Esc"])

    async def answer(self, term: TerminalPort, request_ref: str, answer: Answer) -> bool:
        # The hook comes before the dialog is drawn: keys typed before it would land in the composer.
        if not await term.wait_for(regex="Do you want to proceed", timeout=10):
            return False
        await term.write(text="1" if answer.choice.startswith("allow") else "3")
        return True

    async def transcript(self, env: EnvironmentPort, ref: str, since: int = 0) -> list[Turn]:
        turns: list[Turn] = []
        for line in (await env.read(ref)).decode().splitlines():
            record = json.loads(line)
            message = record.get("message") or {}
            content = message.get("content") or []
            text = content if isinstance(content, str) else "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            if record.get("type") == "assistant":
                usage = message.get("usage") or {}
                turns.append(Turn(len(turns), "assistant", text, usage=TurnUsage(int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), int(usage.get("cache_read_input_tokens") or 0))))
            elif record.get("type") == "user" and text:
                turns.append(Turn(len(turns), "user", text))
        return turns[since:]

    async def stop(self, term: TerminalPort) -> None:
        await term.write(paste="/exit")
        await term.write(keys=["Enter"])

    def classify_screen(self, text: str) -> ScreenClass:
        # Conservative on purpose: a busy screen is not recognised, so silence becomes no_signal.
        if "Do you want to proceed" in text or "Quick safety check" in text:
            return ScreenClass.DIALOG
        if "? for shortcuts" in text:
            return ScreenClass.IDLE_COMPOSER
        return ScreenClass.UNKNOWN

    def composer_holds(self, screen: str, text: str) -> bool:
        # Enough for the fake: the composer line is the last "❯ " line, and a long paste shows a marker.
        lines = [line for line in screen.splitlines() if line.startswith("❯ ")]
        return bool(lines) and ("[Pasted text #" in lines[-1] or text.split()[-1] in lines[-1])


class Notes:
    def __init__(self) -> None:
        self.posted: list[Any] = []

    async def post(self, draft: Any) -> None:
        self.posted.append(draft)


@dataclass
class Stand:
    root: Path
    home: Path
    work: Path
    log: Path
    ptyd: LivePtyd
    terminals: Terminals
    manager: SessionManager
    team: Team
    runtime: CliStaffRuntime
    adapter: Any
    project: Project
    harness: HarnessConfig

    async def hire(self, name: str = "Ada") -> Staff:
        return await self.manager.staff.hire(self.project.id, name=name, harness=getattr(self.adapter, "name", "claude"), isolation="shared")

    async def task(self, title: str = "Menu page") -> str:
        task_id = f"t{uuid.uuid4().hex[:6]}"
        now = "2026-09-24T00:00:00+00:00"
        await self.manager.db.execute(
            "INSERT INTO board_tasks(id, title, status, priority, created_at, updated_at, project_id, brief_json) VALUES (?, ?, 'todo', 3, ?, ?, ?, ?)",
            (task_id, title, now, now, self.project.id, json.dumps(BRIEF)),
        )
        return task_id

    async def events(self, *types: str, **ids: str) -> list[AppEvent]:
        return await self.manager.bus.replay(0, EventFilter(types=types, **ids), limit=5000)

    async def statuses(self, member: Staff) -> list[str]:
        return [e.payload["status"] for e in await self.events("staff.status", staff_id=member.id)]

    async def status_event(self, member: Staff, status: str, *, timeout: float = 30.0) -> AppEvent:
        async with asyncio.timeout(timeout):
            while True:
                for event in await self.events("staff.status", staff_id=member.id):
                    if event.payload["status"] == status:
                        return event
                await asyncio.sleep(0.05)

    async def session_row(self, member: Staff) -> Any:
        live = await self.manager.staff.live(member.id)
        assert live is not None
        return live

    def restart_runtime(self, adapter: Any = None) -> CliStaffRuntime:
        self.runtime.close()
        self.adapter = adapter or self.adapter
        self.runtime = CliStaffRuntime(self.adapter, terminals=self.terminals, store=HarnessStore(self.manager.db), ingress=self.team.ingress, lookup=self.team.live, config=lambda: self.harness)
        self.team.runtimes[getattr(self.adapter, "name", "claude")] = self.runtime
        return self.runtime


def harness_config(**changes: Any) -> HarnessConfig:
    # Constructed without validation so a test can use seconds where the settings insist on minutes.
    return HarnessConfig.model_construct(**{**HarnessConfig().model_dump(), "ready_poll_ms": 50, "stop_grace_s": 5.0, **changes})


def terminals_service(db: Database, manager: SessionManager, run: Path, home: Path, *, cap: int = 20) -> Terminals:
    service = Terminals(db, run_dirs={"container": run, "host": None}, config=lambda: TerminalsConfig(running_cap=cap, kill_grace_ms=300, agent_launch_wait_seconds=60), owners=ManagerOwners(manager), bus=manager.bus)
    # Exactly what the host lets itself read of a home, not the whole home: a transcript directory
    # missing from that list makes the adapter's reads fail here as they would against the daemon.
    service.set_extra_roots("container", "harness-catalog", catalog_roots(str(home)))
    return service


@asynccontextmanager
async def stand(settings: Settings, db: Database, *, adapter: Any = None, extra_env: dict[str, str] | None = None, cap: int = 20, input_idle_ms: int = 0, **config: Any) -> AsyncIterator[Stand]:
    root = Path(tempfile.mkdtemp(prefix="ptyd-"))
    home, work, bin_dir, log = root / "home", root / "work", root / "bin", root / "fake-cli.jsonl"
    home.mkdir()
    work.mkdir()
    fake_cli.install(bin_dir)
    ptyd = LivePtyd(root / "run", home=home, bin_dir=bin_dir, base_env={"FAKE_CLI_LOG": str(log), "FAKE_CLI_TIME_SCALE": "0.05", **(extra_env or {})}, input_idle_ms=input_idle_ms)
    await ptyd.start()
    manager = await _manager(settings, db, ScriptedProvider([]))
    terminals = terminals_service(db, manager, root / "run", home, cap=cap)
    try:
        await terminals.start()
        assert await terminals.wait_available("container")
        manager.config.staff.launch_stagger_seconds = 0
        app = SimpleNamespace(manager=manager, extensions={"terminals": terminals}, settings=settings, notifications=Notes(), db=db)
        team = Team(app)  # type: ignore[arg-type]
        app.extensions["staff"] = team
        team.attach()
        project = await manager.projects.create("Bakery", [FolderSpec(str(work), env="container")])
        await manager.projects.update_orchestrator(project.id, enabled=True, autonomy="normal")
        project = await manager.projects.get(project.id) or project
        chosen = adapter or StubClaude()
        cfg = harness_config(**config)
        runtime = CliStaffRuntime(chosen, terminals=terminals, store=HarnessStore(db), ingress=team.ingress, lookup=team.live, config=lambda: cfg)
        team.runtimes[getattr(chosen, "name", "claude")] = runtime
        made = Stand(root, home, work, log, ptyd, terminals, manager, team, runtime, chosen, project, cfg)
        yield made
    finally:
        made_runtime = locals().get("made")
        if made_runtime is not None:
            made_runtime.runtime.close()
        await terminals.close()
        await manager.close()
        await ptyd.stop()
        shutil.rmtree(root, ignore_errors=True)


def trust(s: Stand) -> None:
    """What accepting Claude's trust dialog once leaves behind, for the tests that are not about it."""
    (s.home / ".claude.json").write_text(json.dumps({"projects": {str(s.work): {"hasTrustDialogAccepted": True}}}))


async def eventually(check: Any, what: str, *, timeout: float = 30.0) -> None:
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.05)


# -- a session from launch to release -----------------------------------------------------------------


async def test_a_session_runs_from_the_trust_dialog_to_its_release(settings: Settings, db: Database) -> None:
    async with stand(settings, db) as s:
        ada = await s.hire()
        task_id = await s.task()
        assigned = await s.team.assign(ada, task_id)
        assert assigned["state"] == "started"
        await s.status_event(ada, "turn_done_unseen")
        assert await s.statuses(ada) == ["starting", "working", "turn_done_unseen"]

        row = await s.session_row(ada)
        view = await s.terminals.get(row.terminal_id)
        # The CLI's terminal is the member's, counted as a Claude Code terminal, started by an agent.
        assert (view["owner"]["kind"], view["owner"]["id"], view["profile"], view["created_by"]) == ("staff", ada.id, "harness:claude", f"agent:staff:{row.id}")
        launch = await HarnessStore(db).open_launch_for(row.id)
        assert launch is not None and launch.terminal_id == row.terminal_id and launch.launch_dir and launch.session_ref == row.cli_session_id
        # The trust dialog was answered by the readiness gate, on screen, and written down as such.
        writes = await db.fetchall("SELECT detail_json FROM terminal_audit WHERE terminal_id = ? AND action = 'write'", (row.terminal_id,))
        assert [json.loads(w["detail_json"]).get("note") for w in writes] == ["readiness: folder trust: down", "readiness: folder trust"]
        assert json.loads((s.home / ".claude.json").read_text())["projects"][str(s.work)]["hasTrustDialogAccepted"] is True
        assert s.adapter.after_spawned == [launch.launch_id]
        # Where the CLI keeps the session, learnt from its first hook.
        assert row.transcript_ref and row.transcript_ref.endswith(f"{row.cli_session_id}.jsonl")
        first = [m for m in await s.manager.staff.messages(ada.id)]
        assert [m.state for m in first] == ["acknowledged"]

        async def spent() -> bool:
            return bool((await s.session_row(ada)).usage)

        await eventually(spent, "the turn's usage was recorded")
        usage = (await s.session_row(ada)).usage
        assert usage["source"] == "subscription" and usage["output_tokens"] > 0

        last = await s.runtime.read(await s.team.live(row.id), ReadRequest("last"))  # type: ignore[arg-type]
        assert "hello" in last.text and last.next_cursor is not None
        turns = await s.runtime.read(await s.team.live(row.id), ReadRequest("turns", turns=2))  # type: ignore[arg-type]
        # The protocol's line leads the first prompt, the task after it.
        assert turns.text.startswith("» [team] You are Ada, staff of Bakery") and "[task" in turns.text and "hello" in turns.text
        screen = await s.runtime.read(await s.team.live(row.id), ReadRequest("screen"))  # type: ignore[arg-type]
        assert "? for shortcuts" in screen.text

        assert await s.team.release(ada)
        assert (await s.statuses(ada))[-1] == "exited"
        assert (await s.terminals.get(row.terminal_id))["status"] == "exited"
        assert await HarnessStore(db).open_launch_for(row.id) is None
        assert launch.launch_id in s.ptyd.ended_launches
        assert [e["name"] for e in read_log(s.log) if e["event"] == "hook"][-1] == "SessionEnd"
        # Asked to exit, it exited: the terminal was not killed.
        assert (await s.terminals.get(row.terminal_id))["exit_code"] == 0


async def test_a_session_that_is_not_signed_in_fails_with_the_screen_it_shows(settings: Settings, db: Database) -> None:
    async with stand(settings, db, extra_env={"FAKE_CLAUDE_LOGGED_IN": "0"}) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        failed = await s.status_event(ada, "error")
        assert failed.payload["detail"].startswith("Claude Code is not signed in") and "Select login method" in failed.payload["detail"]
        row = await s.session_row(ada)
        # Left running, for the operator to sign in and look.
        assert (await s.terminals.get(row.terminal_id))["status"] == "running"


async def test_a_dialog_the_adapter_does_not_know_is_waited_on_and_never_answered(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude(knows_trust=False), ready_timeout_s=1.0) as s:
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        failed = await s.status_event(ada, "error")
        assert failed.payload["detail"].startswith("not ready after 1 s") and "Quick safety check" in failed.payload["detail"]
        row = await s.session_row(ada)
        assert await db.fetchall("SELECT 1 FROM terminal_audit WHERE terminal_id = ? AND action = 'write'", (row.terminal_id,)) == []


# -- requests and the team tools ----------------------------------------------------------------------


async def test_a_permission_becomes_a_request_and_its_answer_reaches_the_dialog(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude("perm:npm install grammy")) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        waiting = await s.status_event(ada, "permission")
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert waiting.payload["waiting_for"] == f"permission [{ask.short_id}]: Bash"
        assert (ask.kind, ask.routed_to, ask.request_ref, ask.text) == ("permission", "orchestrator", "perm-1", "Bash: npm install grammy")
        [pending] = await s.events("permission.pending", staff_id=ada.id)
        assert pending.payload["short_id"] == ask.short_id and pending.payload["tool"] == "Bash"

        answered = await s.team.answer(ask.short_id, allow=True, by="operator")
        assert answered["delivered"] is True
        await s.status_event(ada, "turn_done_unseen")
        assert await s.statuses(ada) == ["starting", "working", "permission", "working", "turn_done_unseen"]
        assert [e["label"] for e in read_log(s.log) if e["event"] == "dialog_answered" and e["kind"] == "permission"] == ["Yes"]
        row = await s.session_row(ada)
        assert "Ran npm install grammy." in "\n".join((await s.terminals.read_screen(row.terminal_id))["lines"])


async def test_a_permission_answered_in_the_terminal_closes_its_request(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude("perm:make build")) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        await s.status_event(ada, "permission")
        row = await s.session_row(ada)
        await s.terminals.wait_for(row.terminal_id, regex="Do you want to proceed", timeout=10)
        await s.ptyd.type_as_human(row.terminal_id, "1")
        await s.status_event(ada, "turn_done_unseen")
        assert await s.manager.asks.open_for(s.project.id) == []
        [resolved] = await s.events("permission.resolved", staff_id=ada.id)
        assert (resolved.payload["by"], resolved.payload["via"], resolved.payload["decision"]) == ("operator", "terminal", "terminal")
        assert await s.statuses(ada) == ["starting", "working", "permission", "working", "turn_done_unseen"]


async def test_the_team_tools_are_answered_by_the_host(settings: Settings, db: Database) -> None:
    script = "askorch:Euros or dollars?|euros|dollars;report:checkpoint:menu drafted;report:finished:not a kind"
    async with stand(settings, db, adapter=StubClaude(script)) as s:
        trust(s)
        ada = await s.hire()
        task_id = await s.task()
        await s.team.assign(ada, task_id)
        asked = await s.status_event(ada, "question")
        assert "Euros or dollars?" in asked.payload["waiting_for"]
        [ask] = await s.manager.asks.open_for(s.project.id)
        assert ask.kind == "question" and ask.request_ref.startswith("team:") and ask.detail["options"] == ["euros", "dollars"]

        await s.team.answer(ask.short_id, text="euros", selected=["euros"], by="orchestrator")
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        screen = "\n".join((await s.terminals.read_screen(row.terminal_id, scrollback=200))["lines"])
        # The held question got the answer; the report got what the team made of it; a refused
        # report came back to the worker as an error with the reason.
        assert "AskOrchestrator: euros" in screen
        assert "Report: reported checkpoint" in screen
        assert "Report: kind is checkpoint, needs_input, stuck or done" in screen
        [report] = await s.events("staff.report", staff_id=ada.id)
        assert (report.payload["kind"], report.payload["text"], report.payload["task_id"]) == ("checkpoint", "menu drafted", task_id)
        assert await s.statuses(ada) == ["starting", "working", "question", "working", "turn_done_unseen"]
        assert [r["body"] for r in s.ptyd.replies][0] == {"text": "euros"}


async def test_a_held_question_gets_the_option_and_the_operators_whole_note(settings: Settings, db: Database) -> None:
    from tests.unit.test_answer_words import NOTE

    async with stand(settings, db, adapter=StubClaude("askorch:Euros or dollars?|euros|dollars")) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        await s.status_event(ada, "question")
        [ask] = await s.manager.asks.open_for(s.project.id)
        await s.team.answer(ask.short_id, selected=["euros"], note=NOTE, by="operator")
        # The note once replaced the option here, and a command-line dialog got the option alone.
        assert [r["body"] for r in s.ptyd.replies][0] == {"text": f"euros — {NOTE}"}


async def test_an_answer_after_the_hold_expired_arrives_as_a_message(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude("askorch:Which oven?|left|right")) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        await s.status_event(ada, "question")
        [ask] = await s.manager.asks.open_for(s.project.id)
        reply_id = ask.request_ref.removeprefix("team:")
        # The hold ends without an answer: the worker was told to carry on.
        for launch in s.ptyd.launches.values():
            future = launch.held.get(reply_id)
            if future is not None:
                future.set_result((204, b"", ""))
        await s.team.answer(ask.short_id, text="the left one", by="operator")

        async def delivered() -> bool:
            return any(e["event"] == "submitted" and "[the operator answers your question] the left one" in str(e.get("text") or "") for e in read_log(s.log))

        await eventually(delivered, "the answer was sent as a message")


# -- silence, exits and companions ----------------------------------------------------------------------


async def test_silence_is_shown_as_no_signal_and_the_idle_composer_ends_the_turn(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude("silent"), extra_env={"FAKE_CLI_TIME_SCALE": "1"}, no_signal_after_s=0.5, reconcile_gap_ms=200) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        silent = await s.status_event(ada, "no_signal")
        assert silent.payload["detail"] == "read from the screen"
        done = await s.status_event(ada, "turn_done_unseen")
        assert done.payload["detail"] == "read from the screen"
        assert await s.statuses(ada) == ["starting", "working", "no_signal", "turn_done_unseen"]
        assert not [e for e in read_log(s.log) if e["event"] == "hook" and e["name"] == "Stop"]


async def test_a_member_whose_task_is_handed_in_is_never_called_silent(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude("silent"), extra_env={"FAKE_CLI_TIME_SCALE": "1"}, no_signal_after_s=0.5, reconcile_gap_ms=200) as s:
        trust(s)
        ada = await s.hire()
        task_id = await s.task()
        await s.team.assign(ada, task_id)
        await s.status_event(ada, "working")
        await s.manager.db.execute("UPDATE board_tasks SET status = 'review' WHERE id = ?", (task_id,))
        await asyncio.sleep(3)
        assert await s.statuses(ada) == ["starting", "working"]


async def test_a_cli_that_exits_by_itself_ends_its_session(settings: Settings, db: Database) -> None:
    async with stand(settings, db, extra_env={"FAKE_CLI_FAULTS": "exit_after:1"}) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        ended = await s.status_event(ada, "exited")
        assert ended.payload["detail"].startswith("Claude Code exited with code 3; its screen last showed: ")
        assert await s.manager.staff.live(ada.id) is None
        [session] = await s.manager.staff.sessions(ada.id)
        assert session.end_reason == ended.payload["detail"]
        assert await HarnessStore(db).open_launches() == []


async def test_a_cli_that_fails_at_its_start_says_why_and_leaves_its_card_free(settings: Settings, db: Database) -> None:
    """A helper's CLI exited with code 1 a third of a second after its start. The event said only
    that; ReadStaff failed twice with "the path must be absolute", because the session id stood in for
    the turn record the CLI never wrote; and the card stayed in doing under the dead helper."""
    async with stand(settings, db, extra_env={"FAKE_CLI_FAULTS": "fail_at_start"}) as s:
        trust(s)
        ada = await s.hire()
        task_id = await s.task()
        await s.team.assign(ada, task_id, by="orchestrator")
        ended = await s.status_event(ada, "exited")
        assert ended.payload["detail"].startswith("Claude Code exited with code 1; its screen last showed: ")
        assert "Error: the model given with --model is not available to this account" in ended.payload["detail"]
        [session] = await s.manager.staff.sessions(ada.id)
        assert session.end_reason == ended.payload["detail"] and not session.transcript_ref and session.cli_session_id

        live = LiveSession(ada, session)
        for what in ("last", "turns"):
            page = await s.runtime.read(live, ReadRequest(what))  # type: ignore[arg-type]
            assert page.text == f"no turn record: the session ended before its first turn ({session.end_reason})"

        async def freed() -> bool:
            row = await s.manager.db.fetchone("SELECT status, assignee_staff_id, notes FROM board_tasks WHERE id = ?", (task_id,))
            return row is not None and row["status"] == "todo" and row["assignee_staff_id"] is None

        await eventually(freed, "the card went back to todo, unassigned")


async def test_a_companion_that_dies_takes_the_side_channel_with_it(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude(companion=True)) as s:
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        await s.status_event(ada, "turn_done_unseen")
        launch = await HarnessStore(db).open_launch_for((await s.session_row(ada)).id)
        assert launch is not None and launch.companion_terminal_id
        companion = await s.terminals.get(launch.companion_terminal_id)
        assert companion["title"].endswith("· app-server") and companion["created_by"].startswith("agent:staff:")
        await s.terminals.kill(launch.companion_terminal_id)
        failed = await s.status_event(ada, "error")
        assert failed.payload["detail"] == "side channel lost: the app-server exited"


# -- the machine's cap ----------------------------------------------------------------------------------


async def test_a_launch_at_the_machine_cap_waits_for_a_place(settings: Settings, db: Database) -> None:
    async with stand(settings, db, cap=1) as s:
        # The operator's terminal holds the one place until it is killed: a Claude sitting at the trust
        # question of a folder nobody trusted, where a program that exits by itself would free the
        # place early and let the launch through without waiting — how this test once flaked.
        mine = await s.terminals.create(TerminalSpec(env="container", owner=Owner("free"), argv=["claude"], created_by="operator"))
        await s.terminals.wait_for(mine["id"], regex="Quick safety check", timeout=30)
        trust(s)
        s.team._capacity = SimpleNamespace(running=_zero, cap=lambda: 20, waiting=lambda: 0, unavailable=lambda env: None)
        ada = await s.hire()
        assigning = asyncio.create_task(s.team.assign(ada, await s.task()))
        # Woken by the line moving rather than by a poll racing the machine's load.
        assert await s.terminals.wait_queue(bool, timeout=120), "the launch never joined the terminals' line"
        [waiter] = s.terminals.queue()
        assert waiter["actor"].startswith("agent:staff:") and waiter["profile"] == "harness:claude"
        assert not assigning.done()
        await s.terminals.kill(mine["id"])
        assert (await asyncio.wait_for(assigning, 120))["state"] == "started"
        await s.status_event(ada, "turn_done_unseen", timeout=120)


async def _zero() -> int:
    return 0


# -- a restarted host -----------------------------------------------------------------------------------


async def test_a_restarted_host_takes_the_session_up_and_loses_no_event(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=StubClaude("slow:40")) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        await s.status_event(ada, "working")
        row = await s.session_row(ada)
        # The host goes away mid-turn; the CLI goes on and finishes while nobody listens.
        s.runtime.close()
        await s.terminals.close()

        async def stopped() -> bool:
            return any(e["type"] == "hook" and e["data"].get("name") == "Stop" for e in s.ptyd.events)

        await eventually(stopped, "the turn finished while the host was away")
        assert (await s.session_row(ada)).status == "working"

        s.terminals = terminals_service(db, s.manager, s.root / "run", s.home)
        s.team.app.extensions["terminals"] = s.terminals
        await s.terminals.start()
        assert await s.terminals.wait_available("container")
        runtime = s.restart_runtime(StubClaude("slow:40"))
        assert await runtime.reconcile(wait=5) == 1
        launch = await HarnessStore(db).open_launch_for(row.id)
        assert launch is not None and runtime.adapter.attached == [launch.launch_id]  # type: ignore[attr-defined]
        await s.status_event(ada, "turn_done_unseen")
        assert await s.statuses(ada) == ["starting", "working", "turn_done_unseen"]
        # Still the runtime's: it can be released like any other.
        assert await s.team.release(ada)
        assert await HarnessStore(db).open_launches() == []


async def test_a_restarted_host_ends_what_ended_while_it_was_away(settings: Settings, db: Database) -> None:
    async with stand(settings, db) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        s.runtime.close()
        await s.terminals.close()
        # The CLI is killed while no host watches.
        await s.ptyd.kill_all()
        s.terminals = terminals_service(db, s.manager, s.root / "run", s.home)
        await s.terminals.start()
        assert await s.terminals.wait_available("container")
        runtime = s.restart_runtime()
        assert await runtime.reconcile(wait=5) == 0
        ended = await s.status_event(ada, "exited")
        assert ended.payload["detail"] == "Claude Code ended while the host was away"
        assert await HarnessStore(db).open_launch_for(row.id) is None


async def test_a_post_for_an_ended_launch_moves_nothing(settings: Settings, db: Database) -> None:
    async with stand(settings, db) as s:
        trust(s)
        ada = await s.hire()
        await s.team.assign(ada, await s.task())
        await s.status_event(ada, "turn_done_unseen")
        row = await s.session_row(ada)
        launch = await HarnessStore(db).open_launch_for(row.id)
        assert launch is not None
        token = s.ptyd.launches[launch.launch_id].token
        await s.team.release(ada)
        before = await s.statuses(ada)
        request = urllib.request.Request(
            f"http://127.0.0.1:{s.ptyd.hook_port}/hook/{launch.launch_id}/Stop", data=b"{}", method="POST", headers={"Authorization": f"Bearer {token}"}
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            await asyncio.to_thread(urllib.request.urlopen, request, None, 5)
        assert refused.value.code == 410
        await asyncio.sleep(0.2)
        assert await s.statuses(ada) == before


# -- wiring ---------------------------------------------------------------------------------------------


async def test_runtimes_are_installed_per_adapter_and_say_when_they_cannot_start(settings: Settings, db: Database) -> None:
    async with stand(settings, db) as s:
        runtimes: dict[str, Any] = {}
        made = install_runtimes({"claude": StubClaude}, runtimes, terminals=s.terminals, store=HarnessStore(db), ingress=s.team.ingress, lookup=s.team.live, config=lambda: s.harness)
        try:
            assert list(runtimes) == ["claude"] and runtimes["claude"] is made[0] and made[0].kind == "claude"
            assert isinstance(made[0].adapter, HarnessAdapter)
            assert (await made[0].available("container")).ok
            unavailable = await made[0].available("host")
            assert not unavailable.ok and "host terminal service is not available" in unavailable.reason
            await HarnessStore(db).record_check("container", "claude", install=InstallInfo(True, "claude", "2.1.281", "native"), login=LoginState("no"))
            assert (await made[0].available("container")).reason == "Claude Code is not signed in in the container environment"
        finally:
            for runtime in made:
                runtime.close()


async def test_the_harness_manager_holds_a_launch_back_while_it_updates_the_cli(settings: Settings, db: Database) -> None:
    async with stand(settings, db) as s:
        store = HarnessStore(db)
        await store.record_check("container", "claude", install=InstallInfo(True, "claude", "2.1.281", "native"), login=LoginState("yes"))

        async def nobody(harness: str) -> list[dict[str, Any]]:
            return []

        harness = HarnessManager(store, ports=lambda env: None, environments=lambda: [], config=lambda: s.harness, live_staff=nobody)
        made = install_runtimes({"claude": StubClaude}, {}, terminals=s.terminals, store=store, ingress=s.team.ingress, lookup=s.team.live, config=lambda: s.harness, blocker=harness.launch_blocker)
        try:
            assert (await made[0].available("container")).ok
            harness.operations[("container", "claude")] = Operation(kind="update", env="container", harness="claude", started_at="")
            assert (await made[0].available("container")).reason == "Claude Code is being updated in the container environment"
            del harness.operations[("container", "claude")]
            await store.record_self_check("container", "claude", CheckResult(ok=False, steps=(CheckStep("version", True), CheckStep("hook", False, "no SessionStart")), version="2.1.281", duration_ms=1))
            assert (await made[0].available("container")).reason == "Claude Code's last self-check failed at hook: no SessionStart"
        finally:
            for runtime in made:
                runtime.close()


async def test_the_adapter_test_rig_hands_a_launch_its_hook_posts() -> None:
    """The ports adapter tests use without a host give the same hook stream the runtime gives."""
    async with Rig() as rig:
        (rig.home / ".claude.json").write_text(json.dumps({"projects": {str(rig.work): {"hasTrustDialogAccepted": True}}}))
        launch = await rig.register()
        plan = StubClaude().launch_plan(LaunchSpec(harness="claude", env="container", cwd=str(rig.work), launch_id=launch["launch_id"], first_prompt="go"))
        settings = plan.files["settings.json"].decode()
        term = await rig.spawn(["claude", "--session-id", plan.session_ref, "--settings", settings, "echo:hi"], launch_id=launch["launch_id"])
        names = []
        async with asyncio.timeout(30):
            async for post in term.hooks():
                names.append(post.name)
                if post.name == "Stop":
                    break
        assert names == ["SessionStart", "UserPromptSubmit", "Stop"]
        assert await term.reply("no-such-reply", {"text": "x"}) is False
