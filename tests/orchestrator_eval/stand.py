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
from daedalus.extensions.orchestrator import WAKE_TYPES, Orchestrators
from daedalus.extensions.staff import Team
from daedalus.host import prompts
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.services import SessionServices, locator
from daedalus.host.wake_queue import Batch, Pending
from daedalus.staff_runtime import FakeStaffRuntime, LiveSession, ReadPage
from daedalus.stores.database import Database
from daedalus.stores.files import StoredFile
from daedalus.stores.projects import FolderSpec, Project
from daedalus.stores.staff import Staff
from tests.unit.test_session_runner import ScriptedProvider, _manager

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
        app = SimpleNamespace(manager=manager, extensions={}, settings=settings, notifications=notes, db=manager.db, config=manager.config)
        team = Team(app)  # type: ignore[arg-type]
        app.extensions["staff"] = team
        team.attach()
        manager.config.staff.launch_stagger_seconds = 0
        runtime = FakeStaffRuntime(kind="daedalus")
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
        return cls(root, repo, db, manager, team, board, orch, project, session_id, runtime, notes)

    async def close(self) -> None:
        locator.unregister(self.session_id)
        await self.manager.close()
        await self.db.close()

    async def brief(self, section: str, text: str) -> None:
        await self.manager.projects.set_brief(self.project.id, section, text, "operator")

    async def journal(self, kind: str, text: str, *, author: str = "orchestrator") -> None:
        await self.manager.projects.record(self.project.id, author, kind, text)

    async def hire(self, name: str, role: str, *, one_off: bool = False, harness: str = "daedalus") -> Staff:
        member = await self.manager.staff.hire(self.project.id, name=name, role=role, harness=harness, isolation="shared", one_off=one_off, created_by="orchestrator")
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
        return task_id

    async def start(self, name: str, task_id: str) -> LiveSession:
        """The member at work on the card: its session started the way an assignment starts one."""
        member = self.members[name]
        await self.team.assign(member, task_id, by="orchestrator")
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

    async def report(self, name: str, kind: str, text: str, artifacts: list[str] | None = None) -> str:
        live = await self.team.live_of(self.members[name])
        assert live is not None, f"{name} has no session to report from"
        return await self.team.ingress.report(live, kind, text, artifacts)

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

    def mark(self) -> None:
        """From here on, what the project publishes is news for the next wake-up."""
        self.mark_seq = self.manager.bus.head

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
