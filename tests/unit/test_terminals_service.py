"""The terminals service against an in-process daemon: the mirror, the owners, reconcile, the audit and the cap."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import TerminalsConfig
from daedalus.harness.env import staff_inherited_env
from daedalus.stores.database import Database
from daedalus.terminals.endpoint import absolute_on
from daedalus.terminals.model import (
    EnvUnavailable,
    InvalidRequest,
    NotFound,
    Origin,
    OverCap,
    Owner,
    TerminalSpec,
    Unsupported,
)
from daedalus.terminals.service import Terminals
from daedalus.terminals.wire import encode_frame
from tests.support.fake_ptyd import FakePtyd


async def wait_until(check: Any, expected: Any, *, timeout: float = 30.0) -> None:
    """Poll an awaitable check until it answers ``expected``; the bound only catches a hang."""
    last = None
    try:
        async with asyncio.timeout(timeout):
            while (last := await check()) != expected:
                await asyncio.sleep(0.01)
    except TimeoutError:
        raise AssertionError(f"waited {timeout:.0f}s for {expected!r}; last saw {last!r}") from None


class FakeOwners:
    """Owners as a test states them: which exist, their names, their projects and default places."""

    def __init__(self) -> None:
        self.existing: dict[Owner, str] = {}
        self.projects: dict[Owner, str] = {}
        self.cwds: dict[tuple[str, Owner], str] = {}

    def add(self, owner: Owner, label: str = "", project: str | None = None, cwd: str | None = None, env: str = "container") -> Owner:
        self.existing[owner] = label or f"{owner.kind} {owner.id}"
        if project:
            self.projects[owner] = project
        if cwd:
            self.cwds[(env, owner)] = cwd
        return owner

    async def exists(self, owner: Owner) -> bool:
        return owner.kind == "free" or owner in self.existing

    async def labels(self, owners: Iterable[Owner]) -> dict[Owner, str]:
        return {o: self.existing[o] for o in owners if o in self.existing}

    async def project_of(self, owner: Owner) -> str | None:
        return self.projects.get(owner)

    async def default_cwd(self, env: str, owner: Owner, project_id: str | None) -> str | None:
        return self.cwds.get((env, owner))

    async def sandbox_writable(self, env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]:
        return [cwd]


class FakeBus:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def publish(self, event_type: str, payload: dict[str, Any], **ids: Any) -> None:
        self.published.append((event_type, dict(payload), ids))


@pytest.fixture
def run_dir() -> Iterable[Path]:
    # A unix socket path has a length limit of about a hundred bytes, which pytest's temporary
    # directories can pass; a short directory of its own keeps the socket inside it.
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))
    yield path / "run"
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(run_dir: Path) -> AsyncIterator[FakePtyd]:
    fake = await FakePtyd(run_dir).start()
    yield fake
    await fake.stop()


@pytest.fixture
def owners() -> FakeOwners:
    return FakeOwners()


@pytest.fixture
def cfg() -> TerminalsConfig:
    return TerminalsConfig(running_cap=20, agent_launch_wait_seconds=5.0)


async def _service(db: Database, run_dir: Path, owners: FakeOwners, cfg: TerminalsConfig, bus: FakeBus | None = None) -> Terminals:
    service = Terminals(db, run_dirs={"container": run_dir, "host": None}, config=lambda: cfg, owners=owners, bus=bus, public_host="192.0.2.1", port_ranges={"container": "8120-8139"})  # type: ignore[arg-type]
    await service.start()
    return service


@pytest.fixture
async def service(db: Database, run_dir: Path, owners: FakeOwners, cfg: TerminalsConfig, daemon: FakePtyd) -> AsyncIterator[Terminals]:
    made = await _service(db, run_dir, owners, cfg, FakeBus())
    assert await made.wait_available("container")
    yield made
    await made.close()


async def _row(db: Database, terminal_id: str) -> dict[str, Any]:
    row = await db.fetchone("SELECT * FROM terminals WHERE id = ?", (terminal_id,))
    assert row is not None
    return dict(row)


async def _audit(db: Database, terminal_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in await db.fetchall("SELECT * FROM terminal_audit WHERE terminal_id = ? ORDER BY seq", (terminal_id,))]


# -- creating, listing, ending ---------------------------------------------------------------


async def test_staff_harness_inheritance_is_fixed_at_the_service_boundary(service: Terminals, daemon: FakePtyd, owners: FakeOwners) -> None:
    staff = owners.add(Owner("staff", "member"), project="project", cwd="/tmp")
    await service.create(TerminalSpec(env="container", owner=staff, profile="harness:claude"))
    launch = next(params for method, params in daemon.calls if method == "terminal.create")
    assert launch["inherited_env"] == staff_inherited_env("claude")
    assert "OPENAI_API_KEY" not in launch["inherited_env"]

    await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    operator_launch = [params for method, params in daemon.calls if method == "terminal.create"][-1]
    assert "inherited_env" not in operator_launch


async def test_staff_harness_refuses_a_daemon_without_inheritance_selection(service: Terminals, daemon: FakePtyd, owners: FakeOwners) -> None:
    daemon.inheritance_selection = False
    staff = owners.add(Owner("staff", "member"), project="project", cwd="/tmp")
    with pytest.raises(InvalidRequest, match="cannot select inherited variables"):
        await service.create(TerminalSpec(env="container", owner=staff, profile="harness:claude"))
    assert not any(method == "terminal.create" for method, _ in daemon.calls)


async def test_a_terminal_is_created_mirrored_listed_and_ended(service: Terminals, daemon: FakePtyd, db: Database, owners: FakeOwners) -> None:
    session = owners.add(Owner("session", "s1"), "Checkout page", project="p1", cwd="/tmp")
    view = await service.create(TerminalSpec(env="container", owner=session))
    assert view["status"] == "running" and view["owner"] == {"kind": "session", "id": "s1", "label": "Checkout page"}
    assert view["project_id"] == "p1" and view["cwd"] == "/tmp" and view["live"]["clients"] == 0
    assert len(view["id"]) == 12 and view["id"] in daemon.terminals
    # The daemon was told whose terminal it is, so a host that dies before its INSERT can adopt it.
    assert daemon.terminals[view["id"]].labels == {"owner_kind": "session", "owner_id": "s1", "project_id": "p1", "profile": "shell"}
    listed = await service.list(owner=session)
    assert [v["id"] for v in listed] == [view["id"]]
    ended = await service.kill(view["id"])
    assert ended["status"] == "exited" and ended["exit_signal"] == "SIGHUP"
    assert [a["action"] for a in await _audit(db, view["id"])] == ["create", "kill"]
    assert service.bus.published[0][0] == "terminal.created"  # type: ignore[union-attr]
    assert service.bus.published[0][2]["session_id"] == "s1" and service.bus.published[0][2]["project_id"] == "p1"  # type: ignore[union-attr]
    assert service.bus.published[-1][:2] == ("terminal.exited", {"exit_code": -1, "signal": "SIGHUP"})  # type: ignore[union-attr]


async def test_an_exit_event_ends_the_row_with_its_code_and_keeps_the_last_screen(service: Terminals, daemon: FakePtyd, db: Database) -> None:
    view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    daemon.terminals[view["id"]].preview = [[{"t": "bye"}]]
    daemon.exit(view["id"], 3)
    await wait_until(lambda: _status(db, view["id"]), "exited")
    row = await _row(db, view["id"])
    assert row["exit_code"] == 3 and json.loads(row["final_preview_json"]) == [[{"t": "bye"}]]
    # After the daemon forgot it, the list still shows its last screen.
    del daemon.terminals[view["id"]]
    [shown] = await service.list(preview_rows=6)
    assert shown["preview"] == [[{"t": "bye"}]]


async def test_a_late_daemon_exit_proves_a_previously_lost_terminal_once(service: Terminals, db: Database) -> None:
    view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    row = await _row(db, view["id"])
    assert await service._end(row, status="lost", exit_code=None, signal=None)
    assert not await db.fetchall("SELECT * FROM terminal_exit_observations")
    assert not await service._end(row, status="exited", exit_code=0, signal=None, observed_instance="another-daemon")
    assert (await _row(db, view["id"]))["status"] == "lost"
    assert await service._end(row, status="exited", exit_code=0, signal=None, observed_instance=row["ptyd_instance"])
    assert (await _row(db, view["id"]))["status"] == "exited"
    assert len(await db.fetchall("SELECT * FROM terminal_exit_observations")) == 1
    published = len(service.bus.published)
    assert not await service._end(row, status="exited", exit_code=0, signal=None, observed_instance=row["ptyd_instance"])
    assert len(service.bus.published) == published


async def test_a_shells_commands_are_mirrored_and_listed(service: Terminals, daemon: FakePtyd, db: Database) -> None:
    shell = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    program = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp", argv=["cat"]))
    # A shell starts with its integration; a program given as argv is started exactly as asked.
    assert shell["shell_integration"] is True and program["shell_integration"] is False
    assert (await _row(db, shell["id"]))["shell_integration"] == 1 and (await _row(db, program["id"]))["shell_integration"] == 0
    with pytest.raises(Unsupported, match="reports no commands"):
        await service.commands(program["id"])
    daemon.terminals[shell["id"]].commands = [
        {"n": 1, "command": "make", "cwd": "/tmp", "exit_code": 2, "finished_at": "x"},
        {"n": 2, "command": "sleep 9", "cwd": "/tmp", "exit_code": None, "finished_at": None},
    ]
    assert [c["n"] for c in await service.commands(shell["id"], last=1)] == [2]
    # A command's end is written down with how long it ran, so the list shows it after the daemon forgot.
    daemon.emit("terminal.command", shell["id"], {"phase": "end", "n": 1, "exit_code": 2, "command": "make", "duration_ms": 1500, "seq": 10})

    async def last_command() -> Any:
        row = await _row(db, shell["id"])
        return json.loads(row["last_command_json"])["duration_ms"] if row["last_command_json"] else None

    await wait_until(last_command, 1500)
    stored = json.loads((await _row(db, shell["id"]))["last_command_json"])
    assert stored["command"] == "make" and stored["exit_code"] == 2


async def _status(db: Database, terminal_id: str) -> str:
    row = await db.fetchone("SELECT status FROM terminals WHERE id = ?", (terminal_id,))
    return str(row["status"]) if row else ""


async def test_what_cannot_be_done_is_refused_with_its_reason(service: Terminals, daemon: FakePtyd, db: Database) -> None:
    with pytest.raises(NotFound):
        await service.create(TerminalSpec(env="container", owner=Owner("session", "nobody")))
    with pytest.raises(EnvUnavailable):
        await service.create(TerminalSpec(env="host", owner=Owner("free")))
    # The sandbox is the daemon's to build, and this daemon cannot: the row it reserved goes again.
    with pytest.raises(Unsupported):
        await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp", sandbox=True))
    assert await service.count_running() == 0
    view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    with pytest.raises(Exception, match="still running"):
        await service.remove(view["id"])
    await service.kill(view["id"])
    await service.remove(view["id"])
    assert await db.fetchone("SELECT 1 FROM terminals WHERE id = ?", (view["id"],)) is None
    # The audit outlives the row.
    assert [a["action"] for a in await _audit(db, view["id"])] == ["create", "kill", "remove"]


async def test_a_sandboxed_terminal_is_given_its_owners_writable_folders_and_keeps_the_choice_on_restart(db: Database, run_dir: Path, owners: FakeOwners, cfg: TerminalsConfig, daemon: FakePtyd, tmp_path: Path) -> None:
    daemon.sandbox = "ok"
    service = await _service(db, run_dir, owners, cfg)
    try:
        assert await service.wait_available("container")
        assert [e.sandbox for e in service.environments()] == ["ok", ""]
        view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd=str(tmp_path), sandbox=True))
        [params] = [p for m, p in daemon.calls if m == "terminal.create"]
        assert params["sandbox"] == {"writable": [str(tmp_path)]}
        assert view["sandbox"] is True and view["sandbox_skipped"] == []
        [create] = [a for a in await _audit(db, view["id"]) if a["action"] == "create"]
        assert json.loads(create["detail_json"])["writable"] == [str(tmp_path)]

        # A restart keeps the sandbox unless it is asked to change it, either way.
        same = await service.restart(view["id"])
        assert same["sandbox"] is True and daemon.terminals[same["id"]].sandbox is not None
        plain = await service.restart(same["id"], sandbox=False)
        assert plain["sandbox"] is False and daemon.terminals[plain["id"]].sandbox is None
        assert "sandbox" not in [p for m, p in daemon.calls if m == "terminal.create"][-1]
        boxed = await service.restart(plain["id"], sandbox=True)
        assert boxed["sandbox"] is True and (await _row(db, boxed["id"]))["sandbox"] == 1

        # A folder the daemon cannot bind is left read-only, and the answer says which and why.
        owners.sandbox_writable = _writable([str(tmp_path), "/no/such/folder"])  # type: ignore[method-assign]
        partial = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd=str(tmp_path), sandbox=True))
        assert partial["sandbox_skipped"] == [{"path": "/no/such/folder", "reason": "missing"}]
    finally:
        await service.close()


def _writable(paths: list[str]) -> Any:
    async def writable(env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]:
        return paths

    return writable


async def test_an_environment_reports_why_it_cannot_sandbox(service: Terminals) -> None:
    [container, host] = service.environments()
    assert container.sandbox == "not available in this build" and host.sandbox == ""
    assert container.view()["sandbox"] == "not available in this build"


async def test_a_missing_directory_falls_back_to_home_and_says_so(service: Terminals, daemon: FakePtyd) -> None:
    view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/no/such/place"))
    assert view["cwd_fallback"] is True and view["cwd"] == daemon.home


async def test_renaming_and_handing_to_free(service: Terminals, owners: FakeOwners, db: Database) -> None:
    session = owners.add(Owner("session", "s1"), project="p1")
    view = await service.create(TerminalSpec(env="container", owner=session, cwd="/tmp"))
    renamed = await service.update(view["id"], title="tests")
    assert renamed["title"] == "tests"
    freed = await service.update(view["id"], owner=Owner("free"))
    assert freed["owner"]["kind"] == "free" and freed["project_id"] == "p1"
    # A free terminal is not the session's any more: ending the session's leaves it alone.
    assert await service.close_owned("session", "s1") == 0
    assert (await _row(db, view["id"]))["status"] == "running"


# -- owners ------------------------------------------------------------------------------------


async def test_ending_an_owner_ends_its_terminals_and_only_its(service: Terminals, owners: FakeOwners, db: Database) -> None:
    one = owners.add(Owner("session", "s1"))
    two = owners.add(Owner("session", "s2"))
    project = owners.add(Owner("project", "p1"))
    a = await service.create(TerminalSpec(env="container", owner=one, cwd="/tmp"))
    b = await service.create(TerminalSpec(env="container", owner=one, cwd="/tmp"))
    c = await service.create(TerminalSpec(env="container", owner=two, cwd="/tmp"))
    d = await service.create(TerminalSpec(env="container", owner=project, cwd="/tmp"))
    assert await service.running_by_session() == {"s1": 2, "s2": 1}
    assert await service.close_owned("session", "s1") == 2
    assert [await _status(db, t["id"]) for t in (a, b, c, d)] == ["exited", "exited", "running", "running"]
    assert await service.close_owned("project", "p1") == 1
    assert await _status(db, d["id"]) == "exited"


# -- reconcile ---------------------------------------------------------------------------------


async def test_a_host_restart_leaves_a_terminal_running_and_a_daemon_restart_loses_it(db: Database, run_dir: Path, owners: FakeOwners, cfg: TerminalsConfig, daemon: FakePtyd) -> None:
    first = await _service(db, run_dir, owners, cfg)
    assert await first.wait_available("container")
    view = await first.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    await first.close()

    second = await _service(db, run_dir, owners, cfg)
    assert await second.wait_available("container")
    assert await _status(db, view["id"]) == "running"
    await daemon.restart()
    await wait_until(lambda: _status(db, view["id"]), "lost", timeout=15)
    row = await _row(db, view["id"])
    assert row["exited_at"] is not None and row["exit_code"] is None
    await second.close()


async def test_a_terminal_that_ended_while_the_host_was_away_is_ended_on_return(db: Database, run_dir: Path, owners: FakeOwners, cfg: TerminalsConfig, daemon: FakePtyd) -> None:
    first = await _service(db, run_dir, owners, cfg)
    assert await first.wait_available("container")
    ended = await first.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    forgotten = await first.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    await first.close()
    daemon.exit(ended["id"], 7)
    del daemon.terminals[forgotten["id"]]  # ended and forgotten by the same daemon
    await db.execute("UPDATE terminals SET created_at = '2026-01-01T00:00:00.000Z' WHERE id = ?", (forgotten["id"],))

    second = await _service(db, run_dir, owners, cfg)
    assert await second.wait_available("container")
    assert (await _row(db, ended["id"]))["exit_code"] == 7
    assert (await _row(db, forgotten["id"]))["status"] == "exited"
    await second.close()


async def test_a_stray_terminal_is_adopted_from_its_labels(service: Terminals, daemon: FakePtyd, owners: FakeOwners, db: Database) -> None:
    owners.add(Owner("staff", "st1"))
    daemon.spawn("stray00000001", labels={"owner_kind": "staff", "owner_id": "st1", "project_id": "p9", "profile": "harness:claude"})
    daemon.spawn("stray00000002", labels={"owner_kind": "martian"})
    await wait_until(lambda: _status(db, "stray00000002"), "running")
    row = await _row(db, "stray00000001")
    assert (row["owner_kind"], row["owner_id"], row["project_id"], row["profile"], row["created_by"]) == ("staff", "st1", "p9", "harness:claude", "system")
    assert (await _row(db, "stray00000002"))["owner_kind"] == "free"


async def test_a_terminal_whose_owner_went_while_its_daemon_was_away_is_ended(db: Database, run_dir: Path, owners: FakeOwners, cfg: TerminalsConfig, daemon: FakePtyd) -> None:
    session = owners.add(Owner("session", "s1"))
    first = await _service(db, run_dir, owners, cfg)
    assert await first.wait_available("container")
    view = await first.create(TerminalSpec(env="container", owner=session, cwd="/tmp"))
    await first.close()
    del owners.existing[session]  # deleted while nothing could reach the daemon
    second = await _service(db, run_dir, owners, cfg)
    assert await second.wait_available("container")
    await wait_until(lambda: _status(db, view["id"]), "exited")
    await second.close()


async def test_a_host_that_fell_behind_the_event_log_reads_the_listing(service: Terminals, daemon: FakePtyd, db: Database) -> None:
    view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    term = daemon.terminals[view["id"]]
    term.status, term.exit_code = "exited", 5  # ended without an event reaching the host
    for writer, queue in daemon._subscribers:
        queue.put_nowait(None)
        payload = json.dumps({"jsonrpc": "2.0", "method": "events.resync", "params": {"from_seq": 1}}).encode()
        writer.write(encode_frame(0, payload))
    await wait_until(lambda: _status(db, view["id"]), "exited")
    assert (await _row(db, view["id"]))["exit_code"] == 5


async def test_an_environment_missing_at_start_is_used_once_it_appears(db: Database, run_dir: Path, owners: FakeOwners, cfg: TerminalsConfig) -> None:
    service = await _service(db, run_dir, owners, cfg)
    assert not await service.wait_available("container")
    [container, host] = service.environments()
    assert (container.available, container.reason) == (False, "not_installed")
    assert (host.available, host.reason) == (False, "not_configured")
    daemon = await FakePtyd(run_dir).start()
    try:
        await wait_until(lambda: _available(service), True, timeout=15)
        [container, _] = service.environments()
        assert container.version == "fake" and container.port_range == "8120-8139" and container.public_host == "192.0.2.1" and container.home == "/root"
        view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
        assert view["status"] == "running"
    finally:
        await service.close()
        await daemon.stop()


async def _available(service: Terminals) -> bool:
    return service.environments()[0].available


async def test_a_daemon_of_another_protocol_is_refused(db: Database, run_dir: Path, owners: FakeOwners, cfg: TerminalsConfig) -> None:
    daemon = await FakePtyd(run_dir, protocol=2).start()
    service = await _service(db, run_dir, owners, cfg)
    try:
        await service.wait_available("container")
        [container, _] = service.environments()
        assert not container.available and container.reason == "protocol_mismatch" and "update Daedalus" in container.detail
    finally:
        await service.close()
        await daemon.stop()


# -- the audit -----------------------------------------------------------------------------------


async def test_an_agent_write_is_audited_truncated_and_whole_hashed(service: Terminals, daemon: FakePtyd, db: Database) -> None:
    view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    text = "é" * 5000
    receipt = await service.write(view["id"], text=text, origin=Origin("orchestrator", launch_id="L1", note="brief"))
    assert receipt.bytes == len(text.encode())
    sent = daemon.terminals[view["id"]].writes[-1]
    assert sent["origin"] == {"kind": "agent", "actor": "orchestrator", "launch_id": "L1", "note": "brief"} and sent["wait"] == "keyboard"
    write = [a for a in await _audit(db, view["id"]) if a["action"] == "write"][-1]
    detail = json.loads(write["detail_json"])
    assert write["actor"] == "orchestrator"
    assert len(detail["text"].encode()) <= 4096 and detail["sha256"] == hashlib.sha256(text.encode()).hexdigest() and detail["length"] == len(text.encode())
    assert detail["launch_id"] == "L1" and detail["bytes"] == len(text.encode())


async def test_old_rows_and_old_audit_are_pruned(service: Terminals, db: Database) -> None:
    old = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    fresh = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    await service.kill(old["id"])
    await service.kill(fresh["id"])
    await db.execute("UPDATE terminals SET exited_at = '2020-01-01T00:00:00.000Z' WHERE id = ?", (old["id"],))
    await db.execute("UPDATE terminal_audit SET at = '2020-01-01T00:00:00.000Z' WHERE terminal_id = ? AND action = 'create'", (old["id"],))
    assert await service.prune() == (1, 1)
    assert await db.fetchone("SELECT 1 FROM terminals WHERE id = ?", (old["id"],)) is None
    assert await db.fetchone("SELECT 1 FROM terminals WHERE id = ?", (fresh["id"],)) is not None
    assert [a["action"] for a in await _audit(db, old["id"])] == ["kill"]


# -- the cap -------------------------------------------------------------------------------------


async def test_the_operator_is_asked_past_the_cap_and_then_let_through(service: Terminals, cfg: TerminalsConfig, db: Database) -> None:
    cfg.running_cap = 2
    for _ in range(2):
        await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    with pytest.raises(OverCap) as refused:
        await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    assert refused.value.details == {"running": 2, "cap": 2}
    view = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"), confirm_over_cap=True)
    assert await service.count_running() == 3
    assert json.loads((await _audit(db, view["id"]))[0]["detail_json"])["over_cap"] is True


async def test_agent_launches_wait_in_line_at_the_cap_and_go_in_order(service: Terminals, cfg: TerminalsConfig) -> None:
    cfg.running_cap = 1
    first = await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    started: list[str] = []

    async def launch(name: str) -> None:
        await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp", created_by=f"agent:{name}", profile="harness:claude"))
        started.append(name)

    a = asyncio.create_task(launch("a"))
    await wait_until(lambda: _queued(service), 1)
    b = asyncio.create_task(launch("b"))
    await wait_until(lambda: _queued(service), 2)
    assert [w["actor"] for w in service.queue()] == ["agent:a", "agent:b"] and service.queue()[0]["profile"] == "harness:claude"
    assert started == []
    await service.kill(first["id"])
    await asyncio.wait_for(a, 5)
    assert started == ["a"] and not b.done()
    # Raising the cap is noticed without anything ending.
    cfg.running_cap = 2
    await asyncio.wait_for(b, 5)
    assert started == ["a", "b"] and service.queue() == []


async def _queued(service: Terminals) -> int:
    return len(service.queue())


async def test_an_agent_launch_gives_up_after_its_wait(service: Terminals, cfg: TerminalsConfig) -> None:
    cfg.running_cap = 1
    await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    with pytest.raises(OverCap) as refused:
        await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp", created_by="agent:x"), wait=0.2)
    assert refused.value.details["waited"] is True and service.queue() == []


async def test_a_cancelled_launch_leaves_the_line(service: Terminals, cfg: TerminalsConfig) -> None:
    cfg.running_cap = 1
    await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    task = asyncio.create_task(service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp", created_by="agent:x")))
    await wait_until(lambda: _queued(service), 1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert service.queue() == []


# -- load -------------------------------------------------------------------------------------------


async def test_the_load_reports_what_runs_and_learns_a_cost_per_profile(service: Terminals, daemon: FakePtyd) -> None:
    views = [await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp", profile=p)) for p in ("shell", "harness:claude")]
    daemon.rss = {views[0]["id"]: 10 << 20, views[1]["id"]: 500 << 20}
    stats = daemon._handle("terminal.stats", {})
    daemon.emit("terminal.stats", None, stats)
    await wait_until(lambda: _profiles(service), 2)
    load = await service.load(cap=4)
    assert load["running"] == 2 and load["cap"] == 20
    # ptyd's own memory is counted with the terminals: it holds their emulators and output rings.
    assert load["used"]["rss_bytes"] == (510 << 20) + (30 << 20) and load["used"]["daemon_rss_bytes"] == 30 << 20
    assert load["envs"][0]["rss_bytes"] == 510 << 20 and load["envs"][0]["daemon_rss_bytes"] == 30 << 20
    # Only the terminals' own samples teach the per-profile cost; the daemon's share is not in them.
    assert load["profiles"]["harness:claude"]["rss_bytes"] == 500 << 20
    assert load["likely"]["basis"] == "running" and load["likely"]["rss_bytes"] == 255 << 20
    projection = load["projection"]
    assert projection["cap"] == 4 and projection["terminals_rss_bytes"] == (540 << 20) + 2 * (255 << 20)


async def test_a_daemon_that_reports_no_process_of_its_own_adds_nothing(service: Terminals, daemon: FakePtyd) -> None:
    # An older daemon sends no "daemon" block; the sum is then the terminals' alone.
    await service.create(TerminalSpec(env="container", owner=Owner("free"), cwd="/tmp"))
    daemon.daemon = {}
    load = await service.load()
    assert load["used"]["rss_bytes"] == 50 << 20 and load["used"]["daemon_rss_bytes"] == 0


async def _profiles(service: Terminals) -> int:
    return len(service.costs.profiles())


async def test_a_host_terminal_takes_a_windows_folder_and_a_container_does_not(db: Database, run_dir: Path, owners: FakeOwners, cfg: TerminalsConfig, daemon: FakePtyd) -> None:
    # The host of a native Windows installation names its folders C:\...; a leading slash was the
    # only absolute path this took, so the app's "On the host" opened nothing there.
    service = Terminals(db, run_dirs={"container": None, "host": run_dir}, config=lambda: cfg, owners=owners, bus=FakeBus(), public_host="192.0.2.1", port_ranges={"host": "8120-8139"})  # type: ignore[arg-type]
    await service.start()
    try:
        assert await service.wait_available("host")
        view = await service.create(TerminalSpec(env="host", owner=Owner("free"), cwd="C:\\Users\\operator"))
        assert view["env"] == "host"
        with pytest.raises(InvalidRequest, match="absolute path"):
            await service.create(TerminalSpec(env="host", owner=Owner("free"), cwd="Users\\operator"))
    finally:
        await service.close()
    assert absolute_on("container", "/tmp") and not absolute_on("container", "C:\\Users\\operator")
    assert absolute_on("host", "/home/operator") and absolute_on("host", "D:\\work") and not absolute_on("host", "work")
