"""The guest door on an operator's own machine: no container, and on Windows no unix sockets.

The whole conversation is run on a real process in the loopback form, because that is the form nothing
in compose exercises: control port, public port, the front's way down to the door, and the token.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from guestd import confine
from guestd.__main__ import may_dial
from guestd.front import Front, door_port
from guestd.standin import KeyProxyCaller

from daedalus.extensions.guest_door import DoorClient, DoorUnavailable, ensure_token

PACKAGE = Path(__file__).resolve().parents[2] / "deploy" / "guestd"
FIELDS = {
    "label": "a", "topic": "b", "mode": "direct", "briefing": "", "model_path": "", "model_name": "", "guest_tools": "read", "price_in": 0, "price_out": 0, "approval": False,
    "usd_limit": 1, "turns_per_day": 1, "turns_total": 1, "invite_hours": 1, "conversation_hours": 1, "idle_hours": 1, "retention_days": 1,
}


async def appears(path: Path) -> None:
    for _ in range(400):
        if path.exists():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


async def test_the_door_runs_on_loopback_ports_with_no_container_and_no_unix_socket(tmp_path: Path) -> None:
    state, run, front_dir, token_dir = tmp_path / "state", tmp_path / "run", tmp_path / "front", tmp_path / "token"
    token_dir.mkdir()
    (token_dir / "control.token").write_text("host-token")
    env = {
        "PATH": "/usr/bin", "PYTHONPATH": str(PACKAGE.parent), "GUEST_STATE_DIR": str(state), "GUEST_RUN_DIR": str(run), "GUEST_FRONT_DIR": str(front_dir), "GUEST_TOKEN_DIR": str(token_dir),
        "GUEST_LISTEN": "tcp", "GUEST_CONFINE": "1",
    }
    door = subprocess.Popen([sys.executable, "-m", "guestd"], cwd=PACKAGE.parent, env=env)
    try:
        await appears(run / "control.endpoint")
        await appears(front_dir / "public.endpoint")
        assert not (run / "control.sock").exists() and not (front_dir / "public.sock").exists()
        client = DoorClient(run, token_dir=token_dir, door_uid=-1)
        await client.call("PUT", "/control/settings", json={"public_base": "https://door.example.test"})
        invite = (await client.call("POST", "/control/links", json=FIELDS))["invite"]
        front = Front(str(front_dir / "public.sock"), "127.0.0.1")
        outer = await asyncio.start_server(front.handle, "127.0.0.1", 0, limit=16 * 1024)
        port = outer.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as public:
            page = await public.get(f"/a/{invite}", headers={"x-forwarded-for": "198.51.100.4"})
            assert page.status_code == 200 and f"https://door.example.test/a/{invite}/redeem" in page.text
            assert (await public.post(f"/a/{invite}/redeem")).status_code == 201
            assert (await public.get("/control/health", headers={"authorization": "Bearer host-token"})).status_code == 404
        outer.close()
        await client.aclose()
    finally:
        door.terminate()
        assert door.wait(timeout=10) == 0, "told to end, it ends cleanly and takes its endpoints with it"
    assert not (run / "control.endpoint").exists() and not (front_dir / "public.endpoint").exists()


async def test_the_host_reads_the_doors_port_again_when_the_door_comes_back_on_another(tmp_path: Path) -> None:
    (tmp_path / "control.token").write_text("t")

    async def answer(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def serve() -> web.AppRunner:
        door = web.Application()
        door.router.add_get("/control/health", answer)
        runner = web.AppRunner(door)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        (tmp_path / "control.endpoint").write_text(f"tcp:127.0.0.1:{runner.addresses[0][1]}\n")
        return runner

    client = DoorClient(tmp_path, token_dir=tmp_path, door_uid=-1)
    first = await serve()
    assert (await client.call("GET", "/control/health"))["ok"] is True
    await first.cleanup()
    with pytest.raises(DoorUnavailable):
        await client.call("GET", "/control/health")
    second = await serve()
    assert (await client.call("GET", "/control/health"))["ok"] is True
    await second.cleanup()
    await client.aclose()


def test_a_token_is_made_with_no_other_user_to_give_it_to(tmp_path: Path) -> None:
    token = ensure_token(tmp_path, -1)
    assert token and (tmp_path / "control.token").read_text() == token
    assert ensure_token(tmp_path, -1) == token


async def test_a_stand_ins_call_finds_the_proxy_on_the_port_named_in_its_file(tmp_path: Path) -> None:
    seen: list[str] = []
    endpoint = tmp_path / "guest.endpoint"

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    caller = KeyProxyCaller("", httpx.AsyncClient(transport=httpx.MockTransport(handle)), endpoint=str(endpoint))
    assert caller.base() == "", "no file yet: no model, said as much, rather than a call to nowhere"
    endpoint.write_text("tcp:127.0.0.1:43111\n")
    await caller({"model_path": "/acme/v1"}, {"max_tokens": 10, "messages": []})
    endpoint.write_text("tcp:127.0.0.1:43112\n")
    await caller({"model_path": "/acme/v1"}, {"max_tokens": 10, "messages": []})
    assert seen == ["http://127.0.0.1:43111/acme/v1/chat/completions", "http://127.0.0.1:43112/acme/v1/chat/completions"]


async def test_the_guest_listener_takes_a_port_where_there_are_no_sockets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_keyproxy_guest import load

    endpoint = tmp_path / "guest.endpoint"
    monkeypatch.delenv("KEYPROXY_GUEST_SOCKET", raising=False)
    monkeypatch.setenv("KEYPROXY_GUEST_ENDPOINT", str(endpoint))
    proxy = load()
    app = web.Application()
    app["client"] = None
    await proxy.start_guest_listener(app)
    port = int(endpoint.read_text().strip().rsplit(":", 1)[1])
    async with httpx.AsyncClient(trust_env=False) as client:
        refused = await client.get(f"http://127.0.0.1:{port}/acme/v1/models")
    assert refused.status_code == 403 and refused.json()["error"]["type"] == "guest_refused"
    await proxy.stop_guest_listener(app)


def test_the_front_reads_the_doors_port_each_time(tmp_path: Path) -> None:
    path = tmp_path / "public.sock"
    assert door_port(str(path)) is None
    (tmp_path / "public.endpoint").write_text("tcp:127.0.0.1:40101\n")
    assert door_port(str(path)) == 40101


@pytest.mark.parametrize("event", sorted(confine.REFUSED_EVENTS))
def test_the_door_starts_no_programs(event: str) -> None:
    with pytest.raises(confine.Confined):
        confine.hook(lambda _: True)(event, ())


def test_the_door_dials_the_proxy_and_nothing_else(tmp_path: Path) -> None:
    endpoint = tmp_path / "guest.endpoint"
    endpoint.write_text("tcp:127.0.0.1:43111\n")
    audit = confine.hook(may_dial("", str(endpoint)))
    audit("socket.connect", (None, ("127.0.0.1", 43111)))
    for address in (("127.0.0.1", 3201), ("192.0.2.1", 43111), ("127.0.0.1", 8765), "/run/other.sock"):
        with pytest.raises(confine.Confined):
            audit("socket.connect", (None, address))


def test_the_door_looks_up_no_names() -> None:
    audit = confine.hook(lambda _: True)
    audit("socket.getaddrinfo", ("127.0.0.1", 80))
    with pytest.raises(confine.Confined):
        audit("socket.getaddrinfo", ("attacker.example.test", 80))


@pytest.mark.skipif(os.name == "nt", reason="a real signal")
def test_a_confined_process_really_cannot_dial_out_or_start_a_program(tmp_path: Path) -> None:
    code = f"""
import socket, subprocess, sys
sys.path.insert(0, {str(PACKAGE.parent)!r})
from guestd.confine import Confined, confine
confine(lambda address: False)
results = []
try:
    socket.create_connection(("127.0.0.1", 9), timeout=1)
    results.append("dialled")
except Confined:
    results.append("refused")
except OSError:
    results.append("dialled")
try:
    subprocess.run(["/bin/true"])
    results.append("ran")
except Confined:
    results.append("refused")
print(",".join(results))
"""
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20, env={"PATH": "/usr/bin"}, check=False)
    assert done.stdout.strip() == "refused,refused", done.stderr


@pytest.mark.parametrize("listen", ["unix", "tcp"])
async def test_a_stand_in_answers_from_a_confined_process_in_either_form(listen: str) -> None:
    """The one call a confined door makes, for real: its hook must let this through and nothing else, and
    a hook that was too strict would only have shown as a stand-in that never answers."""
    root = Path(tempfile.mkdtemp(prefix="gn", dir="/tmp"))
    seen: list[str] = []

    async def complete(request: web.Request) -> web.Response:
        seen.append(request.path)
        return web.json_response({"choices": [{"message": {"content": "an answer"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}})

    proxy = web.Application()
    proxy.router.add_post("/{tail:.*}", complete)
    runner = web.AppRunner(proxy)
    await runner.setup()
    env = {"PATH": "/usr/bin", "PYTHONPATH": str(PACKAGE.parent), "GUEST_CONFINE": "1", "GUEST_LISTEN": listen}
    for name in ("state", "run", "front", "token"):
        (root / name).mkdir()
        env[f"GUEST_{name.upper()}_DIR"] = str(root / name)
    (root / "token" / "control.token").write_text("host-token")
    if listen == "tcp":
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        (root / "guest.endpoint").write_text(f"tcp:127.0.0.1:{runner.addresses[0][1]}\n")
        env["GUEST_MODEL_ENDPOINT"] = str(root / "guest.endpoint")
    else:
        await web.UnixSite(runner, str(root / "guest.sock")).start()
        env["GUEST_MODEL_SOCKET"] = str(root / "guest.sock")
    door = subprocess.Popen([sys.executable, "-m", "guestd"], cwd=PACKAGE.parent, env=env, stderr=subprocess.PIPE, text=True)
    try:
        await appears(root / "run" / ("control.endpoint" if listen == "tcp" else "control.sock"))
        await appears(root / "front" / ("public.endpoint" if listen == "tcp" else "public.sock"))
        client = DoorClient(root / "run", token_dir=root / "token", door_uid=-1)
        await client.call("PUT", "/control/settings", json={"public_base": "https://door.example.test"})
        fields = {**FIELDS, "mode": "stand_in", "briefing": "a briefing", "model_path": "acme/v1", "model_name": "m", "turns_per_day": 5, "turns_total": 5}
        invite = (await client.call("POST", "/control/links", json=fields))["invite"]
        if listen == "tcp":
            base, transport = f"http://127.0.0.1:{door_port(str(root / 'front' / 'public.sock'))}", None
        else:
            base, transport = "http://door", httpx.AsyncHTTPTransport(uds=str(root / "front" / "public.sock"))
        async with httpx.AsyncClient(base_url=base, transport=transport, trust_env=False, timeout=20) as public:
            token = (await public.post(f"/a/{invite}/redeem", headers={"x-guest-client": "192.0.2.5"})).json()["token"]
            bearer = {"authorization": f"Bearer {token}", "x-guest-client": "192.0.2.5"}
            await public.post("/a/messages", json={"text": "hello"}, headers=bearer)
            read = (await public.get("/a/messages?after=0&wait=10", headers=bearer)).json()
        assert [m["text"] for m in read["messages"]] == ["an answer"]
        assert seen == ["/acme/v1/chat/completions"]
        await client.aclose()
    finally:
        door.terminate()
        _, errors = door.communicate(timeout=10)
        await runner.cleanup()
        shutil.rmtree(root, ignore_errors=True)
    assert "Confined" not in errors, errors


def test_a_door_told_to_stop_does_not_wait_for_a_held_read() -> None:
    from guestd.__main__ import GRACE_SECONDS, public_config

    assert public_config(object()).timeout_graceful_shutdown == GRACE_SECONDS  # type: ignore[arg-type]


def test_the_compose_door_is_told_none_of_this() -> None:
    """The container's form stays what it was: a socket, and a wall better than any of this."""
    import yaml

    compose = yaml.safe_load((PACKAGE.parents[1] / "deploy" / "compose.yaml").read_text())
    environment = compose["services"]["guest"]["environment"]
    assert "GUEST_LISTEN" not in environment and "GUEST_CONFINE" not in environment


def test_a_signal_is_handled_where_there_is_no_add_signal_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    from guestd.__main__ import on_stop

    class Windows:
        def add_signal_handler(self, *_: object) -> None:
            raise NotImplementedError

        def call_soon_threadsafe(self, stop: object) -> None:
            called.append(stop)

    called: list[object] = []
    handlers: dict[int, object] = {}
    monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)
    monkeypatch.setattr(signal, "signal", lambda signum, handler: handlers.setdefault(signum, handler))
    stop = object()
    on_stop(Windows(), stop)  # type: ignore[arg-type]
    assert signal.SIGTERM in handlers and 21 in handlers
    handlers[21](21, None)  # type: ignore[operator]
    assert called == [stop]
