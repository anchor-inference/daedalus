"""daedalus-cli's real device client against this host's real relay, conversations and journal: the
relay's own scenarios (``tests/unit/test_remote_relay.py``) with the fake terminal swapped for the Go
client, so a disagreement between the two sides of the contract fails here rather than on a laptop.

The client is the Go test binary of ``cli/internal/device``, whose ``TestServeAnExternalRelay`` connects
to the relay these tests serve and runs until told to stop. Build it, then run:

    (cd cli && CGO_ENABLED=0 go test -c -o /tmp/device.test ./internal/device)
    DAEDALUS_INTEGRATION=1 DAEDALUS_CLI_DEVICE_TEST_BIN=/tmp/device.test pytest tests/integration/test_cli_device_real.py

A server policy refuses a long foreground ``sleep``, so the commands that must still be running at a
reconnect sleep three seconds, not thirty.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from daedalus.config import Settings
from daedalus.host import call_scope
from daedalus.remote.hands import DeviceExecBackend
from daedalus.stores.database import Database
from tests.support.waiting import until
from tests.unit.test_remote_relay import DEVICE, TOKEN, Rig, _tool_results, rig
from tests.unit.test_session_runner import ScriptedProvider

BINARY = os.environ.get("DAEDALUS_CLI_DEVICE_TEST_BIN", "")

pytestmark = [
    pytest.mark.skipif(os.environ.get("DAEDALUS_INTEGRATION") != "1", reason="set DAEDALUS_INTEGRATION=1 to run"),
    pytest.mark.skipif(not BINARY or not Path(BINARY).is_file(), reason="set DAEDALUS_CLI_DEVICE_TEST_BIN to the built cli/internal/device test binary"),
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder.resolve()


class GoClient:
    """One process of the Go client: started, told to stop by a file, and asked how it ended."""

    def __init__(self, url: str, tmp: Path, *, guard: str = "auto", token: str = TOKEN, client: str = "") -> None:
        self.base = url.removesuffix("/api/cli/relay").replace("ws://", "http://")
        self.guard, self.token, self.client = guard, token, client
        self.stop_file = tmp / f"stop-{id(self)}"
        self.status_file = tmp / f"status-{id(self)}"
        self.proc: asyncio.subprocess.Process | None = None
        self.log = ""

    async def start(self) -> None:
        env = {
            **os.environ,
            "DAEDALUS_TEST_RELAY_SERVER": self.base,
            "DAEDALUS_TEST_RELAY_TOKEN": self.token,
            "DAEDALUS_TEST_RELAY_GUARD": self.guard,
            "DAEDALUS_TEST_RELAY_CLIENT": self.client,
            "DAEDALUS_TEST_RELAY_STOP": str(self.stop_file),
            "DAEDALUS_TEST_RELAY_STATUS": str(self.status_file),
        }
        self.proc = await asyncio.create_subprocess_exec(
            BINARY, "-test.run", "^TestServeAnExternalRelay$", "-test.v", "-test.timeout", "120s",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )

    async def ended(self, timeout: float = 15) -> str:
        assert self.proc is not None
        out, _ = await asyncio.wait_for(self.proc.communicate(), timeout)
        self.log = out.decode()
        return self.status_file.read_text() if self.status_file.exists() else "no status: " + self.log[-2000:]

    async def stop(self) -> str:
        self.stop_file.write_text("")
        return await self.ended()


async def _connected(built: Rig, client: GoClient) -> None:
    await client.start()
    await until(lambda: built.hub.connected(DEVICE), "the Go client connected", timeout=15)


def _calls(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [row for row in rows if row["tool_call_id"]]


async def test_exec_runs_once_and_streams(settings: Settings, db: Database, root: Path, tmp_path: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo first; sleep 0.3; echo second; echo ran >> ran.log"}}, {"text": "done"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        client = GoClient(built.url, tmp_path, client="go-1")
        await _connected(built, client)
        link = built.hub.link_of(DEVICE)
        assert link is not None and link.hello.client == "go-1"
        assert {"find", "search", "stat", "list", "read", "write", "mkdir"} <= link.hello.fs_methods()
        await built.manager.submit(session_id, "build it")
        assert await built.settled(1) == ["completed"]
        [result] = _tool_results(provider)
        assert "first" in result and "second" in result and "exit_code=0" in result, result
        assert (root / "ran.log").read_text() == "ran\n"
        streamed = "".join(str(e.payload.get("delta") or "") for e in built.events if getattr(e.type, "value", e.type) == "remote_output")
        assert "first" in streamed and "second" in streamed, streamed
        rows = _calls(await built.journal.list(session_id=session_id))
        assert [(r["method"], r["state"], r["delivery"]) for r in rows] == [("exec.run", "done", "delivered")]
        assert await client.stop() == "stopped"


async def test_native_file_calls_find_and_search(settings: Settings, db: Database, root: Path, tmp_path: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        session_id = await built.session()
        client = GoClient(built.url, tmp_path)
        await _connected(built, client)
        backend = DeviceExecBackend(built.hub, built.journal, device_id=DEVICE, device_name="laptop", session_id=session_id, root=str(root), turn_active=lambda _: True)
        fs = backend.filesystem(30)
        token = call_scope.CURRENT.set(call_scope.CallScope(session_id, "call_fs", "Edit"))
        try:
            await fs.write_text(root / "src" / "notes.txt", "one\r\ntwo TODO\n")
            assert (root / "src" / "notes.txt").read_bytes() == b"one\r\ntwo TODO\n"
            assert await fs.read_text(root / "src" / "notes.txt") == "one\r\ntwo TODO\n"
            window = await fs.read_window(root / "src" / "notes.txt", start=1, max_lines=10, max_chars=1000, line_chars=100)
            assert window.lines == ["two TODO"] and window.total_lines == 2
            assert await fs.is_file(root / "src" / "notes.txt") and await fs.is_dir(root)
            assert await fs.listdir(root) == ["src"]
            assert await fs.find(root, "*.txt", 10) == ["src/notes.txt"]
            code, out, _ = await fs.search(root, "todo", glob=None, case_insensitive=True, limit=10)
            assert code == 0 and out == f"{root}/src/notes.txt:2:two TODO\n", out
            code, out, _ = await fs.search(root, "absent", glob=None, case_insensitive=False, limit=10)
            assert code == 1 and out == ""
        finally:
            call_scope.CURRENT.reset(token)
        methods = {r["method"] for r in await built.journal.list(session_id=session_id)}
        assert "exec.run" not in methods, methods
        assert {"fs.find", "fs.search", "fs.write", "fs.read"} <= methods
        assert await client.stop() == "stopped"


async def test_guard_refusals_reach_the_agent(settings: Settings, db: Database, root: Path, tmp_path: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "rm -rf build"}}, {"text": "understood"},
                                 {"tool": "Exec", "args": {"command": "ls"}}, {"text": "listed"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        client = GoClient(built.url, tmp_path, guard="plan")
        await _connected(built, client)
        await built.manager.submit(session_id, "clean up")
        assert await built.settled(1) == ["completed"]
        assert "plan mode" in _tool_results(provider)[-1]
        rows = _calls(await built.journal.list(session_id=session_id))
        assert rows[0]["state"] == "refused" and "plan_guard" in str(rows[0]["outcome"]), rows[0]
        # A read-only command still runs under plan.
        await built.manager.submit(session_id, "look")
        assert await built.settled(2) == ["completed", "completed"]
        assert "exit_code=0" in _tool_results(provider)[-1]
        assert await client.stop() == "stopped"


@pytest.mark.parametrize(("sleep", "state"), [("0.1", "done"), ("3", "running")], ids=["done-while-away", "running-at-reconnect"])
async def test_a_drop_mid_command_never_runs_it_twice(settings: Settings, db: Database, root: Path, tmp_path: Path, sleep: str, state: str) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": f"echo ran >> ran.log; sleep {sleep}; echo once"}}, {"text": "done"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        client = GoClient(built.url, tmp_path)
        await _connected(built, client)
        await built.manager.submit(session_id, "build it")
        await until(lambda: (root / "ran.log").exists(), "the command started", timeout=10)
        link = built.hub.link_of(DEVICE)
        assert link is not None
        await link.close(1001, "test drop")
        await until(lambda: built.hub.link_of(DEVICE) not in (None, link), "the client reconnected", timeout=15)
        # The reconnect's hello accounts for the request: done (fetched with device.result) or still
        # running (its answer adopted on the new link).
        relinked = built.hub.link_of(DEVICE)
        assert relinked is not None
        known = relinked.hello.known
        assert [v for k, v in known.items() if ":call_" in k] == [state], known
        await until(lambda: "completed" in [s for _, _, s in built.finished], "the turn finished", timeout=30)
        assert (root / "ran.log").read_text() == "ran\n", "the command ran twice"
        assert "once" in _tool_results(provider)[-1], _tool_results(provider)[-1]
        rows = _calls(await built.journal.list(session_id=session_id))
        assert rows[-1]["state"] == "done", rows
        assert await client.stop() == "stopped"


async def test_stop_while_running_cancels_the_command(settings: Settings, db: Database, root: Path, tmp_path: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo $$ > pid; sleep 3; echo late > late.txt"}}, {"text": "after"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        client = GoClient(built.url, tmp_path)
        await _connected(built, client)
        await built.manager.submit(session_id, "build it")
        await until(lambda: (root / "pid").exists() and bool((root / "pid").read_text().strip()), "the command started", timeout=10)
        pid = int((root / "pid").read_text())
        assert await built.manager.stop(session_id) is True

        def gone() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            return False

        await until(gone, "the command was ended by request.cancel", timeout=2.5)
        await asyncio.sleep(1.5)
        assert not (root / "late.txt").exists()
        assert await client.stop() == "stopped"


async def test_revoke_and_replace_end_the_client(settings: Settings, db: Database, root: Path, tmp_path: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        # Another process of the same device keeps its own link; the same process again replaces it.
        client = GoClient(built.url, tmp_path, client="go-1")
        await _connected(built, client)
        other = built.terminal(client="go-2")
        await other.connect()
        await asyncio.sleep(0.5)
        assert client.proc is not None and client.proc.returncode is None, "another process replaced this one"
        await other.disconnect()
        same = built.terminal(client="go-1")
        await same.connect()
        assert await client.ended() == "replaced"
        await same.disconnect()
        await until(lambda: not built.hub.connected(DEVICE), timeout=5)

        again = GoClient(built.url, tmp_path, client="go-3")
        await _connected(built, again)
        await built.hub.revoked(DEVICE)
        assert await again.ended() == "unauthorized"

        stranger = GoClient(built.url, tmp_path, token="dcl_nobody")
        await stranger.start()
        assert await stranger.ended() == "unauthorized"
