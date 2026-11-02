"""The relay to an operator's terminal client, against a fake client over a real loopback WebSocket.

The client is ``tests/support/fake_terminal.py``: it speaks the relay as ``docs/architecture/cli.md``
says and runs what it is sent in a folder of the test. The conversations here are real sessions of a
session manager against a scripted model, so what is checked is what the operator would see: the
command ran on their machine, once; a refusal in their terminal reached the agent as a refusal; a
dropped link parked the turn instead of failing it, and the turn went on where it stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from protocore.contracts.tools import ExternalCallPauseRequested, ToolPolicyDenied
from protocore.contracts.types import MessageRole
from websockets.exceptions import ConnectionClosed

from daedalus.config import Settings
from daedalus.host import call_scope
from daedalus.host.session_runner import SessionManager
from daedalus.remote.hands import NO_TURN, DeviceExecBackend
from daedalus.remote.journal import Journal
from daedalus.remote.relay import DeviceHub
from daedalus.remote.turns import EXTERNAL_CALL, PLAN_LINE, RemoteTurns, subscribe
from daedalus.stores.database import Database
from tests.support.fake_terminal import FakeDevices, FakeTerminal, relay_server
from tests.support.waiting import until
from tests.unit.test_session_runner import ScriptedProvider, _manager

DEVICE = "dev-laptop"
TOKEN = "dcl_test_token"


class Rig:
    """A session manager with the relay wired in, a relay route on a free port, and a client for it."""

    def __init__(self, manager: SessionManager, hub: DeviceHub, turns: RemoteTurns, journal: Journal, devices: FakeDevices, url: str, root: Path) -> None:
        self.manager = manager
        self.hub = hub
        self.turns = turns
        self.journal = journal
        self.devices = devices
        self.url = url
        self.root = root
        self.events: list[Any] = []
        self.finished: list[tuple[str, str, str]] = []
        self.published: list[tuple[str, dict[str, Any]]] = []

    def terminal(self, **kwargs: Any) -> FakeTerminal:
        return FakeTerminal(self.url, TOKEN, self.root, **kwargs)

    async def session(self) -> str:
        state = await self.manager.create_session("remote")
        self.turns.bind(state.session.id, DEVICE, str(self.root))
        self.devices.scope.setdefault(DEVICE, set()).add(state.session.id)
        return state.session.id

    async def settled(self, count: int) -> list[str]:
        await until(lambda: len(self.finished) >= count, timeout=30)
        return [status for _, _, status in self.finished]


@contextlib.asynccontextmanager
async def rig(settings: Settings, db: Database, provider: ScriptedProvider, root: Path) -> AsyncIterator[Rig]:
    manager = await _manager(settings, db, provider)
    devices = FakeDevices()
    devices.pair(DEVICE, TOKEN)
    published: list[tuple[str, dict[str, Any]]] = []

    async def announce(name: str, payload: dict[str, Any], session_id: str | None) -> None:
        published.append((name, payload))

    turns_box: list[RemoteTurns] = []

    async def bound(device_id: str) -> list[dict[str, str]]:
        return await turns_box[0].bound_to(device_id)

    hub = DeviceHub(lambda: devices, bound=bound, announce=announce, server_version="test")
    journal = Journal(db)
    turns = RemoteTurns(manager, hub, journal)
    turns_box.append(turns)
    subscribe(manager, turns)
    async with relay_server({"remote_relay": hub, "remote_journal": journal}) as url:
        built = Rig(manager, hub, turns, journal, devices, url, root)
        built.published = published

        async def sink(session_id: str, event: Any) -> None:
            built.events.append(event)

        async def finished(session_id: str, run_id: str, status: str) -> None:
            built.finished.append((session_id, run_id, status))

        manager.add_sink(sink)
        manager.on_finished(finished)
        try:
            yield built
        finally:
            await hub.close_all()
            await manager.close()


def _tool_results(provider: ScriptedProvider) -> list[str]:
    return [str(block.content) for message in provider.requests[-1].messages if message.role is MessageRole.tool for block in message.content_blocks]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    return folder.resolve()


# -- the door --------------------------------------------------------------------------------------


async def test_a_token_nobody_paired_is_closed_with_4401(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        stranger = FakeTerminal(built.url, "dcl_unknown", root)
        with pytest.raises((ConnectionClosed, AssertionError)) as raised:
            await stranger.connect()
        if isinstance(raised.value, ConnectionClosed):
            assert raised.value.rcvd is not None and raised.value.rcvd.code == 4401
        assert not built.hub.connected(DEVICE)


async def test_a_socket_from_a_web_page_is_refused_before_anything_is_read(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        with pytest.raises(Exception):  # noqa: B017 — refused at the handshake or closed at once, either is a refusal
            await built.terminal().connect(origin="https://example.invalid")
        assert not built.hub.connected(DEVICE)


async def test_anything_but_device_hello_first_is_a_protocol_error(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        from websockets.asyncio.client import connect

        async with connect(built.url, additional_headers={"Authorization": f"Device {TOKEN}"}) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "exec.run", "params": {}}))
            with pytest.raises(ConnectionClosed) as raised:
                await asyncio.wait_for(ws.recv(), 5)
            assert raised.value.rcvd is not None and raised.value.rcvd.code == 4400


async def test_a_second_connection_of_the_same_device_replaces_the_first(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        first, second = built.terminal(), built.terminal()
        await first.connect()
        await second.connect()
        await asyncio.wait_for(first.closed.wait(), 5)
        assert built.hub.connected(DEVICE)
        await second.disconnect()


async def test_two_processes_of_one_device_each_keep_their_link_and_the_newest_serves(settings: Settings, db: Database, root: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo one"}}, {"text": "done"}, {"tool": "Exec", "args": {"command": "echo two"}}, {"text": "done"}])
    async with rig(settings, db, provider, root) as built:
        older, newer = built.terminal(client="p1"), built.terminal(client="p2")
        await older.connect()
        await newer.connect()
        # Bound while both are up: the links carry it at once, without the clients declaring it.
        session_id = await built.session()
        await until(lambda: built.hub.link_of(DEVICE, session_id) is not None, "the new conversation is carried", timeout=5)
        assert not older.closed.is_set() and not newer.closed.is_set()
        await built.manager.submit(session_id, "first")
        assert await built.settled(1) == ["completed"]
        assert (len(older.executed), len(newer.executed)) == (0, 1), "the newest link carries the session"
        # The newest process goes: the other one serves, and the device stays connected.
        await newer.disconnect()
        await until(lambda: built.hub.link_of(DEVICE, session_id) is not None and built.hub.link_of(DEVICE, session_id).hello.client == "p1", timeout=5)
        assert [p["connected"] for name, p in built.published if name == "device.connection"] == [True]
        await built.manager.submit(session_id, "second")
        assert await built.settled(2) == ["completed", "completed"]
        assert (len(older.executed), len(newer.executed)) == (1, 1)
        # The same process reconnecting over a half-open socket replaces its own link, and only it.
        again = built.terminal(client="p1")
        await again.connect()
        await asyncio.wait_for(older.closed.wait(), 5)
        assert older.close_code == 4409
        assert built.hub.connected(DEVICE)
        await again.disconnect()
        await until(lambda: not built.hub.connected(DEVICE), timeout=5)
        assert [p["connected"] for name, p in built.published if name == "device.connection"] == [True, False]


async def test_the_welcome_names_the_conversations_bound_to_the_device_and_guards_stay_in_scope(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        session_id = await built.session()
        client = built.terminal(guard="plan")
        client.sessions = [session_id, "someone-elses-session"]
        await client.connect()
        assert client.welcome is not None and client.welcome["bound"] == [{"session_id": session_id, "cwd": str(root)}]
        assert client.welcome["contract"] == 1
        await until(lambda: built.hub.guard(DEVICE, session_id) == "plan", timeout=5)
        # A session outside the device's scope is not the device's to guard.
        assert built.hub.guard(DEVICE, "someone-elses-session") == "auto"
        state = built.manager.live_state(session_id)
        assert state is not None and await built.turns.guard_line(state) == PLAN_LINE
        await client.notify("device.guard", {"session_id": session_id, "guard": "auto"})
        await until(lambda: built.hub.guard(DEVICE, session_id) == "auto", timeout=5)
        assert ("remote.guard", {"device_id": DEVICE, "guard": "plan"}) in built.published
        await client.disconnect()


# -- the backend -----------------------------------------------------------------------------------


async def test_nothing_runs_on_the_operators_machine_outside_a_turn(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        session_id = await built.session()
        client = built.terminal()
        await client.connect()
        backend = DeviceExecBackend(built.hub, built.journal, device_id=DEVICE, device_name="laptop", session_id=session_id, root=str(root), turn_active=lambda _: False)
        with pytest.raises(ToolPolicyDenied, match=NO_TURN):
            await backend.run("echo hi", cwd=None, env=None, timeout=10)
        token = call_scope.CURRENT.set(call_scope.CallScope(session_id, "call_x", "Exec"))
        try:
            with pytest.raises(ToolPolicyDenied, match=NO_TURN):
                await backend.run("echo hi", cwd=None, env=None, timeout=10)
        finally:
            call_scope.CURRENT.reset(token)
        assert client.executed == []
        await client.disconnect()


async def test_a_call_with_its_terminal_away_parks_instead_of_failing(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        session_id = await built.session()
        backend = DeviceExecBackend(built.hub, built.journal, device_id=DEVICE, device_name="laptop", session_id=session_id, root=str(root), turn_active=lambda _: True)
        token = call_scope.CURRENT.set(call_scope.CallScope(session_id, "call_y", "Exec"))
        try:
            with pytest.raises(ExternalCallPauseRequested) as parked:
                await backend.run("echo hi", cwd=None, env=None, timeout=10)
        finally:
            call_scope.CURRENT.reset(token)
        assert parked.value.payload["delivery"] == "not_delivered"
        entry = await built.journal.get(f"{session_id}:call_y:1")
        assert entry is not None and entry.state == "held" and entry.delivery == "not_delivered"


async def test_native_file_calls_read_and_write_the_bound_folder(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        session_id = await built.session()
        client = built.terminal()
        await client.connect()
        backend = DeviceExecBackend(built.hub, built.journal, device_id=DEVICE, device_name="laptop", session_id=session_id, root=str(root), turn_active=lambda _: True)
        fs = backend.filesystem(30)
        token = call_scope.CURRENT.set(call_scope.CallScope(session_id, "call_fs", "Edit"))
        try:
            await fs.write_text(root / "notes.txt", "one\r\ntwo\n")
            assert (root / "notes.txt").read_bytes() == b"one\r\ntwo\n"
            assert await fs.read_text(root / "notes.txt") == "one\r\ntwo\n"
            window = await fs.read_window(root / "notes.txt", start=1, max_lines=10, max_chars=1000, line_chars=100)
            assert window.lines == ["two"] and window.total_lines == 2
            assert await fs.is_file(root / "notes.txt") and await fs.is_dir(root)
            assert await fs.listdir(root) == ["notes.txt"]
        finally:
            call_scope.CURRENT.reset(token)
        methods = [message["method"] for message in client.requests]
        assert "exec.run" not in methods, "a file call went through the shell although the client serves it natively"
        rows = await built.journal.list(session_id=session_id)
        assert {row["intent"] for row in rows if row["method"] == "fs.write"} == {"write"}
        assert {row["intent"] for row in rows if row["method"] == "fs.read"} == {"read"}
        await client.disconnect()


# -- conversations ---------------------------------------------------------------------------------


async def test_exec_runs_on_the_terminal_and_streams_its_output(settings: Settings, db: Database, root: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo first; sleep 0.2; echo second"}}, {"text": "done"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        client = built.terminal()
        await client.connect()
        await built.manager.submit(session_id, "build it")
        assert await built.settled(1) == ["completed"]
        assert len(client.executed) == 1
        [result] = _tool_results(provider)
        assert "first" in result and "second" in result and "exit_code=0" in result
        streamed = "".join(str(event.payload.get("delta") or "") for event in built.events if getattr(event.type, "value", event.type) == "remote_output")
        assert "first" in streamed and "second" in streamed
        envelope = next(event.payload["_session_envelope"] for event in built.events if getattr(event.type, "value", event.type) == "remote_output")
        assert "ephemeral" in envelope, "a command's output is live only, never stored for replay"
        rows = [row for row in await built.journal.list(session_id=session_id) if row["tool_call_id"]]
        assert [(row["method"], row["state"], row["delivery"]) for row in rows] == [("exec.run", "done", "delivered")]
        # The host's own reads at the turn's start (the notes files) are audited too, with no call behind them.
        assert all(row["intent"] == "read" for row in await built.journal.list(session_id=session_id) if not row["tool_call_id"])
        await client.disconnect()


async def test_a_refusal_in_the_terminal_reaches_the_agent_as_a_refusal(settings: Settings, db: Database, root: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "rm -rf build"}}, {"text": "understood"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        client = built.terminal(guard="ask")
        await client.connect()
        await built.manager.submit(session_id, "clean up")
        assert await built.settled(1) == ["completed"]
        assert client.executed == []
        [result] = _tool_results(provider)
        assert "The operator refused this in their terminal: not now" in result
        rows = [row for row in await built.journal.list(session_id=session_id) if row["tool_call_id"]]
        assert rows[0]["state"] == "refused" and "refused_by_operator" in rows[0]["outcome"]
        await client.disconnect()


async def test_a_terminal_away_from_the_start_parks_the_turn_until_it_connects(settings: Settings, db: Database, root: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo built"}}, {"text": "done"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        await built.manager.submit(session_id, "build it")
        assert await built.settled(1) == ["awaiting"]
        state = built.manager.live_state(session_id)
        assert state is not None and state.pending is not None and state.pending.kind == EXTERNAL_CALL
        assert [call["delivery"] for call in state.pending.payload["calls"]] == ["not_delivered"]
        assert any(name == "remote.waiting" and payload["waiting"] for name, payload in built.published)
        with pytest.raises(RuntimeError, match="reconnect"):
            await built.manager.answer(session_id, [{"selected": ["x"]}], via="app")

        client = built.terminal()
        await client.connect()
        assert await built.settled(2) == ["awaiting", "completed"]
        assert len(client.executed) == 1
        assert "built" in _tool_results(provider)[-1]
        assert state.pending is None
        assert ("remote.waiting", {"waiting": False, "device_id": DEVICE}) in built.published
        await client.disconnect()


async def test_a_link_dropped_mid_command_parks_and_resumes_without_running_it_twice(settings: Settings, db: Database, root: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 0.3; echo built-once"}}, {"text": "done"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        client = built.terminal()
        client.drop_on = {"exec.run"}
        await client.connect()
        await built.manager.submit(session_id, "build it")
        assert await built.settled(1) == ["awaiting"]
        state = built.manager.live_state(session_id)
        assert state is not None and state.pending is not None
        assert [call["delivery"] for call in state.pending.payload["calls"]] == ["unknown"]
        # The command went on on the operator's machine and finished while the link was down.
        await until(lambda: bool(client.unsent), timeout=10)

        await client.connect()
        assert await built.settled(2) == ["awaiting", "completed"]
        assert len(client.executed) == 1, "the command ran again after the reconnect"
        assert "built-once" in _tool_results(provider)[-1]
        assert [message["method"] for message in client.requests].count("device.result") == 1
        await client.disconnect()


async def test_a_command_still_running_at_the_reconnect_is_waited_for_not_run_again(settings: Settings, db: Database, root: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 1; echo slow-once"}}, {"text": "done"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        client = built.terminal()
        client.drop_on = {"exec.run"}
        await client.connect()
        await built.manager.submit(session_id, "build it")
        assert await built.settled(1) == ["awaiting"]
        assert client.running, "the command should still be running on the terminal"

        await client.connect()
        assert await built.settled(2) == ["awaiting", "completed"]
        assert len(client.executed) == 1
        assert "slow-once" in _tool_results(provider)[-1]
        await client.disconnect()


async def test_stop_ends_a_parked_turn_and_the_terminal_coming_back_runs_nothing(settings: Settings, db: Database, root: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo never"}}, {"text": "after"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        await built.manager.submit(session_id, "build it")
        assert await built.settled(1) == ["awaiting"]

        assert await built.manager.stop(session_id) is True
        assert await built.settled(2) == ["awaiting", "cancelled"]
        state = built.manager.live_state(session_id)
        assert state is not None and state.pending is None
        history = await built.manager.sessions.list_messages(session_id, "daedalus", limit=100)
        closing = [block for message in history if message.role is MessageRole.tool for block in message.content_blocks]
        assert closing and "did not run" in str(closing[-1].content)
        rows = [row for row in await built.journal.list(session_id=session_id) if row["tool_call_id"]]
        assert [row["state"] for row in rows] == ["cancelled"]

        client = built.terminal()
        await client.connect()
        await asyncio.sleep(0.3)
        assert client.executed == []
        assert len(built.finished) == 2
        await client.disconnect()


async def test_a_parked_turn_survives_a_restart_and_goes_on_when_the_terminal_connects(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([{"tool": "Exec", "args": {"command": "echo after-restart"}}]), root) as built:
        session_id = await built.session()
        await built.manager.submit(session_id, "build it")
        assert await built.settled(1) == ["awaiting"]

    provider = ScriptedProvider([{"text": "done"}])
    async with rig(settings, db, provider, root) as built:
        # The pairing's store would hand the home over at start (``RemoteTurns.load``).
        built.turns.bind(session_id, DEVICE, str(root))
        built.devices.scope.setdefault(DEVICE, set()).add(session_id)
        assert await built.manager.resume_unfinished() == []
        state = built.manager.live_state(session_id)
        assert state is not None and state.pending is not None and state.pending.kind == EXTERNAL_CALL
        assert any(name == "remote.waiting" and payload["waiting"] for name, payload in built.published)

        client = built.terminal()
        await client.connect()
        assert await built.settled(1) == ["completed"]
        assert len(client.executed) == 1
        assert "after-restart" in _tool_results(provider)[-1]
        await client.disconnect()


async def test_revoking_the_device_ends_its_live_relay(settings: Settings, db: Database, root: Path) -> None:
    async with rig(settings, db, ScriptedProvider([]), root) as built:
        client = built.terminal()
        await client.connect()
        assert built.hub.connected(DEVICE)
        await built.hub.revoked(DEVICE)
        await asyncio.wait_for(client.closed.wait(), 5)
        await until(lambda: not built.hub.connected(DEVICE), timeout=5)
        # Announced by the control book, which keeps when the device went.
        assert [(p["connected"], bool(p["disconnected_at"])) for name, p in built.published if name == "device.connection"] == [(True, False), (False, True)]


async def test_a_message_sent_while_the_turn_waits_is_queued_for_it_not_taken_as_an_answer(settings: Settings, db: Database, root: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo built"}}, {"text": "done"}, {"text": "and the rest"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        await built.manager.submit(session_id, "build it")
        assert await built.settled(1) == ["awaiting"]
        await built.manager.submit(session_id, "also tidy the readme")
        state = built.manager.live_state(session_id)
        assert state is not None and state.pending is not None and state.pending.kind == EXTERNAL_CALL

        client = built.terminal()
        await client.connect()
        await until(lambda: "completed" in [status for _, _, status in built.finished], timeout=30)
        sent = [str(block.text) for request in provider.requests for message in request.messages if message.role is MessageRole.user for block in message.content_blocks if hasattr(block, "text")]
        assert any("also tidy the readme" in text for text in sent)
        assert len(client.executed) == 1
        await client.disconnect()


async def test_stop_while_a_command_runs_on_the_terminal_tells_it_to_end_the_command(settings: Settings, db: Database, root: Path) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 3; echo late"}}, {"text": "after"}])
    async with rig(settings, db, provider, root) as built:
        session_id = await built.session()
        client = built.terminal()
        await client.connect()
        await built.manager.submit(session_id, "build it")
        await until(lambda: bool(client.running), timeout=10)
        assert await built.manager.stop(session_id) is True
        assert (await built.settled(1))[-1] == "cancelled"
        await until(lambda: any(note["method"] == "request.cancel" for note in client.notifications), timeout=5)
        [cancel] = [note for note in client.notifications if note["method"] == "request.cancel"]
        assert cancel["params"]["id"] == client.executed[0]
        rows = [row for row in await built.journal.list(session_id=session_id) if row["tool_call_id"]]
        assert rows[0]["state"] == "cancelled"
        await client.disconnect()
