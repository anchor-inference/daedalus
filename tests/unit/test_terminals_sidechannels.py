"""The terminals service's side channels against an in-process daemon: programs, files and their
roots, launches with their hook events and replies, and byte streams — and the audit of each."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

from daedalus.config import TerminalsConfig
from daedalus.stores.database import Database
from daedalus.terminals import endpoint
from daedalus.terminals.model import Forbidden, HookEvent, LaunchSpec, NotFound, Owner, StaleLaunch, TerminalSpec
from daedalus.terminals.service import Terminals
from tests.support.fake_ptyd import FakePtyd


class FreeOwners:
    async def exists(self, owner: Owner) -> bool:
        return True

    async def labels(self, owners: Iterable[Owner]) -> dict[Owner, str]:
        return {}

    async def project_of(self, owner: Owner) -> str | None:
        return None

    async def default_cwd(self, env: str, owner: Owner, project_id: str | None) -> str | None:
        return None

    async def sandbox_writable(self, env: str, owner: Owner, project_id: str | None, cwd: str) -> list[str]:
        return [cwd]


async def eventually(check: Any, *, timeout: float = 30.0) -> None:
    """Poll a condition until it holds; the bound only catches a hang."""
    async with asyncio.timeout(timeout):
        while not await check():
            await asyncio.sleep(0.02)


@pytest.fixture
def run_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))
    yield path / "run"
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def daemon(run_dir: Path) -> AsyncIterator[FakePtyd]:
    fake = await FakePtyd(run_dir).start()
    yield fake
    await fake.stop()


@pytest.fixture
def frequent_housekeeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Roots reach the daemon on the service's housekeeping tick, every two seconds in production.

    A test that waits for a root to arrive and then to leave sat out one whole tick per step, six
    seconds for nothing. The tick is shortened before the service starts, because the loop has
    already begun its first two-second sleep by the time the test body runs; the roots are still
    delivered by the same tick, so what the tests claim about it is unchanged.
    """
    from daedalus.terminals import service as service_module

    monkeypatch.setattr(service_module, "FLUSH_SECONDS", 0.05)


@pytest.fixture
async def service(db: Database, run_dir: Path, daemon: FakePtyd) -> AsyncIterator[Terminals]:
    made = Terminals(db, run_dirs={"container": run_dir, "host": None}, config=lambda: TerminalsConfig(), owners=FreeOwners())  # type: ignore[arg-type]
    await made.start()
    assert await made.wait_available("container")
    yield made
    await made.close()


async def _audit(db: Database, action: str) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT * FROM terminal_audit WHERE action = ? ORDER BY seq", (action,))
    return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]


async def test_programs_run_and_are_audited(service: Terminals, daemon: FakePtyd, db: Database) -> None:
    daemon.exec_results["claude"] = {"stdout": "2.1.0 (Claude Code)\n"}
    result = await service.exec_run("container", ["claude", "--version"], cwd="/tmp", actor="agent:harness")
    assert result.exit_code == 0 and result.stdout == "2.1.0 (Claude Code)\n" and not result.timed_out
    method, params = daemon.calls[-1]
    assert method == "exec.run" and params["argv"] == ["claude", "--version"] and params["cwd"] == "/tmp" and params["timeout_ms"] == 60000
    with pytest.raises(Forbidden):
        await service.exec_run("container", ["sh", "-c", "id"])
    rows = await _audit(db, "exec")
    assert [r["actor"] for r in rows] == ["agent:harness", "system"]
    assert rows[0]["detail"]["exit_code"] == 0 and rows[0]["detail"]["argv"] == ["claude", "--version"]
    assert "error" in rows[1]["detail"]


@pytest.mark.usefixtures("frequent_housekeeping")
async def test_roots_follow_the_project_folders_and_the_adapters(service: Terminals, daemon: FakePtyd, db: Database, tmp_path: Path) -> None:
    folder = tmp_path / "site"
    folder.mkdir()
    (folder / "notes.md").write_text("hello")
    await db.execute("INSERT INTO projects(id, name, created_at) VALUES ('p1', 'Site', '2026-09-24T00:00:00Z')")
    await db.execute("INSERT INTO project_folders(id, project_id, path, env, created_at) VALUES ('f1', 'p1', ?, 'container', '2026-09-24T00:00:00Z')", (str(folder),))
    await db.execute("INSERT INTO project_folders(id, project_id, path, env, created_at) VALUES ('f2', 'p1', '/home/someone/elsewhere', 'host', '2026-09-24T00:00:00Z')")

    async def has(root: str) -> bool:
        return root in daemon.roots

    # Within a housekeeping tick of the folder appearing; only the container's own folders.
    await eventually(lambda: has(str(folder)))
    assert "/home/someone/elsewhere" not in daemon.roots
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    service.set_extra_roots("container", "claude", [str(transcripts)])
    await eventually(lambda: has(str(transcripts)))
    chunk = await service.fs_read("container", str(folder / "notes.md"))
    assert chunk.data == b"hello" and chunk.eof and chunk.next_offset == 5
    listing = await service.fs_list("container", str(folder))
    assert [e["name"] for e in listing["entries"]] == ["notes.md"]
    with pytest.raises(Forbidden):
        await service.fs_read("container", "/etc/hostname")
    service.set_extra_roots("container", "claude", [])

    async def gone() -> bool:
        return str(transcripts) not in daemon.roots

    await eventually(gone)


@pytest.mark.usefixtures("frequent_housekeeping")
async def test_a_restarted_daemon_is_given_its_roots_again(service: Terminals, daemon: FakePtyd, db: Database) -> None:
    service.set_extra_roots("container", "codex", ["/root/.codex/sessions"])

    async def has() -> bool:
        return "/root/.codex/sessions" in daemon.roots

    await eventually(has)
    await daemon.restart()
    daemon.roots = []
    await eventually(has)


async def test_tail_passes_the_file_id_back(service: Terminals, daemon: FakePtyd, tmp_path: Path) -> None:
    log = tmp_path / "session.jsonl"
    log.write_text("one\n")
    daemon.roots = [str(tmp_path)]
    first = await service.fs_tail("container", str(log), from_offset=0)
    assert first.data == b"one\n" and first.next_offset == 4 and first.file_id
    replacement = tmp_path / "next.jsonl"
    replacement.write_text("brand new\n")
    replacement.replace(log)  # a new file in the old one's place, as a log rotation leaves it
    second = await service.fs_tail("container", str(log), from_offset=first.next_offset, file_id=first.file_id)
    assert second.rotated and second.data == b"brand new\n"


async def test_a_launch_delivers_its_hooks_in_order_and_ends(service: Terminals, daemon: FakePtyd, db: Database) -> None:
    launch = await service.register_launch("container", LaunchSpec(files={"settings.json": b'{"hooks":{}}'}, ports=[18300], hold_max_ms=60000), actor="agent:harness")
    assert launch.launch_id in daemon.launches and daemon.launches[launch.launch_id]["files"] == {"settings.json": b'{"hooks":{}}'}
    assert launch.env_vars["DAEDALUS_HOOK_CMD"] and launch.dir.endswith(launch.launch_id)
    view = await service.create(TerminalSpec(env="container", owner=Owner("free"), argv=["claude"], launch_id=launch.launch_id, profile="harness:claude", created_by="agent:harness"))
    assert [p["launch_id"] for m, p in daemon.calls if m == "terminal.create"] == [launch.launch_id]
    daemon.launches[launch.launch_id]["terminal_id"] = view["id"]
    # Posted before anyone reads: kept for the reader.
    daemon.post_hook(launch.launch_id, "SessionStart", {"session_id": "abc"})
    daemon.post_hook(launch.launch_id, "PermissionRequest", {"tool_name": "Bash"}, hold_ms=60000)
    daemon.post_hook(launch.launch_id, "Stop", {})
    got: list[HookEvent] = []

    async def read() -> None:
        async for event in service.hook_events(launch.launch_id):
            got.append(event)

    reader = asyncio.create_task(read())

    async def three() -> bool:
        return len(got) == 3

    await eventually(three)
    assert [e.name for e in got] == ["SessionStart", "PermissionRequest", "Stop"]
    assert got[0].seq < got[1].seq < got[2].seq and got[0].terminal_id == view["id"] and got[0].body == {"session_id": "abc"}
    held = got[1]
    assert held.reply_id and held.hold_ms == 60000
    await service.reply_hook("container", held.reply_id, 200, {"decision": "allow"}, launch_id=launch.launch_id, actor="orchestrator")
    assert daemon.replies[-1]["body"] == {"decision": "allow"}
    with pytest.raises(NotFound):
        await service.reply_hook("container", held.reply_id, 200, {}, launch_id=launch.launch_id)
    daemon.end_launch(launch.launch_id)
    await asyncio.wait_for(reader, 10)  # the end of the launch ends the stream of its hooks
    replies = await _audit(db, "hook_reply")
    assert [r["actor"] for r in replies] == ["orchestrator", "system"] and replies[0]["terminal_id"] == view["id"]
    assert replies[0]["detail"]["reply_id"] == held.reply_id and "error" in replies[1]["detail"]
    [registered] = await _audit(db, "launch")
    assert registered["detail"]["launch_id"] == launch.launch_id and set(registered["detail"]["files"]) == {"settings.json"}
    # An ended launch's hooks end at once for a late reader.
    late = [e async for e in service.hook_events(launch.launch_id)]
    assert late == []


async def test_unregister_ends_the_hook_stream(service: Terminals, daemon: FakePtyd) -> None:
    launch = await service.register_launch("container", LaunchSpec(launch_id="L7", terminal_id="t7"))
    stream = service.hook_events("L7")
    reader = asyncio.create_task(anext(stream, None))
    await asyncio.sleep(0.05)
    assert await service.unregister_launch("container", launch.launch_id) is True
    assert await asyncio.wait_for(reader, 10) is None
    with pytest.raises(StaleLaunch):
        await service.net_dial("container", "unix:bridge.sock", "L7")


async def test_a_dialled_stream_carries_bytes_and_closes(service: Terminals, daemon: FakePtyd, db: Database) -> None:
    await service.register_launch("container", LaunchSpec(launch_id="L1"))
    with pytest.raises(Forbidden):
        await service.net_dial("container", "tcp:127.0.0.1:18301", "L1")
    await service.net_allow("container", "L1", 18301)
    stream = await service.net_dial("container", "tcp:127.0.0.1:18301", "L1", actor="agent:harness")
    await stream.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    assert await stream.read() == b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    await stream.close()
    assert stream.closed and await stream.read() == b""
    assert [r["action"] for r in await db.fetchall("SELECT action FROM terminal_audit ORDER BY seq")] == ["launch", "net_allow", "dial"]


async def test_a_stream_nobody_reads_is_closed_as_too_slow(service: Terminals, daemon: FakePtyd) -> None:
    await service.register_launch("container", LaunchSpec(launch_id="L2"))
    stream = await service.net_dial("container", "unix:bridge.sock", "L2")
    chunk = b"x" * (256 << 10)
    for _ in range(6):  # 1.5 MiB echoed back, never read
        await stream.write(chunk)

    async def closed() -> bool:
        return stream.closed

    await eventually(closed)
    assert stream.reason == "consumer too slow"


async def test_the_state_directory_is_remembered_for_sealing(service: Terminals, run_dir: Path) -> None:
    class Settings:
        sealed_everywhere = (run_dir,)

    assert endpoint.sealed_state_dirs(Settings()) == (Path(f"{run_dir}-state"),)  # type: ignore[arg-type]


async def test_a_terminal_for_an_unknown_launch_is_refused_and_leaves_no_row(service: Terminals, db: Database) -> None:
    with pytest.raises(StaleLaunch):
        await service.create(TerminalSpec(env="container", owner=Owner("free"), argv=["claude"], launch_id="never"))
    assert await db.fetchall("SELECT id FROM terminals") == []
