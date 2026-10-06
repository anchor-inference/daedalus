"""The stand a scenario plays on: the product's orchestrator of one project, on a database of its own.

Everything the orchestrator touches is the product — the tools as they are declared, the operations
behind them, the board, the team, the file store, the journal, the state block and the wake-up lines
it reads. Two things are not. Staff never run: their runtime is :class:`FakeStaffRuntime`, which
remembers what it was sent, so a member works only as far as a scenario says it did (it hires, starts
a session and reports through the same ingress a real member's report takes). And no turn goes
through the session runner: the scenario's model is called by a plain loop in ``run.py``, so the
stand's session manager exists only for what the orchestrator's office needs of it — the session that
holds the office, the bus, the stores.

The wake queue is stopped as soon as the office exists: a report a scenario publishes must reach the
model through the loop, not through a delivery into the stand's session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from daedalus.config import Settings
from daedalus.extensions.board import Board
from daedalus.extensions.effects import EffectDispatcher
from daedalus.extensions.orchestrator import WAKE_TYPES, Orchestrators
from daedalus.extensions.orchestrator_domain import apply_goal_revision
from daedalus.extensions.staff import Team
from daedalus.extensions.task_launch import TaskLaunchEffect
from daedalus.host import prompts
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.services import SessionServices, locator
from daedalus.host.wake_queue import Batch, Pending
from daedalus.staff_runtime import FakeStaffRuntime, LiveSession, ReadPage
from daedalus.stores.control import ControlStore, Principal
from daedalus.stores.database import Database
from daedalus.stores.executions import ExecutionStore
from daedalus.stores.files import StoredFile
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.projects import FolderSpec, Project
from daedalus.stores.staff import Staff
from tests.support.authorized_launch import operator_assignment
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import ObservedFakeStaffRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings(root: Path) -> Settings:
    os.environ.setdefault("OWNER_USER_ID", "1")
    trigger = root / "rebuild-trigger"
    trigger.mkdir(parents=True, exist_ok=True)
    (trigger / "alive").write_text("")
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        state_dir=root / "state",
        workspaces_dir=root / "workspaces",
        bot_repo_dir=REPO_ROOT,
        core_repo_dir=REPO_ROOT.parent / "protocore-exp",
        owner_user_id=1,
        telegram_bot_token="123:abc",
        rebuild_trigger_dir=trigger,
    )


def repository(root: Path) -> Path:
    """The project's folder: a git repository with a first commit, as a project's primary folder is."""
    repo = root / "tern"
    repo.mkdir(parents=True)
    for args in (["init", "-q", "-b", "main"], ["config", "user.name", "someone"], ["config", "user.email", "someone@example.invalid"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Tern\n\nA note-taking app.\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "start"], cwd=repo, check=True, capture_output=True)
    return repo


def ago(minutes: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


class Harnesses:
    """The harness manager as far as hiring and the orchestrator's view ask it: two command-line agents
    installed in the container, Codex with the operator's pick of two of its models."""

    offered = {"codex": ["gpt-6-luna", "gpt-6-sol"], "claude": ["claude-sonnet-5"]}
    listed = {"codex": ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"], "claude": ["claude-sonnet-5", "claude-opus-5"]}
    labels = {"codex": "Codex", "claude": "Claude Code"}

    async def harnesses(self, env: str) -> list[dict[str, Any]]:
        return [
            {
                "harness": h, "label": self.labels[h], "installed": True, "installed_version": "1.0.0", "logged_in": True, "tested": True, "unavailable": "",
                "models": self.offered[h], "all_models": self.listed[h], "models_chosen": self.offered[h] != self.listed[h], "agents": [],
            }
            for h in self.offered
        ]

    async def catalog(self, env: str, harness: str, folder_id: str | None = None) -> Any:
        from daedalus.harness.contract import Catalog

        modes = ("read-only", "workspace-write", "danger-full-access") if harness == "codex" else ("default", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions")
        return Catalog(agents=(), models=tuple(self.listed.get(harness, [])), modes=modes, efforts=("low", "medium", "high"))

    async def hire_problem(self, env: str, harness: str) -> str:
        return "" if harness in self.offered else f"{harness} is not installed in the {env} environment"

    async def hire_warning(self, env: str, harness: str) -> str:
        return ""

    def capabilities(self, harness: str) -> Any:
        from daedalus.harness.capabilities import capabilities

        return capabilities(harness)


class Notes:
    """The notifications service as far as the stand needs it: what the orchestrator posted."""

    def __init__(self) -> None:
        self.posted: list[Any] = []

    async def post(self, draft: Any) -> None:
        self.posted.append(draft)


@dataclass
class Stand:
    root: Path
    repo: Path
    db: Database
    manager: Any
    team: Team
    board: Board
    orch: Orchestrators
    project: Project
    session_id: str
    runtime: FakeStaffRuntime
    notes: Notes
    mark_seq: int = 0
    background: list[Any] = field(default_factory=list)
    """The execution store and the effect dispatcher's task, released and stopped on close."""
    members: dict[str, Staff] = field(default_factory=dict)
    keep: dict[str, Any] = field(default_factory=dict)
    """What a scenario's setup keeps for its check: the card it is about, the files it attached."""

    # -- building -------------------------------------------------------------------------------

    @classmethod
    async def open(cls, root: Path, *, name: str = "Tern", autonomy: str = "normal", concurrency: int = 4) -> Stand:
        settings = _settings(root)
        db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
        await db.open()
        manager = await _manager(settings, db, ScriptedProvider([]))
        notes = Notes()
        # Launches go through the durable launch command, as in the product: the execution store
        # owns attempts and the effect dispatcher carries the queued launch out.
        executions = ExecutionStore(manager.db)
        executions.acquire()
        await executions.boot()
        app = SimpleNamespace(manager=manager, extensions={}, settings=settings, notifications=notes, db=manager.db,
                              config=manager.config, executions=executions)
        team = Team(app)  # type: ignore[arg-type]
        app.extensions["staff"] = team
        team.attach()
        dispatcher = EffectDispatcher(OutboxStore(manager.db))
        dispatcher.register("task.launch", TaskLaunchEffect(app))
        app.extensions["effects"] = dispatcher
        running = asyncio.create_task(dispatcher.run())
        dispatcher.enable()
        manager.config.staff.launch_stagger_seconds = 0
        # The observed fake reports the execution reference a launch now requires of a runtime.
        runtime = ObservedFakeStaffRuntime(kind="daedalus")
        runtime.manager = manager
        runtime.page = ReadPage("(the member's last reply is in its report)", None, False)
        team.runtimes["daedalus"] = runtime
        board = Board(app)  # type: ignore[arg-type]
        app.extensions["board"] = board
        manager.service_hooks["board"] = board.service
        orch = Orchestrators(app)  # type: ignore[arg-type]
        app.extensions["orchestrator"] = orch
        orch.attach()
        repo = repository(root)
        project = await manager.projects.create(name, [FolderSpec(str(repo))])
        await manager.projects.ensure_roots()
        project = await orch.enable(project.id, autonomy=autonomy)
        await manager.projects.update_orchestrator(project.id, concurrency=concurrency)
        await orch.stop_queue(project.id)
        session_id = project.settings.orchestrator.session_id
        services = SessionServices(session_id=session_id, workspace_dir=root, max_tool_output_chars=20_000, extra={"manager": manager})
        locator.register(services)
        # Peek reads a project folder through the services of the orchestrator's loaded session; a
        # session that never ran has none, so the stand's own stand in for them.
        state = await manager.get_state(session_id)
        if state is not None and state.services is None:
            state.services = services
        project = await manager.projects.get(project.id)
        assert project is not None
        return cls(root, repo, db, manager, team, board, orch, project, session_id, runtime, notes,
                   background=[executions, running])

    async def close(self) -> None:
        executions, running = self.background
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
        self.team.queue.close()
        executions.release()
        locator.unregister(self.session_id)
        await self.manager.close()
        await self.db.close()

    def clis(self) -> None:
        """Command-line agents to hire: Codex and Claude Code, run by the same fake runtime, with a
        terminal to start them in."""
        from tests.unit.test_staff_runtime import Capacity

        self.team.runtimes["codex"] = self.runtime
        self.team.runtimes["claude"] = self.runtime
        self.team._capacity = Capacity()
        self.team.app.extensions["harness"] = Harnesses()

    async def brief(self, section: str, text: str) -> None:
        if section != "goals":
            await self.manager.projects.set_brief(self.project.id, section, text, "operator")
            return
        # The goal is a versioned record the brief only mirrors; it changes through a revision.
        async with self.db.transaction() as conn:
            row = await (await conn.execute("SELECT goal_revision FROM projects WHERE id = ?", (self.project.id,))).fetchone()
            await apply_goal_revision(conn, project_id=self.project.id, expected_goal_revision=int(row["goal_revision"]),
                                      body=text, root_task_ids=[], origin_kind="operator", origin_ref="stand",
                                      control=ControlStore(self.db), principal=Principal.operator({"via": "token", "user_id": 1}))

    async def journal(self, kind: str, text: str, *, author: str = "orchestrator") -> None:
        await self.manager.projects.record(self.project.id, author, kind, text)

    async def hire(self, name: str, role: str, *, one_off: bool = False, harness: str = "daedalus", model: str = "") -> Staff:
        member = await self.manager.staff.hire(self.project.id, name=name, role=role, harness=harness, model=model, isolation="shared", one_off=one_off, created_by="orchestrator")
        self.members[name] = member
        return member

    async def card(
        self,
        title: str,
        *,
        objective: str,
        deliverable: str,
        boundaries: str,
        done_when: str,
        status: str = "todo",
        assignee: str | None = None,
        notes: str = "",
        minutes_ago: float = 30,
        depends_on: list[str] | None = None,
        priority: int = 3,
    ) -> str:
        """A card the orchestrator wrote earlier, as it lies on the board now."""
        task_id = uuid.uuid4().hex[:6]
        member = self.members.get(assignee or "")
        at = ago(minutes_ago)
        await self.db.execute(
            "INSERT INTO board_tasks(id, title, status, priority, depends_on, session_id, origin_session_id, notes, created_at, updated_at, project_id, assignee_staff_id, brief_json)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, title, status, priority, json.dumps(depends_on or []), self.session_id, notes, at, at, self.project.id,
                member.id if member is not None else None,
                json.dumps({"objective": objective, "deliverable": deliverable, "boundaries": boundaries, "done_when": done_when}),
            ),
        )
        # A card on today's board has its first contract version and work step, as Board.add writes
        # them; a launch refuses a card without one.
        brief = {"objective": objective, "deliverable": deliverable, "boundaries": boundaries, "done_when": done_when}
        await self.db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,snapshot_json,created_at)"
                              " VALUES (?,1,'orchestrator',?,?,?)",
                              (task_id, self.session_id, json.dumps({"requirements": [], "checklist": [], "acceptance": "",
                                                                    "depends_on": depends_on or [], "brief": brief}), at))
        await self.db.execute("INSERT INTO workflow_steps(id,task_id,step_kind,state,contract_revision) VALUES (?,?,'work','pending',1)",
                              (f"task:{task_id}:work", task_id))
        return task_id

    async def start(self, name: str, task_id: str) -> LiveSession:
        """The member at work on the card: its session started the way an assignment starts one."""
        member = self.members[name]
        await operator_assignment(self.team, member, task_id)
        live = await self.team.live_of(member)
        assert live is not None, f"{name} did not start"
        # A fake session says nothing after its start; a real one is at work by the time anything
        # it does can be news.
        await self.team.ingress.status(live, "working", actor="orchestrator")
        return (await self.team.live_of(member)) or live

    async def idle(self, name: str) -> None:
        """The member's turn over and seen: free for the next task, as the state block shows it."""
        live = await self.team.live_of(self.members[name])
        if live is not None:
            await self.team.ingress.status(live, "idle", detail="waiting for work")

    async def report(self, name: str, kind: str, text: str, artifacts: list[str] | None = None, operator_steps: dict[str, Any] | None = None) -> str:
        """A member's report through the team's ingress. Steps for the operator go with it only where
        the build takes them; elsewhere a member had no way to send them and the text alone carries them."""
        import inspect

        live = await self.team.live_of(self.members[name])
        assert live is not None, f"{name} has no session to report from"
        # Each report is one tool call of the member's, named as the team tool names it.
        extra: dict[str, Any] = {"call_id": f"stand-report:{uuid.uuid4().hex}"}
        if operator_steps is not None and "operator_steps" in inspect.signature(self.team.ingress.report).parameters:
            extra["operator_steps"] = operator_steps
        return await self.team.ingress.report(live, kind, text, artifacts, **extra)

    async def steered(self, text: str) -> str:
        """An operator's message placed into a running turn, as the build under test words it."""
        hook = getattr(self.orch, "steer_note", None)
        return str(await hook(self.session_id, text)) if hook is not None else text

    async def delivered(self) -> list[str]:
        """What the host put in front of the operator in the orchestrator's chat, beside the model:
        notes the model never reads and the notifications posted."""
        from daedalus.host.prompts import without_turn_context

        texts = [f"{getattr(d, 'title', '')}\n{getattr(d, 'body', '')}" for d in self.notes.posted]
        for message in await self.manager.sessions.list_transcript(self.session_id):
            if message.metadata.get("daedalus.notice"):
                texts.append(without_turn_context("".join(getattr(b, "text", "") for b in message.content_blocks)))
        return texts

    async def attach(self, name: str, data: bytes, mime: str) -> StoredFile:
        """A file the operator attached in the orchestrator's chat: kept by handle in the project's scope."""
        return await self.manager.files.add(data, name=name, mime=mime, origin="operator", origin_ref=self.session_id, scope=self.project.id, actor="operator")

    def write(self, relative: str, text: str) -> None:
        """A file in the project's folder, as the team left it there."""
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    @staticmethod
    def attached(text: str, files: list[StoredFile]) -> str:
        """An operator's message with attachments, as the session runner writes it for an orchestrator."""
        how = "Read one with Peek(op='read', path=…); hand them to staff with Assign or Tell (files=[…])."
        return text.strip() + "\n\nAttached files (kept by handle; " + how + "):\n" + "\n".join(f"- {f.line()}" for f in files)

    # -- what the model reads --------------------------------------------------------------------

    async def mark(self) -> None:
        """From here on, what the project publishes is news for the next wake-up. What the setup did
        before it is the past the recent turns already dealt with: a result it opened is closed as
        decided then, where this build keeps such results."""
        self.mark_seq = self.manager.bus.head
        try:
            await self.db.execute("UPDATE open_loops SET closed_at = ?, closed_by = 'system', decision = 'before the episode' WHERE closed_at IS NULL", (ago(0),))
        except Exception:  # noqa: BLE001 — a build without the register has nothing to close
            pass

    async def news(self) -> str:
        """The wake-up the events since the mark make, rendered by the product: "" when none of them wakes it."""
        found: list[AppEvent] = await self.manager.bus.replay(self.mark_seq, EventFilter(types=WAKE_TYPES, project_id=self.project.id), limit=5000)
        self.mark_seq = self.manager.bus.head
        # Coalesced as the queue does: the latest word about a thing replaces the earlier one.
        items: dict[str, Pending] = {}
        for event in found:
            wake = await self.orch.classify(self.project.id, event)
            if wake is not None:
                items.pop(wake.key, None)
                items[wake.key] = Pending(event, wake, 0.0)
        if not items:
            return ""
        batch = Batch(tuple(items.values()), any(p.wake.urgent for p in items.values()))
        return await self.orch.render(self.project.id, batch)

    async def turn_context(self) -> str:
        project = await self.manager.projects.get(self.project.id)
        assert project is not None
        state = await self.orch.project_state(project, session_id=self.session_id)
        return prompts.turn_context(notes="\n" + state)

    async def turn_ended(self, started_at: str) -> None:
        """What the host does when an orchestrator's turn ends, where this build does anything then."""
        hook = getattr(self.orch, "turn_ended", None)
        if hook is not None:
            await hook(self.project.id, started_at)

    # -- what came of it -------------------------------------------------------------------------

    async def cards(self) -> list[dict[str, Any]]:
        return await self.board.list(None, include_done=True, project_id=self.project.id)

    async def requirements(self, task_id: str) -> list[dict[str, Any]]:
        """The card's requirements where this build keeps them; none where it has no such thing."""
        try:
            rows = await self.db.fetchall("SELECT * FROM task_requirements WHERE task_id = ? ORDER BY number", (task_id,))
        except Exception:  # noqa: BLE001 — a build without requirements has no table for them
            return []
        return [dict(r) for r in rows]

    async def acceptance(self, task_id: str) -> str:
        try:
            row = await self.db.fetchone("SELECT acceptance_state FROM board_tasks WHERE id = ?", (task_id,))
        except Exception:  # noqa: BLE001 — a build without acceptance levels
            return ""
        return str(row["acceptance_state"] or "") if row is not None else ""

    async def open_loops(self) -> list[dict[str, Any]]:
        try:
            rows = await self.db.fetchall("SELECT * FROM open_loops WHERE project_id = ? AND closed_at IS NULL", (self.project.id,))
        except Exception:  # noqa: BLE001 — a build without the register
            return []
        return [dict(r) for r in rows]


__all__ = ["Notes", "Stand", "ago"]
