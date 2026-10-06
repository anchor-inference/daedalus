"""A project whose folders are all on the host, seen from the agent's container: where its
orchestrator runs, how it reads those folders, what happens when it cannot be woken, and who may
work there."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.types import MessageRole, TextBlock

from daedalus.config import Settings
from daedalus.extensions.dispatches import Dispatches
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.host.host_exec import HostExecBackend
from daedalus.host.session_runner import host_home_name
from daedalus.stores.database import Database
from daedalus.stores.projects import FolderSpec, Project
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import Rig, _idle, events, events_messages, git, rig

HOST_ROOT = "/home/someone/labs"
HOME_NAME = "project-{project_id}"
STUCK_KEY = "orchestrator_stuck:{project_id}"


def install_dispatches(r: Rig) -> Dispatches:
    dispatches = Dispatches(r.team.app)
    r.team.app.extensions["dispatches"] = dispatches
    r.manager.asks.default_dispatch = dispatches.default_dispatch
    r.orch.state_sections.append(dispatches.state_section)
    return dispatches


async def host_project(r: Rig, name: str = "Labs") -> Project:
    project = await r.manager.projects.create(name, [FolderSpec(HOST_ROOT, env="host")])
    await r.manager.projects.ensure_roots()
    found = await r.manager.projects.get(project.id)
    assert found is not None and found.primary.env == "host"
    return found


@dataclass
class FakeHost:
    """The host terminal daemon's side channels over a directory of this machine standing in for
    the operator's: host paths under ``HOST_ROOT`` are read from ``disk``, and every call is kept."""

    disk: Path
    up: bool = True
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def available(self) -> bool:
        return self.up

    def local(self, path: str) -> Path:
        if path != HOST_ROOT and not path.startswith(HOST_ROOT + "/"):
            raise PermissionError(f"looking at {path}: outside the roots")
        return self.disk / path[len(HOST_ROOT) :].lstrip("/")

    def _check(self) -> None:
        if not self.up:
            raise ConnectionError("the host terminal bridge is not available: the host terminal service is not running")

    async def stat(self, path: str) -> dict[str, Any]:
        self._check()
        self.calls.append(("stat", path))
        target = self.local(path)
        if not target.exists():
            return {"exists": False}
        return {"exists": True, "type": "dir" if target.is_dir() else "file", "size": target.stat().st_size}

    async def list_dir(self, path: str, *, limit: int) -> dict[str, Any]:
        self._check()
        self.calls.append(("list", path))
        target = self.local(path)
        if not target.exists():
            raise FileNotFoundError(f"listing {path}: not found")
        entries = [{"name": e.name, "type": "dir" if e.is_dir() else "file", "size": 0} for e in sorted(target.iterdir())]
        return {"entries": entries[:limit], "truncated": len(entries) > limit}

    async def read(self, path: str, *, offset: int, max_bytes: int) -> Any:
        self._check()
        self.calls.append(("read", path))
        data = self.local(path).read_bytes()
        chunk = data[offset : offset + max_bytes]
        return SimpleNamespace(data=chunk, eof=offset + len(chunk) >= len(data))

    async def exec_run(self, env: str, argv: list[str], *, cwd: str, env_vars: dict[str, str] | None = None, timeout: float) -> Any:
        self._check()
        self.calls.append(("exec", argv))
        assert env == "host" and argv[0] == "git", "only git is on the daemon's program list for Peek"
        mapped = [str(self.local(a)) if a.startswith(HOST_ROOT) else a for a in argv]
        done = subprocess.run(mapped, cwd=self.local(cwd), env={"PATH": "/usr/bin:/bin", **(env_vars or {})}, capture_output=True, text=True, timeout=timeout, check=False)
        return SimpleNamespace(exit_code=done.returncode, stdout=done.stdout.replace(str(self.disk), HOST_ROOT), stderr=done.stderr, timed_out=False)


def host_disk(tmp_path: Path) -> Path:
    disk = tmp_path / "operator-machine"
    (disk / "src").mkdir(parents=True)
    git(disk, "init", "-q", "-b", "main")
    git(disk, "config", "user.name", "someone")
    git(disk, "config", "user.email", "someone@example.invalid")
    (disk / "README.md").write_text("Labs: experiments\nrun with make\n")
    (disk / "src" / "main.py").write_text("print('hello from the host')\n")
    git(disk, "add", "-A")
    git(disk, "commit", "-qm", "first experiment")
    (disk / "notes.txt").write_text("not committed yet\n")
    return disk


async def chat_notices(r: Rig, session_id: str) -> list[str]:
    out = []
    for message in await r.manager.sessions.list_transcript(session_id):
        if message.role is MessageRole.user and message.metadata.get("daedalus.notice"):
            out.append("".join(b.text for b in message.content_blocks if isinstance(b, TextBlock)))
    return out


# -- where the orchestrator runs ---------------------------------------------------------------------


async def test_an_orchestrator_of_a_project_whose_only_folder_is_on_the_host_runs(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "Surveying the folders."}])
    try:
        if r.manager.projects.local_env != "container":
            pytest.skip("a host folder is out of reach only in a container installation")
        dispatches = install_dispatches(r)
        project = await host_project(r)
        sid = (await r.orch.enable(project.id)).settings.orchestrator.session_id
        state = await r.manager.get_state(sid)
        assert state is not None
        home = settings.workspaces_dir / HOME_NAME.format(project_id=project.id)
        assert state.workspace == home and home.is_dir(), "it runs in a directory of its own, not in the host path"
        assert state.project is not None and [str(f.path) for f in state.project.folders] == [HOST_ROOT], "the folder stays the project's"
        assert state.services is not None and isinstance(state.services.exec_backend, HostExecBackend)
        assert state.services.workspace_dir == Path(HOST_ROOT), "its tools work in the host folder, through the host terminal daemon"

        dispatch = await dispatches.create(await r.manager.projects.get(project.id), text="Survey the folders and write the brief", title="Setup", kind="setup")  # type: ignore[arg-type]

        async def woken() -> bool:
            return bool(await events_messages(r.manager, sid)) and await _idle(r.manager, sid)

        await until_await(woken, "the host project's orchestrator ran")
        [batch] = await events_messages(r.manager, sid)
        assert f"dispatch {dispatch.id}" in batch
        assert r.provider.requests, "the model was asked"
        assert (await r.manager.dispatches.get(dispatch.id)).status == "open"  # type: ignore[union-attr]
    finally:
        await r.manager.close()


async def test_an_orchestrator_stored_in_a_host_folder_is_moved_at_start_and_gets_its_waiting_events(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "Surveying the folders."}])
    try:
        if r.manager.projects.local_env != "container":
            pytest.skip("a host folder is out of reach only in a container installation")
        dispatches = install_dispatches(r)
        project = await host_project(r)
        # As the stuck project was stored: an orchestrator session without a home of its own. It is
        # made while the host answers, as an ordinary session in a host folder is; it then runs from
        # the directory every such session runs from until the start gives it the office's own.
        r.manager.host_bridge = FakeHost(host_disk(tmp_path))  # type: ignore[assignment]
        state = await r.manager.create_session(f"Orchestrator · {project.name}", metadata={"orchestrator_of": project.id, "telegram_detached": True}, project_id=project.id)
        sid = state.session.id
        assert state.workspace == settings.workspaces_dir / host_home_name(sid)
        await r.manager.projects.update_orchestrator(project.id, enabled=True)
        assert await r.manager.projects.set_orchestrator(project.id, expect="", value=sid)
        await r.manager.db.kv_set(f"orchestrator_cursor:{project.id}", r.manager.bus.head)
        dispatch = await dispatches.create(await r.manager.projects.get(project.id), text="Survey the folders", kind="setup")  # type: ignore[arg-type]

        await r.orch.resume()  # the start-up of the fixed version

        stored = await r.manager.db.fetchone("SELECT metadata FROM sessions WHERE id = ?", (sid,))
        assert json.loads(stored["metadata"])["home"] == HOME_NAME.format(project_id=project.id)
        assert (await r.manager.get_state(sid)).workspace == settings.workspaces_dir / HOME_NAME.format(project_id=project.id)  # type: ignore[union-attr]

        async def woken() -> bool:
            return bool(await events_messages(r.manager, sid)) and await _idle(r.manager, sid)

        await until_await(woken, "the waiting dispatch reached the moved orchestrator")
        assert f"dispatch {dispatch.id}" in (await events_messages(r.manager, sid))[0]
        assert not await r.orch.give_home(await r.manager.projects.get(project.id)), "a second start changes nothing"  # type: ignore[arg-type]
        assert HOME_NAME.format(project_id=project.id) not in [p.name for p in await r.manager.orphan_workspaces()], "its home is not swept as an orphan"
    finally:
        await r.manager.close()


# -- reading host folders --------------------------------------------------------------------------------


async def test_peek_reads_a_host_folder_through_the_bridge_with_the_same_bounds(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        if r.manager.projects.local_env != "container":
            pytest.skip("a host folder is read through the bridge only from a container installation")
        project = await host_project(r)
        sid = (await r.orch.enable(project.id)).settings.orchestrator.session_id
        with pytest.raises(Refused, match="no host terminal bridge"):
            await r.call(sid, "peek", op="read", path="README.md")

        disk = host_disk(tmp_path)
        (disk / "big.txt").write_text("".join(f"line {n}\n" for n in range(5000)))
        host = FakeHost(disk)
        r.team.app.extensions["host_bridge"] = host

        page = await r.call(sid, "peek", op="read", path="README.md")
        assert "     1\tLabs: experiments" in page and "     2\trun with make" in page
        assert ("read", f"{HOST_ROOT}/README.md") in host.calls
        big = await r.call(sid, "peek", op="read", path="big.txt", offset=4990, limit=1000)
        assert "  4990\tline 4989" in big and "  5000\tline 4999" in big and "continue" not in big
        bounded = await r.call(sid, "peek", op="read", path="big.txt", limit=1000)
        assert bounded.count("\n") <= 401 and "continue with offset=401" in bounded
        listing = await r.call(sid, "peek", op="ls")
        assert "README.md" in listing and "src/" in listing
        assert "src/main.py" in await r.call(sid, "peek", op="find", pattern="*.py")
        assert "src/main.py:1:print('hello from the host')" in await r.call(sid, "peek", op="search", pattern="hello")
        assert await r.call(sid, "peek", op="search", pattern="nowhere-to-be-found") == "(no matches)"
        assert "first experiment" in await r.call(sid, "peek", op="git_log")
        assert "notes.txt" in await r.call(sid, "peek", op="git_status")
        assert not (disk / ".git" / "index.lock").exists()
        with pytest.raises(Refused, match="outside the project's folders"):
            await r.call(sid, "peek", op="read", path="../../etc/passwd")
        with pytest.raises(Refused, match="outside the project's folders"):
            await r.call(sid, "peek", op="read", path="/etc/passwd")
        with pytest.raises(Refused, match="not a revision"):
            await r.call(sid, "peek", op="git_diff", ref="--output=/tmp/x")
        with pytest.raises(Refused, match="no such file"):
            await r.call(sid, "peek", op="read", path="missing.md")
        assert all(c[1][0] == "git" for c in host.calls if c[0] == "exec"), "search and history go through git alone"

        host.up = False
        with pytest.raises(Refused, match="host terminal bridge is not answering"):
            await r.call(sid, "peek", op="read", path="README.md")
    finally:
        await r.manager.close()


# -- a wake-up that cannot be delivered --------------------------------------------------------------------


async def test_an_unreachable_folder_is_told_once_blocks_the_dispatch_and_recovers(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "Back at work."}])
    try:
        dispatches = install_dispatches(r)
        now = [1000.0]
        r.orch.clock = lambda: now[0]
        mount = tmp_path / "unmounted"
        project = await r.manager.projects.create("Mounted", [FolderSpec(str(mount))])
        sid = (await r.orch.enable(project.id)).settings.orchestrator.session_id
        queue = r.orch.queues[project.id]
        dispatch = await dispatches.create(await r.manager.projects.get(project.id), text="Survey the folders", kind="setup")  # type: ignore[arg-type]
        await until_await(lambda: _has(queue), "the dispatch reached the queue")

        now[0] += 2
        assert not await queue.pump()
        blocked = await r.manager.dispatches.get(dispatch.id)
        assert blocked is not None and blocked.status == "blocked" and "not reachable" in blocked.result
        [closed] = await events(r.manager, "dispatch.closed")
        assert closed.payload["status"] == "blocked" and closed.payload["by"] == "system", "the main orchestrator is woken by it"
        assert [e.payload["change"] for e in await events(r.manager, "dispatch.updated")] == ["closed"]
        posted = [d for d in r.team.app.notifications.posted if d.kind == "orchestrator_stuck"]  # type: ignore[attr-defined]
        assert len(posted) == 1 and "cannot run" in posted[0].title and "not reachable" in posted[0].body
        notices = await chat_notices(r, sid)
        assert len(notices) == 1 and "could not be woken" in notices[0] and f"dispatch {dispatch.id} (#1) is marked as blocked" in notices[0]
        assert notices[0].startswith("[events · Mounted · 2 since "), "the app draws it as an events card"
        assert await r.manager.db.kv_get(STUCK_KEY.format(project_id=project.id))
        assert queue.due_at() is not None and queue.due_at() - now[0] >= 60, "it backs off instead of retrying in seconds"

        # Tried again later: still said only once, and the wait grows. Work handed over meanwhile is
        # blocked too, with a line of its own and no second notification.
        second = await dispatches.create(await r.manager.projects.get(project.id), text="And the docs")  # type: ignore[arg-type]
        await until_await(lambda: _count(queue, 2), "the second dispatch reached the queue")
        now[0] += 61
        assert not await queue.pump()
        assert queue.due_at() - now[0] >= 120  # type: ignore[operator]
        assert (await r.manager.dispatches.get(second.id)).status == "blocked"  # type: ignore[union-attr]
        notices = await chat_notices(r, sid)
        assert len(notices) == 2 and f"dispatch {second.id} (#2) is marked as blocked" in notices[1] and "could not be woken" not in notices[1]
        assert len([d for d in r.team.app.notifications.posted if d.kind == "orchestrator_stuck"]) == 1  # type: ignore[attr-defined]
        now[0] += 200
        assert not await queue.pump()
        assert len(await chat_notices(r, sid)) == 2, "nothing new, nothing said"

        # A restart meets the same refusal: not said again.
        await r.orch.stop_queue(project.id)
        queue = await r.orch.start_queue(project.id)
        await until_await(lambda: _has(queue), "the waiting dispatch was read again after the restart")
        now[0] += 2
        assert not await queue.pump()
        assert len([d for d in r.team.app.notifications.posted if d.kind == "orchestrator_stuck"]) == 1  # type: ignore[attr-defined]
        assert len(await chat_notices(r, sid)) == 2

        # The folder comes back while the process is down: the first delivery after the next start
        # still opens the dispatch again.
        await r.orch.stop_queue(project.id)
        mount.mkdir()
        queue = await r.orch.start_queue(project.id)
        await until_await(lambda: _has(queue), "the waiting dispatch was read again after the restart")
        now[0] += 2
        assert await queue.pump()
        assert (await r.manager.dispatches.get(dispatch.id)).status == "open"  # type: ignore[union-attr]
        assert (await r.manager.dispatches.get(second.id)).status == "open"  # type: ignore[union-attr]
        assert await r.manager.db.kv_get(STUCK_KEY.format(project_id=project.id)) is None
        notices = await chat_notices(r, sid)
        assert len(notices) == 3 and "runs again" in notices[2] and f"dispatch {dispatch.id} (#1) is open again" in notices[2]
        await until_await(lambda: _idle(r.manager, sid), "the orchestrator ran its turn")
    finally:
        await r.manager.close()


async def _has(queue: Any) -> bool:
    return bool(queue.items)


async def _count(queue: Any, n: int) -> bool:
    return len(queue.items) >= n


async def test_a_refusal_that_passes_by_itself_is_retried_quietly(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        dispatches = install_dispatches(r)
        now = [1000.0]
        r.orch.clock = lambda: now[0]
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        queue = r.orch.queues[r.project.id]
        r.manager.recovering = True  # the host is still continuing its own runs after a start
        dispatch = await dispatches.create(await r.refreshed(), text="Add a menu")
        await until_await(lambda: _has(queue), "the dispatch reached the queue")
        now[0] += 2
        assert not await queue.pump()
        assert not queue.stuck and queue.due_at() - now[0] <= 5  # type: ignore[operator]
        assert (await r.manager.dispatches.get(dispatch.id)).status == "open"  # type: ignore[union-attr]
        assert not [d for d in r.team.app.notifications.posted if d.kind == "orchestrator_stuck"]  # type: ignore[attr-defined]
        assert await chat_notices(r, sid) == []
    finally:
        r.manager.recovering = False
        await r.manager.close()


# -- who may work in a host folder -------------------------------------------------------------------------


async def test_a_daedalus_member_is_hired_into_a_host_folder_only_while_the_host_answers(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        if r.manager.projects.local_env != "container":
            pytest.skip("a host folder is driven through the daemon only from a container installation")
        project = await host_project(r)
        sid = (await r.orch.enable(project.id)).settings.orchestrator.session_id
        with pytest.raises(Refused, match="Daedalus staff cannot run in the host now: the host terminal daemon is not answering"):
            await r.call(sid, "hire", name="Ada", role="Menu")
        assert await r.manager.staff.list(project.id) == []

        r.manager.host_bridge = FakeHost(host_disk(tmp_path))  # type: ignore[assignment]
        await r.call(sid, "hire", name="Ada", role="Menu")
        [member] = await r.manager.staff.list(project.id)
        assert member.harness == "daedalus" and member.isolation == "shared", "a host folder's git is not known here, so no worktree is assumed"
    finally:
        await r.manager.close()
