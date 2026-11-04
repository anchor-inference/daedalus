"""The terminal WebSocket as a browser meets it: tickets, the Origin rule, the relay and the audit.

A real HTTP server on a loopback port serves the real application, and a real WebSocket client talks
to it, so the close codes are the ones a browser would see (a refusal during the handshake would
reach it as a bare 1006, which is why the gateway accepts first and closes with a code). Behind it
are in-process daemons speaking the real socket protocol, one per environment.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import tempfile
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import terminals as terminals_extension
from daedalus.extensions.api import build_app, server_config
from daedalus.host.session_runner import SessionManager
from daedalus.stores import pairing
from daedalus.stores.database import Database
from daedalus.terminals.gateway import AUDITED_ENVS, TERMINAL_WS_MAX_BYTES, TicketBook, allowed_origin
from daedalus.terminals.model import Owner, TerminalSpec
from daedalus.terminals.service import Terminals
from tests.support.fake_ptyd import FakeChannel, FakePtyd

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = json.loads((ROOT / "miniapp" / "src" / "terminal" / "testdata" / "frames.json").read_text())
H = {"X-Daedalus-Token": "tok"}
BOT_TOKEN = "123:bot"


def golden(direction: str) -> list[bytes]:
    return [bytes.fromhex(f["hex"]) for f in GOLDEN["frames"] if f["direction"] == direction]


@pytest.fixture
def base() -> Iterable[Path]:
    path = Path(tempfile.mkdtemp(prefix="ptyd-"))  # short: a unix socket path is at most 108 bytes
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def gateway_settings(settings: Settings, base: Path) -> Settings:
    return settings.model_copy(update={"terminals_container_dir": base / "c", "terminals_host_dir": base / "h", "telegram_bot_token": BOT_TOKEN, "owner_user_id": 1})


@pytest.fixture
async def daemons(base: Path) -> AsyncIterator[dict[str, FakePtyd]]:
    made = {"container": await FakePtyd(base / "c").start(), "host": await FakePtyd(base / "h", env="host").start()}
    yield made
    for fake in made.values():
        await fake.stop()


@pytest.fixture
async def manager(gateway_settings: Settings, db: Database) -> AsyncIterator[SessionManager]:
    made = SessionManager(gateway_settings, RuntimeConfig(), db=db)
    await made.start()
    yield made
    await made.close()


@pytest.fixture
async def app(gateway_settings: Settings, db: Database, manager: SessionManager, daemons: dict[str, FakePtyd]) -> AsyncIterator[Any]:
    application = SimpleNamespace(settings=gateway_settings, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    tasks = await terminals_extension.install(application)  # type: ignore[arg-type]
    for env in ("container", "host"):
        assert await application.extensions["terminals"].wait_available(env)
    yield application
    for task in tasks:
        task.cancel()
    await application.extensions["terminals"].close()


class Served:
    def __init__(self, app: Any, api: Any, host: str, daemons: dict[str, FakePtyd]) -> None:
        self.app = app
        self.api = api
        self.host = host
        self.daemons = daemons

    @property
    def terminals(self) -> Terminals:
        return self.app.extensions["terminals"]  # type: ignore[no-any-return]

    @property
    def gateway(self) -> Any:
        return self.api.state.terminal_gateway

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=f"http://{self.host}")

    async def terminal(self, env: str = "container") -> str:
        view = await self.terminals.create(TerminalSpec(env=env, owner=Owner("free"), cwd="/tmp"))
        return str(view["id"])

    async def ticket(self, terminal_id: str, *, read_only: bool = False) -> str:
        async with self.http() as http:
            response = await http.post(f"/api/terminals/{terminal_id}/ticket", json={"read_only": read_only}, headers=H)
        assert response.status_code == 200, response.text
        return str(response.json()["ticket"])

    def socket(self, terminal_id: str, ticket: str, *, origin: str | None = "same") -> Any:
        chosen = f"http://{self.host}" if origin == "same" else origin
        return connect(f"ws://{self.host}/ws/terminals/{terminal_id}?ticket={ticket}", origin=Origin(chosen) if chosen else None, max_size=4 << 20, open_timeout=5)


@pytest.fixture
async def served(app: Any, daemons: dict[str, FakePtyd]) -> AsyncIterator[Served]:
    api = build_app(app, "tok")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    # The server as production runs it: its own cap is the relay's 64 MiB, so the terminal's 1 MiB is
    # the socket limits' to hold (test_a_message_past_the_socket_limit_is_refused_by_the_server).
    server = uvicorn.Server(server_config(api, lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started
    yield Served(app, api, f"127.0.0.1:{port}", daemons)
    server.should_exit = True
    await asyncio.wait_for(task, 10)
    listener.close()


async def closed_with(ws: ClientConnection, timeout: float = 5.0) -> int | None:
    """The close code the server sent, after draining whatever came before it."""
    try:
        while True:
            await asyncio.wait_for(ws.recv(), timeout)
    except ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd is not None else None


def _daemon(served: Served, env: str) -> FakePtyd:
    return served.daemons[env]


async def attached(daemon: FakePtyd) -> FakeChannel:
    return await asyncio.wait_for(daemon.attached.get(), 5)


# -- the Origin rule and the ticket book, alone ------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "host", "public", "allowed"),
    [
        ("http://127.0.0.1:8765", "127.0.0.1:8765", "", True),
        ("https://agent.example.com", "agent.example.com", "", True),
        ("https://agent.example.com", "agent.example.com:443", "", True),
        ("http://Agent.Example.com:80", "agent.example.com", "", True),
        ("http://[::1]:8765", "[::1]:8765", "", True),
        ("https://agent.example.com", "127.0.0.1:8765", "https://agent.example.com/app", True),
        ("https://agent.example.com:8443", "127.0.0.1:8765", "https://agent.example.com/app", False),
        ("http://agent.example.com", "127.0.0.1:8765", "https://agent.example.com", False),
        ("https://evil.example.org", "127.0.0.1:8765", "https://agent.example.com", False),
        ("http://127.0.0.1:5173", "127.0.0.1:8765", "", False),
        (None, "127.0.0.1:8765", "", False),
        ("", "127.0.0.1:8765", "", False),
        ("null", "127.0.0.1:8765", "http://127.0.0.1:8765", False),
        ("file://", "127.0.0.1:8765", "", False),
        ("chrome-extension://abc", "abc", "", False),
        ("http://127.0.0.1:99999", "127.0.0.1:99999", "", False),
    ],
)
def test_the_origin_rule(origin: str | None, host: str, public: str, allowed: bool) -> None:
    assert allowed_origin(origin, host, public) is allowed


def test_a_ticket_opens_once_for_its_terminal_within_its_time() -> None:
    now = [100.0]
    book = TicketBook(ttl=30, max_tickets=3, clock=lambda: now[0])
    first = book.issue("t1", False, {"via": "token"})
    assert book.take(first, "t1") is not None
    assert book.take(first, "t1") is None  # spent
    other = book.issue("t1", False, {})
    assert book.take(other, "t2") is None and book.take(other, "t1") is None  # the wrong terminal spends it too
    late = book.issue("t1", True, {})
    now[0] += 30
    assert book.take(late, "t1") is None
    assert book.take("", "t1") is None
    # Expired tickets go at the next issue, and a full book drops its oldest rather than growing.
    stale = [book.issue("t1", False, {}) for _ in range(3)]
    now[0] += 31
    book.issue("t1", False, {})
    assert len(book) == 1
    held = [book.issue("t1", False, {}) for _ in range(5)]
    assert len(book) == 3 and book.take(held[0], "t1") is None and book.take(held[-1], "t1") is not None
    assert all(book.take(t, "t1") is None for t in stale)


# -- refusals --------------------------------------------------------------------------------


async def test_a_wrong_origin_is_refused_and_leaves_the_ticket_unspent(served: Served) -> None:
    terminal_id = await served.terminal()
    ticket = await served.ticket(terminal_id)
    for origin in ("https://evil.example.org", None):
        async with served.socket(terminal_id, ticket, origin=origin) as ws:
            assert await closed_with(ws) == 4403
    async with served.socket(terminal_id, ticket) as ws:
        channel = await attached(_daemon(served, "container"))
        await ws.send(golden("client")[0])
        assert await channel.frame() == golden("client")[0]


async def test_a_spent_an_expired_and_a_foreign_ticket_are_refused(served: Served) -> None:
    terminal_id = await served.terminal()
    other_id = await served.terminal()
    ticket = await served.ticket(terminal_id)
    async with served.socket(terminal_id, ticket) as ws:
        await attached(_daemon(served, "container"))
    async with served.socket(terminal_id, ticket) as ws:
        assert await closed_with(ws) == 4401
    for_other = await served.ticket(other_id)
    async with served.socket(terminal_id, for_other) as ws:
        assert await closed_with(ws) == 4401
    async with served.socket(terminal_id, "made-up") as ws:
        assert await closed_with(ws) == 4401
    book: TicketBook = served.gateway.tickets
    late = await served.ticket(terminal_id)
    real_clock = book.clock
    book.clock = lambda: real_clock() + 31
    try:
        async with served.socket(terminal_id, late) as ws:
            assert await closed_with(ws) == 4401
    finally:
        book.clock = real_clock


async def test_an_unknown_terminal_and_an_absent_environment(served: Served) -> None:
    async with served.http() as http:
        assert (await http.post("/api/terminals/nope/ticket", headers=H)).status_code == 404
        assert (await http.post("/api/terminals/nope/ticket")).status_code == 401
    # Gone from the daemon between the ticket and the socket: the attach answers "no such terminal".
    terminal_id = await served.terminal()
    ticket = await served.ticket(terminal_id)
    del _daemon(served, "container").terminals[terminal_id]
    async with served.socket(terminal_id, ticket) as ws:
        assert await closed_with(ws) == 4404
    # The environment goes away: the ticket is refused with 409 (the app keeps trying), and a ticket
    # issued before it went is refused on the socket with 4409.
    host_id = await served.terminal("host")
    ticket = await served.ticket(host_id)
    await _daemon(served, "host").stop()
    for _ in range(100):
        if not served.terminals.links["host"].available:
            break
        await asyncio.sleep(0.02)
    async with served.http() as http:
        refused = await http.post(f"/api/terminals/{host_id}/ticket", headers=H)
    assert refused.status_code == 409 and refused.json()["code"] == "unavailable"
    async with served.socket(host_id, ticket) as ws:
        assert await closed_with(ws) == 4409


async def test_a_lost_terminal_gets_no_ticket(served: Served) -> None:
    terminal_id = await served.terminal()
    await served.app.db.execute("UPDATE terminals SET status = 'lost' WHERE id = ?", (terminal_id,))
    async with served.http() as http:
        assert (await http.post(f"/api/terminals/{terminal_id}/ticket", headers=H)).status_code == 404


# -- the relay -------------------------------------------------------------------------------


async def test_frames_relay_both_ways_byte_for_byte(served: Served) -> None:
    terminal_id = await served.terminal()
    async with served.socket(terminal_id, await served.ticket(terminal_id)) as ws:
        channel = await attached(_daemon(served, "container"))
        assert channel.terminal_id == terminal_id and channel.client["kind"] == "human" and channel.client["read_only"] is False
        assert channel.client["via"] == "token"
        for frame in golden("client"):
            await ws.send(frame)
            assert await channel.frame() == frame
        for frame in golden("server"):
            await _daemon(served, "container").send_channel(channel.id, frame)
            assert await asyncio.wait_for(ws.recv(), 5) == frame
        # A frame as large as the daemon's framing allows passes whole.
        big = bytes([0x01]) + (7).to_bytes(8, "big") + b"y" * ((1 << 20) - 9)
        await _daemon(served, "container").send_channel(channel.id, big)
        assert await asyncio.wait_for(ws.recv(), 5) == big


async def test_a_read_only_ticket_makes_a_watcher(served: Served) -> None:
    terminal_id = await served.terminal()
    async with served.socket(terminal_id, await served.ticket(terminal_id, read_only=True)) as ws:
        channel = await attached(_daemon(served, "container"))
        assert channel.client["read_only"] is True and channel.client["kind"] == "viewer"
        attach = json.dumps({"lastSeq": 0, "haveState": False, "readOnly": False}).encode()
        await ws.send(bytes([0x13]) + attach)
        forwarded = await channel.frame()
        assert forwarded[0] == 0x13 and json.loads(forwarded[1:]) == {"lastSeq": 0, "haveState": False, "readOnly": True}
        await ws.send(bytes([0x10]) + b"rm -rf /\r")
        ack = golden("client")[3]
        await ws.send(ack)
        assert await channel.frame() == ack  # the typing never arrived; the next frame did
        # An ATTACH that already says read-only goes through untouched.
        same = bytes([0x13]) + b'{"readOnly":true}'
        await ws.send(same)
        assert await channel.frame() == same


async def test_the_browser_leaving_closes_the_channel(served: Served) -> None:
    terminal_id = await served.terminal()
    async with served.socket(terminal_id, await served.ticket(terminal_id)) as ws:
        channel = await attached(_daemon(served, "container"))
        await ws.close()
    await asyncio.wait_for(channel.closed_by_host.wait(), 5)


async def test_the_terminal_letting_go_closes_normally(served: Served) -> None:
    terminal_id = await served.terminal()
    async with served.socket(terminal_id, await served.ticket(terminal_id)) as ws:
        channel = await attached(_daemon(served, "container"))
        await _daemon(served, "container").send_channel(channel.id, golden("server")[0])
        await _daemon(served, "container").close_channel(channel.id)
        assert await asyncio.wait_for(ws.recv(), 5) == golden("server")[0]
        assert await closed_with(ws) == 1000


async def test_a_killed_terminal_service_closes_with_1012(served: Served) -> None:
    terminal_id = await served.terminal()
    async with served.socket(terminal_id, await served.ticket(terminal_id)) as ws:
        await attached(_daemon(served, "container"))
        await _daemon(served, "container").stop()
        assert await closed_with(ws) == 1012


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (b"", 1008),
        (bytes.fromhex("7f00"), 1008),
        (golden("server")[0], 1008),  # a server's frame from a browser
        (bytes([0x11]) + b"\x00" * 7, 1008),  # RESIZE one byte short
        (bytes([0x12]) + b"\x00" * 9, 1008),  # ACK one byte long
        (bytes([0x12]) + (1 << 60).to_bytes(8, "big"), 1008),  # past what a browser counts exactly
        (bytes([0x13]) + b"[1,2]", 1008),
        (bytes([0x13]) + b'{"x":"' + b"a" * 4096 + b'"}', 1009),
        (bytes([0x10]) + b"a" * ((32 << 10) + 1), 1009),
        ("text", 1003),
    ],
)
async def test_a_frame_a_browser_must_not_send_ends_the_socket(served: Served, frame: bytes | str, code: int) -> None:
    terminal_id = await served.terminal()
    async with served.socket(terminal_id, await served.ticket(terminal_id)) as ws:
        channel = await attached(_daemon(served, "container"))
        await ws.send(bytes([0x10]) + b"a" * (32 << 10))  # the largest INPUT is fine
        assert len(await channel.frame()) == 1 + (32 << 10)
        await ws.send(frame)
        assert await closed_with(ws) == code
    await asyncio.wait_for(channel.closed_by_host.wait(), 5)


async def test_a_message_past_the_socket_limit_is_refused_by_the_server(served: Served) -> None:
    terminal_id = await served.terminal()
    async with served.socket(terminal_id, await served.ticket(terminal_id)) as ws:
        channel = await attached(_daemon(served, "container"))
        await ws.send(bytes([0x10]) + b"a" * TERMINAL_WS_MAX_BYTES)
        assert await closed_with(ws) == 1009
    await asyncio.wait_for(channel.closed_by_host.wait(), 5)


# -- the audit -------------------------------------------------------------------------------


async def test_host_attachments_are_audited_with_counts_and_never_the_typing(served: Served) -> None:
    assert AUDITED_ENVS == {"host"}
    host_id = await served.terminal("host")
    container_id = await served.terminal("container")
    secret = b"hunter2-password\r"
    for terminal_id, env in ((host_id, "host"), (container_id, "container")):
        async with served.socket(terminal_id, await served.ticket(terminal_id)) as ws:
            channel = await attached(_daemon(served, env))
            await ws.send(bytes([0x10]) + secret)
            await ws.send(bytes([0x10]) + b"ls\r")
            assert await channel.frame() == bytes([0x10]) + secret
            await channel.frame()
            await _daemon(served, env).send_channel(channel.id, golden("server")[0])
            await asyncio.wait_for(ws.recv(), 5)
        await asyncio.wait_for(channel.closed_by_host.wait(), 5)
    for _ in range(100):
        rows = await served.terminals.audit_log(host_id)
        if rows and rows[0]["action"] == "detach":
            break
        await asyncio.sleep(0.02)
    entries = list(reversed(await served.terminals.audit_log(host_id)))
    assert [e["action"] for e in entries] == ["create", "attach", "detach"]
    attach, detach = entries[1], entries[2]
    assert attach["actor"] == "operator" and attach["env"] == "host"
    assert attach["detail"]["via"] == "token" and attach["detail"]["address"] == "127.0.0.1" and attach["detail"]["read_only"] is False
    assert attach["detail"]["user_agent"].startswith("python-httpx") and attach["detail"]["client_id"]
    assert detach["detail"]["bytes_typed"] == len(secret) + 3 and detach["detail"]["ended_by"] == "browser"
    assert detach["detail"]["bytes_out"] == len(golden("server")[0])
    raw = [r["detail_json"] for r in await served.app.db.fetchall("SELECT detail_json FROM terminal_audit")]
    assert not any("hunter2" in text or "ls\\r" in text for text in raw)
    assert [e["action"] for e in await served.terminals.audit_log(container_id)] == ["create"]


# -- tickets through every way of signing in ----------------------------------------------------


def _init_data(user_id: int) -> str:
    import hashlib
    import hmac
    from urllib.parse import urlencode

    fields = {"auth_date": str(int(time.time())), "query_id": "q", "user": json.dumps({"id": user_id, "first_name": "A"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def test_a_ticket_is_issued_through_each_way_of_signing_in(served: Served) -> None:
    terminal_id = await served.terminal("host")
    async with served.http() as http:
        by_token = await http.post(f"/api/terminals/{terminal_id}/ticket", headers=H)
        by_telegram = await http.post(f"/api/terminals/{terminal_id}/ticket", headers={"Authorization": f"tma {_init_data(1)}"})
        stranger = await http.post(f"/api/terminals/{terminal_id}/ticket", headers={"Authorization": f"tma {_init_data(2)}"})
        paired = await http.get("/api/auth/pair", params={"code": await pairing.mint(served.app.db)})
        assert paired.status_code == 303
        by_cookie = await http.post(f"/api/terminals/{terminal_id}/ticket", json={"read_only": True})
    assert stranger.status_code == 403
    tickets = {}
    for via, response in (("token", by_token), ("telegram", by_telegram), ("cookie", by_cookie)):
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["expires_in"] == 30 and len(body["ticket"]) >= 32
        tickets[via] = body["ticket"]
    for via, ticket in tickets.items():
        async with served.socket(terminal_id, ticket) as ws:
            channel = await attached(_daemon(served, "host"))
            assert channel.client["via"] == via and channel.client["read_only"] is (via == "cookie")
            await ws.close()
        await asyncio.wait_for(channel.closed_by_host.wait(), 5)
    for _ in range(100):
        vias = [e["detail"].get("via") for e in await served.terminals.audit_log(terminal_id) if e["action"] == "attach"]
        if len(vias) == 3:
            break
        await asyncio.sleep(0.02)
    assert sorted(vias) == ["cookie", "telegram", "token"]


async def test_the_doctor_warns_when_telegram_has_no_public_address(gateway_settings: Settings, app: Any) -> None:
    from daedalus.doctor import DoctorContext, _terminals  # noqa: PLC0415 — the probe alone, not the whole doctor

    bare = await _terminals(DoctorContext(settings=gateway_settings.model_copy(update={"miniapp_public_url": ""}), config=app.config, extensions=app.extensions))
    warned = [c for c in bare if c.name == "terminals in Telegram"]
    assert len(warned) == 1 and not warned[0].ok and warned[0].severity == "warn" and "MINIAPP_PUBLIC_URL" in warned[0].message
    for update in ({"miniapp_public_url": "https://agent.example.com/app"}, {"miniapp_public_url": "", "telegram_bot_token": ""}):
        fine = await _terminals(DoctorContext(settings=gateway_settings.model_copy(update=update), config=app.config, extensions=app.extensions))
        assert not [c for c in fine if c.name == "terminals in Telegram"]
