"""Files handed along orchestration by handle, and the bytes put where each agent can open them.

The live defect this answers: a project whose folders are all on the host; the operator attached a
spec in its orchestrator's chat; it was kept in the orchestrator's container inbox and the model was
told that path; Peek refused it as outside the project's folders; the orchestrator wrote the container
path into a brief for a Claude Code member on the host, who had no such path at all. Here the same
chain runs end to end — the operator, the main orchestrator, the project orchestrator, a command-line
member in a host terminal (a terminal daemon running real processes, a fake Claude) — and at every
step the file is where that step can open it, under one handle.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import MessageRole, TextBlock, ToolResultBlock

from daedalus.config import Settings, TerminalsConfig
from daedalus.extensions.api import build_app
from daedalus.extensions.board_commands import BoardCommands
from daedalus.extensions.coordinator_authority import approve_authority
from daedalus.extensions.dispatcher import Dispatcher
from daedalus.extensions.dispatcher_projects import ProjectMaker
from daedalus.extensions.dispatches import Dispatches
from daedalus.extensions.harness import catalog_roots
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.harness.runtime import CliStaffRuntime
from daedalus.host.handoff import Handoff
from daedalus.host.session_runner import Attachment
from daedalus.stores import files as files_module
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.database import Database
from daedalus.stores.files import MAIN, FileRefused
from daedalus.stores.harness import HarnessStore
from daedalus.stores.projects import FolderSpec, Project
from daedalus.stores.staff import Staff
from daedalus.terminals.owners import ManagerOwners
from daedalus.terminals.service import Terminals
from tests.support import fake_cli
from tests.support.authorized_launch import operator_assignment
from tests.support.fake_cli.tui import read_log
from tests.support.live_ptyd import LivePtyd
from tests.support.waiting import until_await
from tests.unit.test_cli_staff_runtime import harness_config
from tests.unit.test_orchestrator import Rig, _idle, approve_coordinator, events, events_messages, rig
from tests.unit.test_session_runner import ScriptedProvider
from tests.unit.test_staff_runtime import board_task, close_team

SPEC = "# Подсказки free/pro\nFree users get three hints a day; pro users get all of them.\n"
SPEC_NAME = "подсказки-free-pro.md"
Args = Callable[[], Awaitable[dict[str, Any]]]


class Routed(ScriptedProvider):
    """One model for the main orchestrator, the project's orchestrator and its staff, each reading its
    own script: they run concurrently (a dispatch wakes the project while the main orchestrator is
    still finishing its turn), so one shared list would hand each the other's steps. A step's
    ``args`` may be a coroutine function, for handles that exist only once the turn before made them."""

    def __init__(self) -> None:
        super().__init__([])
        self.scripts: dict[str, list[dict[str, Any]]] = {"main": [], "project": [], "staff": []}

    @staticmethod
    def role(request: Any) -> str:
        system = "".join(b.text for m in request.messages if m.role is MessageRole.system for b in m.content_blocks if isinstance(b, TextBlock))
        if "You are the main orchestrator" in system:
            return "main"
        if "You are the orchestrator of one project" in system:
            return "project"
        return "staff"

    async def stream_with_tools(self, request: Any) -> AsyncIterator[Any]:
        script = self.scripts[self.role(request)]
        step = script.pop(0) if script else {"text": "Nothing more to do."}
        if callable(step.get("args")):
            step = {**step, "args": await step["args"]()}
        self.script = [step]
        async for delta in super().stream_with_tools(request):
            yield delta


@dataclass
class Chain:
    r: Rig
    model: Routed
    main: Dispatcher
    dispatches: Dispatches
    ptyd: LivePtyd
    terminals: Terminals
    root: Path
    home: Path
    work: Path
    log: Path
    project: Project

    @property
    def manager(self) -> Any:
        return self.r.manager

    async def langpt(self) -> Staff:
        return await self.manager.staff.hire(self.project.id, name="langpt", harness="claude", env="host", isolation="shared")

    async def handle(self, scope: str, *, origin: str = "operator") -> str:
        listed = [f for f in await self.manager.files.listing(scope) if f.origin == origin]
        assert listed, f"no {origin} file in {scope}"
        return listed[0].handle

    def opened(self) -> list[dict[str, Any]]:
        return [e for e in read_log(self.log) if e["event"] == "opened"]


@asynccontextmanager
async def chain(settings: Settings, db: Database, tmp_path: Path) -> AsyncIterator[Chain]:
    """The main orchestrator, a host-only project "Work" with its orchestrator, and a host terminal
    daemon for its command-line staff — seen from a container installation."""
    r = await rig(settings, db, tmp_path)
    if r.manager.projects.local_env != "container":
        await close_team(r.manager)
        await r.manager.close()
        pytest.skip("a host folder is out of reach only in a container installation")
    model = Routed()
    r.manager.providers.rungs_for = lambda config, preset=None: [(model, "scripted-model")]  # type: ignore[method-assign]
    app = r.team.app
    dispatches = Dispatches(app)
    app.extensions["dispatches"] = dispatches
    r.manager.asks.default_dispatch = dispatches.default_dispatch
    r.orch.state_sections.append(dispatches.state_section)
    main = Dispatcher(app)
    app.extensions["dispatcher"] = main
    main.attach()
    maker = ProjectMaker(app, main)
    app.extensions["dispatcher_projects"] = maker
    maker.attach()
    # A short directory: the daemon's socket path must fit the kernel's limit.
    root = Path(tempfile.mkdtemp(prefix="handoff-"))
    home, work, bin_dir, log = root / "home", root / "work", root / "bin", root / "fake-cli.jsonl"
    home.mkdir()
    work.mkdir()
    fake_cli.install(bin_dir)
    ptyd = LivePtyd(root / "run", home=home, bin_dir=bin_dir, env="host", base_env={"FAKE_CLI_LOG": str(log), "FAKE_CLI_TIME_SCALE": "0.05"}, input_idle_ms=0)
    await ptyd.start()
    terminals = Terminals(
        db, run_dirs={"container": None, "host": root / "run"},
        config=lambda: TerminalsConfig(running_cap=20, kill_grace_ms=300, agent_launch_wait_seconds=60), owners=ManagerOwners(r.manager), bus=r.manager.bus,
    )
    terminals.set_extra_roots("host", "harness-catalog", catalog_roots(str(home)))
    runtime = None
    try:
        project = await r.manager.projects.create("Work", [FolderSpec(str(work), env="host")])
        await r.manager.projects.ensure_roots()
        await terminals.start()
        assert await terminals.wait_available("host")
        ptyd.allow_root(work)
        app.extensions["terminals"] = terminals
        app.extensions["host_bridge"] = r.team.host_bridge
        cfg = harness_config()
        runtime = CliStaffRuntime(ClaudeCodeAdapter(), terminals=terminals, store=HarnessStore(db), ingress=r.team.ingress, lookup=r.team.live, config=lambda: cfg)
        r.team.runtimes["claude"] = runtime
        (home / ".claude.json").write_text(json.dumps({"projects": {str(work): {"hasTrustDialogAccepted": True}}}))
        project = await r.orch.enable(project.id)
        coordinator = project.settings.orchestrator.session_id
        operator = Principal.operator({"via": "token", "user_id": 1})
        scope = Scope("project", project.id)
        expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        for bundle_id in ("planning", "execution_project", "review"):
            revision = await ControlStore(db).revision(scope, Entity("project", project.id))
            await approve_authority(app, project.id, operator,
                                    client_operation_id=f"handoff-grant:{uuid.uuid4().hex}",
                                    expected_entity_revision=revision,
                                    expected_coordinator_session_id=coordinator,
                                    bundle_id=bundle_id, expires_at=expires_at)
        yield Chain(r, model, main, dispatches, ptyd, terminals, root, home, work, log, project)
    finally:
        if runtime is not None:
            runtime.close()
        await terminals.close()
        await close_team(r.manager)
        await r.manager.close()
        await ptyd.stop()
        shutil.rmtree(root, ignore_errors=True)


def staging(tmp_path: Path, name: str = SPEC_NAME, body: str = SPEC) -> Attachment:
    """What the upload route hands ``submit``: a staged file with the operator's name for it."""
    path = tmp_path / f"staged-{os.urandom(4).hex()}.part"
    path.write_text(body, encoding="utf-8")
    return Attachment(path=path, name=name, mime_type="text/markdown")


async def user_texts(manager: Any, session_id: str) -> list[str]:
    out = []
    for message in await manager.sessions.list_transcript(session_id):
        if message.role is MessageRole.user:
            out.append("".join(b.text for b in message.content_blocks if isinstance(b, TextBlock)))
    return out


async def tool_results(manager: Any, session_id: str) -> list[str]:
    out = []
    for message in await manager.sessions.list_transcript(session_id):
        for block in message.content_blocks:
            if isinstance(block, ToolResultBlock):
                out.append(block.content)
    return out


def brief_task(objective: str) -> dict[str, str]:
    return {"objective": objective, "deliverable": "an estimate in estimate.md", "boundaries": "read only; change no code", "done_when": "the estimate is reported"}


# -- the live case, end to end --------------------------------------------------------------------------


async def test_an_attachment_in_a_host_projects_chat_reaches_its_host_member_where_it_can_open_it(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The operator attaches a spec in the orchestrator's chat of a host-only project; the orchestrator
    reads it by handle and assigns it to a Claude Code member on the host; the file is written into the
    member's host-side inbox before the brief, the brief names that path, and the member opens it.
    The member's estimate comes back as a project file, and the orchestrator passes it to the operator."""
    async with chain(settings, db, tmp_path) as c:
        sid = c.project.settings.orchestrator.session_id
        langpt = await c.langpt()
        (c.work / "estimate.md").write_text("Estimate: two days.\n")
        task_id = await board_task(c.manager, c.project, "Estimate the hints spec", brief={})
        # The fake model reads ";"-separated steps from the brief: open the handed file, then report
        # done with the estimate as an artifact.
        brief = brief_task(f"Estimate the spec;cat:.agents/inbox/{task_id}/{SPEC_NAME};report:done:estimated|estimate.md;")
        scope = Scope("project", c.project.id)
        revision = await ControlStore(db).revision(scope, Entity("task", task_id))
        await BoardCommands(db).update(Principal.operator({"via": "token", "user_id": 1}), scope, task_id,
                                       client_operation_id=f"handoff-brief:{uuid.uuid4().hex}",
                                       expected_entity_revision=revision, brief=brief)

        async def peek_args() -> dict[str, Any]:
            return {"op": "read", "path": await c.handle(c.project.id)}

        async def assign_args() -> dict[str, Any]:
            return {"staff": "langpt", "task_id": task_id, "files": [await c.handle(c.project.id)],
                    "expected_entity_revision": await ControlStore(db).revision(scope, Entity("task", task_id))}

        async def report_args() -> dict[str, Any]:
            return {"text": "langpt estimated the hints spec", "kind": "done", "files": [await c.handle(c.project.id, origin="staff")]}

        c.model.scripts["project"] += [
            {"tool": "Peek", "args": peek_args},
            {"tool": "Assign", "args": assign_args},
            {"text": "Handed to langpt."},
        ]
        await c.manager.submit(sid, "Передай langpt, пусть оценит", [staging(tmp_path)])

        # The chat names the handle, never a path of the container.
        said = (await user_texts(c.manager, sid))[0]
        handle = await c.handle(c.project.id)
        assert handle in said and SPEC_NAME in said
        assert "/inbox/" not in said and str(settings.workspaces_dir) not in said

        async def delivered() -> bool:
            return bool(c.opened())

        await until_await(delivered, "the member opened the handed file")
        target = c.work / ".agents" / "inbox" / task_id / SPEC_NAME
        assert target.read_text(encoding="utf-8") == SPEC, "the bytes, on the host side, in the member's own folder"
        assert (c.work / ".agents" / "inbox" / ".gitignore").read_bytes() == b"*\n"
        [opened] = c.opened()
        assert opened["path"] == str(target) and opened["first"] == "# Подсказки free/pro"
        # The orchestrator read it through Peek by its handle.
        results = await tool_results(c.manager, sid)
        assert any("Free users get three hints a day" in r for r in results), results
        assert any("admission in progress" in r and "files are copied to them when they start" in r.lower()
                   for r in results), results
        # The brief names the member-local path, which exists there, and nothing of the container.
        first = (await c.manager.staff.messages(langpt.id))[0]
        assert str(target) in first.text and handle in first.text
        assert str(settings.workspaces_dir) not in first.text
        # The move is written down: attached by the operator, delivered to the host path.
        transfers = await c.manager.files.transfers(handle.removeprefix("att:"))
        assert [(t["action"], t["env"]) for t in transfers] == [("attached", ""), ("delivered", "host")]
        assert transfers[1]["target"] == str(target) and transfers[1]["size"] == len(SPEC.encode())
        written = await db.fetchall("SELECT detail_json FROM terminal_audit WHERE action = 'write'")
        assert any(json.loads(w["detail_json"])["path"] == str(target) for w in written), "the daemon's write is audited"

        # The member's artifact comes back as the project's file, named in the orchestrator's wake-up.
        c.model.scripts["project"] += [{"tool": "ProjectReport", "args": report_args}, {"text": "Reported."}]

        async def reported() -> bool:
            return any(e.payload.get("files") for e in await events(c.manager, "staff.report"))

        await until_await(reported, "the member's report brought its artifact back")
        [report] = [e for e in await events(c.manager, "staff.report") if e.payload.get("files")]
        estimate = report.payload["files"][0]
        assert estimate["name"] == "estimate.md" and estimate["size"] == len("Estimate: two days.\n")
        stored = await c.manager.files.get(estimate["id"])
        assert stored is not None and (await c.manager.files.read(stored)) == b"Estimate: two days.\n"

        async def forwarded() -> bool:
            return MAIN in await c.manager.files.scopes(estimate["id"])

        await until_await(forwarded, "the orchestrator passed the estimate on")
        batches = await events_messages(c.manager, sid)
        assert any(f"att:{estimate['id']} estimate.md" in b for b in batches), batches
        await until_await(lambda: _idle(c.manager, sid), "the orchestrator's turn ended")


async def test_the_full_chain_from_the_main_chat_to_a_host_member_keeps_one_handle(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The operator sends the spec to the main orchestrator; it reads it with Files, delegates it to
    Work; Work's orchestrator is woken with the same handle, reads it and assigns it to its member on
    the host, who opens it. A follow-up on the same dispatch brings a second file under its handle."""
    async with chain(settings, db, tmp_path) as c:
        main_sid = await c.main.ensure()
        project_sid = c.project.settings.orchestrator.session_id
        await c.langpt()
        task_id = await board_task(c.manager, c.project, "Estimate the hints spec", brief={})
        brief = brief_task(f"Estimate the spec;cat:.agents/inbox/{task_id}/{SPEC_NAME};echo:read it;")
        scope = Scope("project", c.project.id)
        revision = await ControlStore(db).revision(scope, Entity("task", task_id))
        await BoardCommands(db).update(Principal.operator({"via": "token", "user_id": 1}), scope, task_id,
                                       client_operation_id=f"handoff-brief:{uuid.uuid4().hex}",
                                       expected_entity_revision=revision, brief=brief)

        async def read_args() -> dict[str, Any]:
            return {"op": "read", "file": await c.handle(MAIN)}

        async def delegate_args() -> dict[str, Any]:
            return {"project": "Work", "text": "Оценить ТЗ по подсказкам free/pro; langpt", "title": "Hints spec", "files": [await c.handle(MAIN)]}

        async def peek_args() -> dict[str, Any]:
            return {"op": "read", "path": await c.handle(c.project.id)}

        async def assign_args() -> dict[str, Any]:
            return {"staff": "langpt", "task_id": task_id, "files": [await c.handle(c.project.id)],
                    "expected_entity_revision": await ControlStore(db).revision(scope, Entity("task", task_id))}

        c.model.scripts["main"] += [{"tool": "Files", "args": read_args}, {"tool": "Delegate", "args": delegate_args}, {"text": "Handed to Work."}]
        c.model.scripts["project"] += [{"tool": "Peek", "args": peek_args}, {"tool": "Assign", "args": assign_args}, {"text": "Handed to langpt."}]
        await c.manager.submit(main_sid, "Это для Work, пусть langpt оценит", [staging(tmp_path)])

        async def opened() -> bool:
            return bool(c.opened())

        await until_await(opened, "the member on the host opened the file the operator gave the main orchestrator")
        handle = await c.handle(MAIN)
        # One handle all along: the main orchestrator's file is the project's under the same id.
        assert await c.handle(c.project.id) == handle
        assert set(await c.manager.files.scopes(handle.removeprefix("att:"))) == {MAIN, c.project.id}
        main_said = (await user_texts(c.manager, main_sid))[0]
        assert handle in main_said and "/inbox/" not in main_said
        main_results = await tool_results(c.manager, main_sid)
        assert any("Free users get three hints a day" in r for r in main_results), "the main orchestrator read its attachment"
        assert any("now the project's under the same handle" in r for r in main_results), main_results
        batches = await events_messages(c.manager, project_sid)
        assert any(f"{handle} {SPEC_NAME}" in b and "[from the main orchestrator] dispatch" in b for b in batches), batches
        target = c.work / ".agents" / "inbox" / task_id / SPEC_NAME
        assert c.opened()[0]["path"] == str(target) and c.opened()[0]["first"] == "# Подсказки free/pro"

        # A follow-up on the open dispatch with a second file: the project is woken with its handle.
        [dispatch] = await c.manager.dispatches.recent(project_id=c.project.id, limit=5)
        extra = await c.manager.files.add(b"pro: 50 hints\n", name="limits.txt", origin="operator", scope=MAIN, actor="operator")
        said = await c.main.service("delegate", session_id=main_sid, project="Work", text="Limits attached", dispatch_id=dispatch.id, files=[extra.handle])
        assert f"added to dispatch {dispatch.id}" in said
        assert c.project.id in await c.manager.files.scopes(extra.id)

        async def followed() -> bool:
            return any(extra.handle in b and "follow-up on dispatch" in b for b in await events_messages(c.manager, project_sid))

        await until_await(followed, "the follow-up woke the project with the second file's handle")


async def test_a_new_project_starts_with_the_files_the_main_orchestrator_was_given(settings: Settings, db: Database, tmp_path: Path) -> None:
    async with chain(settings, db, tmp_path) as c:
        main_sid = await c.main.ensure()
        spec = await c.manager.files.add(SPEC.encode(), name=SPEC_NAME, origin="operator", scope=MAIN, actor="operator")
        said = await c.main.service("create_project", session_id=main_sid, name="Hints", goal="hints for free and pro users", files=[spec.handle])
        assert "asked the operator to confirm" in said
        row = await db.fetchone("SELECT id FROM asks WHERE kind = 'project' AND resolved_at IS NULL")
        ask = await c.manager.asks.get(row["id"])
        assert ask is not None and SPEC_NAME in ask.text and ask.detail["files"] == [spec.id]
        maker: Any = c.r.team.app.extensions["dispatcher_projects"]
        answer = await maker.answer_own(ask, allow=True, text=None, selected=None, by="operator", via="app")
        assert answer["delivered"], answer["error"]
        project = next(p for p in await c.manager.projects.list() if p.name == "Hints")
        async def shared() -> bool:
            return project.id in await c.manager.files.scopes(spec.id)

        await until_await(shared, "the approved project received its file handle")
        assert set(await c.manager.files.scopes(spec.id)) == {MAIN, project.id}
        created = [e for e in await events(c.manager, "dispatch.created") if e.project_id == project.id]
        assert created and created[0].payload["files"][0]["id"] == spec.id
        with pytest.raises(ValueError, match="nobody would receive them"):
            await c.main.service("create_project", session_id=main_sid, name="Quiet", start_orchestrator=False, files=[spec.handle])


# -- a Daedalus member in the container, and what comes back -------------------------------------------


async def test_a_daedalus_member_in_the_container_gets_a_copy_in_its_worktree_and_reports_a_file_back(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        model = Routed()
        r.manager.providers.rungs_for = lambda config, preset=None: [(model, "scripted-model")]  # type: ignore[method-assign]
        project = await r.orch.enable(r.project.id)
        sid = project.settings.orchestrator.session_id
        await approve_coordinator(r, sid)
        ira = await r.manager.staff.hire(r.project.id, name="Ira", role="Menu")
        await r.manager.submit(sid, "Для Иры", [staging(tmp_path)])
        await until_await(lambda: _idle(r.manager, sid), "the orchestrator's turn ended")
        handle = (await r.manager.files.listing(r.project.id))[0].handle
        model.scripts["staff"] += [
            {"tool": "Write", "args": {"path": "estimate.md", "content": "Two days.\n"}},
            {"tool": "Report", "args": {"kind": "checkpoint", "note": "estimate written", "artifacts": ["estimate.md", "https://example.invalid/page", "agent/ira/branch", "missing.md"]}},
            {"text": "Done."},
        ]
        said = await r.call(sid, "assign", staff="Ira", title="Estimate", objective="Estimate the spec", deliverable="estimate.md in the worktree",
                            boundaries="Touch nothing else", done_when="estimate.md exists", files=[handle])
        assert "Ira will start" in said and "admission in progress" in said
        live = await r.team.live_of(ira)
        assert live is not None and live.session.worktree_path
        state = await r.manager.get_state(live.session.session_id)
        assert state is not None
        target = Path(state.workspace) / ".agents" / "inbox" / live.session.task_id / SPEC_NAME
        assert target.read_text(encoding="utf-8") == SPEC
        first = (await r.manager.staff.messages(ira.id))[0]
        assert str(target) in first.text
        # Git does not see the inbox: the worktree's own .gitignore keeps it out.
        from tests.unit.test_orchestrator import git

        assert ".agents" not in git(Path(live.session.worktree_path), "status", "--porcelain")

        async def reported() -> bool:
            return any(e.payload.get("files") for e in await events(r.manager, "staff.report", staff_id=ira.id))

        await until_await(reported, "the member's artifact came back")
        [report] = [e for e in await events(r.manager, "staff.report", staff_id=ira.id) if e.payload.get("files")]
        assert [f["name"] for f in report.payload["files"]] == ["estimate.md"], "links, branches and missing names stay plain refs"
        assert report.payload["refs"][1] == "https://example.invalid/page"
        kept = await r.manager.files.get(report.payload["files"][0]["id"])
        assert kept is not None and kept.origin == "staff" and await r.manager.files.read(kept) == b"Two days.\n"
        # The orchestrator can hand the member's file on to someone else, under the same handle.
        assert (await r.manager.files.in_scope(kept.handle, r.project.id)).id == kept.id
        await until_await(lambda: _idle(r.manager, live.session.session_id), "the member's turn ended")
        results = await tool_results(r.manager, live.session.session_id)
        assert any("reported checkpoint; report" in t and "kept for the team: estimate.md" in t
                   for t in results), results
        await until_await(lambda: _idle(r.manager, sid), "the orchestrator's turn ended")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_peek_reads_the_orchestrators_own_inbox_and_its_kept_files_and_lists_them(settings: Settings, db: Database, tmp_path: Path) -> None:
    """What the live orchestrator tried: a file its own inbox held, by the absolute path it was told.
    A host project's orchestrator reads its own working directory; and a handle, and the list."""
    async with chain(settings, db, tmp_path) as c:
        sid = c.project.settings.orchestrator.session_id
        state = await c.manager.get_state(sid)
        assert state is not None
        inbox = Path(state.workspace) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / SPEC_NAME).write_text(SPEC, encoding="utf-8")
        read = await c.r.call(sid, "peek", op="read", path=str(inbox / SPEC_NAME))
        assert "Free users get three hints a day" in read
        with pytest.raises(Refused, match="outside the project's folders"):
            await c.r.call(sid, "peek", op="read", path=str(settings.state_dir / "config.toml"))
        assert "has no files yet" in await c.r.call(sid, "peek", op="files")
        kept = await c.manager.files.add(SPEC.encode(), name=SPEC_NAME, origin="operator", scope=c.project.id, actor="operator")
        listed = await c.r.call(sid, "peek", op="files")
        assert kept.handle in listed and SPEC_NAME in listed
        assert "Free users" in await c.r.call(sid, "peek", op="read", path=kept.handle)
        binary = await c.manager.files.add(b"\x00\x01\x02", name="blob.bin", origin="operator", scope=c.project.id, actor="operator")
        with pytest.raises(Refused, match="binary file"):
            await c.r.call(sid, "peek", op="read", path=binary.handle)
        other = await c.manager.files.add(b"x", name="other.txt", origin="operator", scope=MAIN, actor="operator")
        with pytest.raises(Refused, match="is not one of this project's files"):
            await c.r.call(sid, "peek", op="read", path=other.handle)


# -- refusals --------------------------------------------------------------------------------------------


async def test_refusals_are_said_before_anything_is_sent(settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with chain(settings, db, tmp_path) as c:
        sid = c.project.settings.orchestrator.session_id
        langpt = await c.langpt()
        # A handle of another scope, and a path outside every folder of the project.
        other = await c.manager.files.add(b"x", name="other.txt", origin="operator", scope=MAIN, actor="operator")
        common = {"staff": "langpt", "title": "Look", "objective": "Look at the files", "deliverable": "a short note", "boundaries": "read only, nothing else", "done_when": "the note is reported"}
        with pytest.raises(Refused, match="is not one of this project's files"):
            await c.r.call(sid, "assign", files=[other.handle], **common)
        with pytest.raises(Refused, match="not in any of Work's folders"):
            await c.r.call(sid, "assign", files=["/etc/hostname"], **common)
        with pytest.raises(Refused, match="home directory"):
            await c.r.call(sid, "assign", files=["~/.ssh/id_ed25519"], **common)
        assert not await c.manager.db.fetchall("SELECT id FROM board_tasks WHERE title = 'Look'"), "nothing written before the check"
        # Too large: refused when kept, with the limit named.
        monkeypatch.setattr(files_module, "FILE_MAX_BYTES", 16)
        with pytest.raises(FileRefused, match="at most 16 B"):
            await c.manager.files.add(b"x" * 17, name="big.bin", origin="operator", scope=c.project.id, actor="operator")
        (c.work / "big.log").write_bytes(b"y" * 32)
        monkeypatch.setattr("daedalus.host.handoff.FILE_MAX_BYTES", 16)
        with pytest.raises(Refused, match="at most 16 B"):
            await c.r.call(sid, "assign", files=[str(c.work / "big.log")], **common)
        # An attachment too large is not kept and the chat says so; the upload is not lost silently.
        body, _ = await c.manager._keep_attachments(await c.manager.get_state(sid), "big", [staging(tmp_path, "huge.md", "z" * 64)], c.project.id)
        assert "huge.md: not kept" in body and "at most 16 B" in body
        # A path in the project's folder is taken in as a new handle.
        monkeypatch.setattr(files_module, "FILE_MAX_BYTES", 50 << 20)
        monkeypatch.setattr("daedalus.host.handoff.FILE_MAX_BYTES", 50 << 20)
        (c.work / "notes.txt").write_text("notes\n")
        [taken] = await c.r.team.handoff.resolve([str(c.work / "notes.txt")], project=c.project, actor="orchestrator")
        assert taken.origin == "folder" and await c.manager.files.read(taken) == b"notes\n"
        # The bridge gone: nothing is handed to a host member, and the refusal says who to ask.
        monkeypatch.setattr(c.r.team.host_bridge, "available", lambda: False)
        task_id = await board_task(c.manager, c.project, "Later", brief={"objective": "read the notes", "deliverable": "a summary", "boundaries": "read only", "done_when": "summary reported"})
        await c.manager.files.attach_to_task(task_id, [taken], actor="orchestrator")
        queued = await operator_assignment(c.r.team, langpt, task_id, wait_for_admission=False)

        async def launch_uncertain() -> bool:
            row = await c.manager.db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (queued["effect_id"],))
            return row is not None and row["state"] == "unknown"

        await until_await(launch_uncertain, "the unavailable host bridge stayed unresolved in its receipt")
        failure = await c.manager.db.fetchone("SELECT error FROM effect_outbox WHERE id = ?", (queued["effect_id"],))
        assert failure is not None and "host terminal bridge" in failure["error"]
        assert c.opened() == []


async def test_an_old_host_daemon_without_fs_write_is_refused_with_how_to_update(settings: Settings, db: Database, tmp_path: Path) -> None:
    async with chain(settings, db, tmp_path) as c:
        async def unknown(params: dict[str, Any]) -> dict[str, Any]:
            from tests.support.fake_ptyd import _RpcFail

            raise _RpcFail(-32601, "method not found: fs.write")

        c.ptyd._m_fs_write = unknown  # type: ignore[method-assign]
        stored = await c.manager.files.add(b"x", name="a.txt", origin="operator", scope=c.project.id, actor="operator")
        with pytest.raises(FileRefused, match="update it"):
            await c.r.team.handoff.deliver([stored], env="host", cwd=str(c.work), box="t1", actor="orchestrator")
        assert [t["action"] for t in await c.manager.files.transfers(stored.id)] == ["attached", "refused"]


async def test_a_read_only_folder_takes_no_files(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        folder = r.project.primary
        assert folder is not None
        await r.manager.projects.update_folder(r.project.id, folder.id, readonly=True)
        project = await r.manager.projects.get(r.project.id)
        assert project is not None
        handoff = Handoff(r.manager.files, local_env=r.manager.projects.local_env, host=lambda: None)
        with pytest.raises(FileRefused, match="read-only"):
            handoff.check_target(project.primary)  # type: ignore[arg-type]
        with pytest.raises(FileRefused, match="no host terminal bridge|has none"):
            handoff.host()
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- the live task, recovered after the deploy ----------------------------------------------------------


async def test_an_open_brief_naming_the_orchestrators_inbox_gets_its_file_once(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The live task's brief named ``…/project-<id>/inbox/<file>`` of its orchestrator's container
    home. At the first start of this version the file becomes the project's and goes with the task;
    a path anywhere else is not taken, and the repair runs once."""
    async with chain(settings, db, tmp_path) as c:
        sid = c.project.settings.orchestrator.session_id
        state = await c.manager.get_state(sid)
        assert state is not None
        inbox = Path(state.workspace) / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / SPEC_NAME).write_text(SPEC, encoding="utf-8")
        outside = settings.state_dir / "inbox"
        outside.mkdir(parents=True, exist_ok=True)
        (outside / "secret.md").write_text("no\n")
        langpt = await c.langpt()
        named = f"Исходный документ (прочитать целиком): {inbox / SPEC_NAME}. И ещё {outside / 'secret.md'}"
        task_id = await board_task(c.manager, c.project, "Оценка", brief={"objective": "оценить ТЗ;cat:.agents/inbox/{}/{};echo:ok;".format("TASK", SPEC_NAME), "deliverable": "отчёт оркестратору", "boundaries": named, "done_when": "отчёт с оценкой"})
        brief = json.loads((await db.fetchone("SELECT brief_json FROM board_tasks WHERE id = ?", (task_id,)))["brief_json"])
        brief["objective"] = brief["objective"].replace("TASK", task_id)
        scope = Scope("project", c.project.id)
        revision = await ControlStore(db).revision(scope, Entity("task", task_id))
        await BoardCommands(db).update(Principal.operator({"via": "token", "user_id": 1}), scope, task_id,
                                       client_operation_id=f"recover-brief:{uuid.uuid4().hex}",
                                       expected_entity_revision=revision,
                                       brief={"objective": brief["objective"]}, assignee_staff_id=langpt.id)

        assert await c.r.team.recover_named_files() == 1
        [kept] = await c.manager.files.of_task(task_id)
        assert kept.name == SPEC_NAME and await c.manager.files.read(kept) == SPEC.encode()
        assert await c.r.team.recover_named_files() == 0, "once"
        # Its next start hands it over with the brief.
        launched = await operator_assignment(c.r.team, langpt, task_id)
        assert launched["state"] == "queued"

        async def opened() -> bool:
            return bool(c.opened())

        await until_await(opened, "the member opened the recovered file")
        assert c.opened()[0]["first"] == "# Подсказки free/pro"


# -- the routes the app draws file cards from ------------------------------------------------------------


async def test_the_app_reads_a_file_by_handle_downloads_it_and_sees_its_audit(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id)
        sid = project.settings.orchestrator.session_id
        app = SimpleNamespace(settings=settings, config=r.manager.config, db=db, manager=r.manager, front=None, extensions={}, guard=None, create_session=r.manager.create_session)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            headers = {"X-Daedalus-Token": "tok"}
            sent = await client.post(f"/api/sessions/{sid}/upload", headers=headers, data={"text": "the spec"}, files={"files": (SPEC_NAME, SPEC.encode(), "text/markdown")})
            assert sent.status_code == 200, sent.text
            assert sent.json()["files"] == [SPEC_NAME]
            [stored] = await r.manager.files.listing(r.project.id)
            said = (await user_texts(r.manager, sid))[0]
            assert stored.handle in said and "/inbox/" not in said
            listed = (await client.get(f"/api/files?ids={stored.handle},deadbeef0000", headers=headers)).json()
            assert [f["id"] for f in listed["files"]] == [stored.id] and listed["files"][0]["name"] == SPEC_NAME
            body = await client.get(f"/api/files/{stored.id}/download?path={SPEC_NAME}&token=tok")
            assert body.status_code == 200 and body.content == SPEC.encode() and "inline" in body.headers["content-disposition"]
            detail = (await client.get(f"/api/files/{stored.id}", headers=headers)).json()
            assert detail["scopes"] == [r.project.id] and [t["action"] for t in detail["transfers"]] == ["attached"]
            assert (await client.get("/api/files/000000000000/download?token=tok")).status_code == 404
            assert (await client.get(f"/api/files/{stored.id}/download")).status_code == 401
            projected = (await client.get(f"/api/projects/{r.project.id}/files", headers=headers)).json()
            assert [f["handle"] for f in projected["files"]] == [stored.handle]
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_local_delivery_is_atomic_named_by_its_bytes_and_never_follows_a_link_out(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        handoff = Handoff(r.manager.files, local_env=r.manager.projects.local_env, host=lambda: None)
        env = r.manager.projects.local_env
        member = tmp_path / "member"
        member.mkdir()
        spec = await r.manager.files.add(SPEC.encode(), name=SPEC_NAME, origin="operator", scope=r.project.id, actor="operator")
        [first] = await handoff.deliver([spec], env=env, cwd=str(member), box="t1", actor="orchestrator")
        [again] = await handoff.deliver([spec], env=env, cwd=str(member), box="t1", actor="orchestrator")
        assert first.path == again.path == str(member / ".agents" / "inbox" / "t1" / SPEC_NAME), "the same bytes are not written twice"
        other = await r.manager.files.add(b"another version\n", name=SPEC_NAME, origin="operator", scope=r.project.id, actor="operator")
        [second] = await handoff.deliver([other], env=env, cwd=str(member), box="t1", actor="orchestrator")
        assert second.path.endswith(f"подсказки-free-pro-{other.sha256[:8]}.md") and Path(first.path).read_text(encoding="utf-8") == SPEC
        assert not [p for p in (member / ".agents" / "inbox" / "t1").iterdir() if p.name.endswith(".part")]
        # An inbox that is a link out of the member's folder is not written through.
        linked = tmp_path / "linked"
        (linked / ".agents").mkdir(parents=True)
        (linked / ".agents" / "inbox").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
        (tmp_path / "elsewhere").mkdir()
        with pytest.raises(FileRefused, match="leads outside the member's folder"):
            await handoff.deliver([spec], env=env, cwd=str(linked), box="t1", actor="orchestrator")
        assert not any((tmp_path / "elsewhere").iterdir()), "nothing, not even a directory, was made through the link"
        with pytest.raises(FileRefused, match="nothing here reaches the moon"):
            await handoff.deliver([spec], env="moon", cwd=str(member), box="t1", actor="orchestrator")
    finally:
        await close_team(r.manager)
        await r.manager.close()
