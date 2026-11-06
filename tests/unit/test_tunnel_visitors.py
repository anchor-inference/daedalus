"""A visitor through a tunnel is a stranger, however local the program that carried them.

The router writes ``X-Daedalus-Through`` on everything it passes (``tunneld/router_test.go`` proves it,
along with what it strips). These are the same requests arriving at the host, with the peer a tunnel's
client gives them: loopback. A forged header can only ever take trust away.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import HTTPServer
from typing import Any

import pytest
import websockets
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from daedalus.extensions import api_reveal
from daedalus.extensions.api import build_app, server_config
from daedalus.extensions.api_proxy import OWN_COOKIE_PREFIXES
from daedalus.extensions.api_services_door import SCOPE_KEY, ServicesDoor
from daedalus.extensions.services import PHONE_COOKIE_PREFIX, SHARE_COOKIE_PREFIX
from daedalus.host.reveal import THROUGH_HEADER, came_through, is_local_client, is_local_request
from tests.unit.test_services_origin import FakeApp, Upstream, operator_cookie


@pytest.fixture
def upstream() -> Any:
    Upstream.seen = []
    server = HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


class Peer:
    def __init__(self, host: str | None, headers: dict[str, str], scope: dict[str, Any] | None = None) -> None:
        self.client = type("Client", (), {"host": host})() if host else None
        self.headers = headers
        self.scope = scope or {}


LOCAL = {"host": "127.0.0.1:8765"}


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765", "localhost", "LOCALHOST:1"])
def test_a_browser_on_this_machine_is_local(host: str) -> None:
    assert is_local_request("127.0.0.1", {"host": host})


def test_the_peer_alone_does_not_decide() -> None:
    assert is_local_client("127.0.0.1"), "the address is this machine's; the person need not be"
    assert not is_local_request("127.0.0.1", {**LOCAL, THROUGH_HEADER: "tunnel"}), "the router's mark"
    assert not is_local_request("127.0.0.1", LOCAL, door=True), "a door is for outside"
    assert not is_local_request("192.0.2.1", LOCAL), "another machine"
    assert not is_local_request(None, LOCAL)


@pytest.mark.parametrize("header", ["forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip", "cf-connecting-ip", "true-client-ip"])
def test_anything_that_forwarded_it_makes_it_not_local(header: str) -> None:
    assert not is_local_request("127.0.0.1", {**LOCAL, header: "127.0.0.1"}), "even when what it claims is local"


@pytest.mark.parametrize("host", ["daedalus.example.ts.net", "x.trycloudflare.com:443", "192.0.2.1:8765", "", "localhost.example.test"])
def test_a_client_aimed_at_the_apps_port_by_hand_is_caught_by_the_name_it_was_reached_under(host: str) -> None:
    """Nothing here wrote this tunnel, so no header can be relied on; a Host cannot lie about where it came from
    without the browser at the far end having asked for the wrong site."""
    assert not is_local_request("127.0.0.1", {"host": host})


def test_a_visitor_cannot_undo_the_mark() -> None:
    assert came_through({THROUGH_HEADER: ""}), "its presence is the whole of it: no value says otherwise"
    assert came_through({THROUGH_HEADER: "local"})


def test_reveal_is_not_offered_through_a_tunnel() -> None:
    assert api_reveal.available(True, Peer("127.0.0.1", LOCAL))  # type: ignore[arg-type]
    assert not api_reveal.available(True, Peer("127.0.0.1", {**LOCAL, THROUGH_HEADER: "tunnel"}))  # type: ignore[arg-type]
    assert not api_reveal.available(True, Peer("127.0.0.1", LOCAL, {SCOPE_KEY: True}))  # type: ignore[arg-type]
    assert not api_reveal.available(False, Peer("127.0.0.1", LOCAL))  # type: ignore[arg-type]


def test_no_loopback_peer_is_believed_about_who_it_is() -> None:
    """Off by default natively; ``serve`` passes nothing there even if the setting is filled in. A tunnel's
    client would otherwise name any visitor, and any visitor would name themselves."""
    config = server_config(FastAPI(), trusted_proxies="")
    assert config.proxy_headers is False
    assert config.forwarded_allow_ips == ""


def test_a_forged_address_does_not_become_the_peer_at_the_door(upstream: HTTPServer) -> None:
    door = TestClient(ServicesDoor(build_app(FakeApp(upstream.server_port, services_public_url="https://s.example.test"), "tok")), base_url="https://s.example.test")  # type: ignore[arg-type]
    forged = {"x-forwarded-for": "127.0.0.1", "x-real-ip": "127.0.0.1", THROUGH_HEADER: "tunnel", "x-daedalus-token": "tok"}
    assert door.get("/s/demo-ab12/", headers=forged).status_code == 200
    seen = Upstream.seen[-1]
    assert THROUGH_HEADER not in seen and "x-daedalus-token" not in seen, "a service hears neither the mark nor a credential"
    assert seen["x-forwarded-proto"] == "https", "a tunnel is https at the visitor's end, and apps build links from this"


def test_a_tunnels_visitor_reaches_nothing_but_s_at_the_door(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test")
    door = TestClient(ServicesDoor(build_app(app, "tok")), base_url="https://s.example.test")  # type: ignore[arg-type]
    door.cookies.update(operator_cookie(app))
    for path in ("/api/auth/me", "/api/reveal", "/app/", "/pages/x/", "/c/x", "/"):
        assert door.get(path, headers={THROUGH_HEADER: "tunnel"}).status_code == 404, path


# -- a WebSocket under /s/ -------------------------------------------------------------------------


class SocketServer:
    """A dev server's socket, on a real port: it echoes, and notes what it was sent."""

    def __enter__(self) -> SocketServer:
        self.seen: dict[str, Any] = {}
        started = threading.Event()

        async def echo(connection: Any) -> None:
            self.seen = {"path": connection.request.path, "headers": {k.lower(): v for k, v in connection.request.headers.raw_items()}, "subprotocol": connection.subprotocol}
            async for message in connection:
                await connection.send(f"echo:{message}")

        async def main() -> None:
            self.stop = asyncio.get_running_loop().create_future()
            async with websockets.serve(echo, "127.0.0.1", 0, select_subprotocol=lambda _, offered: offered[0] if offered else None) as server:
                self.port = next(iter(server.sockets)).getsockname()[1]
                started.set()
                await self.stop

        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_until_complete, args=(main(),), daemon=True)
        self.thread.start()
        assert started.wait(5)
        return self

    def __exit__(self, *_: Any) -> None:
        self.loop.call_soon_threadsafe(self.stop.set_result, None)
        self.thread.join(5)


def door_client(port: int, *, mode: str = "public") -> tuple[FakeApp, TestClient]:
    """The fake's two services: ``demo-ab12`` is public, unless told otherwise, and ``vault-cd34`` has a key."""
    app = FakeApp(port, services_public_url="https://s.example.test")
    app.extensions["services"].rows["demo-ab12"]["share_mode"] = mode
    return app, TestClient(ServicesDoor(build_app(app, "tok")), base_url="https://s.example.test")  # type: ignore[arg-type]


@pytest.fixture
def alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("daedalus.extensions.api.pid_alive", lambda _: True)


def test_a_hot_reload_gets_through_the_door(alive: None) -> None:
    with SocketServer() as server:
        _, door = door_client(server.port)
        with door.websocket_connect("/s/demo-ab12/hmr?token=t", subprotocols=["vite-hmr"], headers={"origin": "https://s.example.test", "host": "s.example.test"}) as socket:
            socket.send_text("ping")
            assert socket.receive_text() == "echo:ping"
            assert socket.accepted_subprotocol == "vite-hmr"
    assert server.seen["path"] == "/hmr?token=t"
    assert server.seen["headers"]["x-forwarded-prefix"] == "/s/demo-ab12"


def test_a_socket_from_another_site_is_sent_away(alive: None) -> None:
    with SocketServer() as server:
        _, door = door_client(server.port)
        with pytest.raises(WebSocketDisconnect), door.websocket_connect("/s/demo-ab12/hmr", headers={"origin": "https://evil.example.test", "host": "s.example.test"}):
            pass
    assert not server.seen


def test_a_key_service_needs_its_key_on_a_socket_too(alive: None) -> None:
    with SocketServer() as server:
        _, door = door_client(server.port)
        with pytest.raises(WebSocketDisconnect), door.websocket_connect("/s/vault-cd34/hmr"):
            pass
        with pytest.raises(WebSocketDisconnect), door.websocket_connect("/s/vault-cd34/hmr?key=wrong"):
            pass
        assert not server.seen
        door.cookies.set("dshare-vault-cd34", "s3cret")
        with door.websocket_connect("/s/vault-cd34/hmr") as socket:
            socket.send_text("a")
            assert socket.receive_text() == "echo:a"


def test_the_key_and_the_operators_cookie_stop_at_the_door(alive: None) -> None:
    with SocketServer() as server:
        app, door = door_client(server.port)
        door.cookies.update(operator_cookie(app))
        with door.websocket_connect("/s/vault-cd34/hmr?key=s3cret&x=1") as socket:
            socket.send_text("a")
            socket.receive_text()
    assert server.seen["path"] == "/hmr?x=1"
    assert "cookie" not in server.seen["headers"], "the query's key stops here, and the operator's cookie with it"


def test_a_socket_hears_none_of_this_doors_cookies_and_keeps_the_services_own(alive: None) -> None:
    with SocketServer() as server:
        app, door = door_client(server.port)
        door.cookies.update(operator_cookie(app))
        door.cookies.set("dshare-vault-cd34", "s3cret")
        door.cookies.set("dphone-other", "a-pass")
        door.cookies.set("theirs", "b")
        with door.websocket_connect("/s/vault-cd34/hmr") as socket:
            socket.send_text("a")
            socket.receive_text()
    assert server.seen["headers"]["cookie"] == "theirs=b"


def test_a_page_hears_none_of_this_doors_cookies_either(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test")
    door = TestClient(ServicesDoor(build_app(app, "tok")), base_url="https://s.example.test")  # type: ignore[arg-type]
    door.cookies.set("dshare-vault-cd34", "s3cret")
    door.cookies.set("theirs", "b")
    assert door.get("/s/vault-cd34/").status_code == 200
    assert Upstream.seen[-1]["cookie"] == "theirs=b"


def test_this_doors_cookie_names_are_the_ones_it_sets() -> None:
    assert (SHARE_COOKIE_PREFIX, PHONE_COOKIE_PREFIX) == OWN_COOKIE_PREFIXES


def test_an_underscored_header_does_not_reach_a_service(upstream: HTTPServer) -> None:
    """WSGI folds ``_`` and ``-`` to one variable, so this would be a forged address in a service's hands."""
    door = TestClient(ServicesDoor(build_app(FakeApp(upstream.server_port, services_public_url="https://s.example.test"), "tok")), base_url="https://s.example.test")  # type: ignore[arg-type]
    assert door.get("/s/demo-ab12/", headers={"x_forwarded_for": "198.51.100.99", "x_real_ip": "127.0.0.1"}).status_code == 200
    assert not {"x_forwarded_for", "x_real_ip"} & set(Upstream.seen[-1])


def test_a_service_that_is_down_or_unshared_closes_the_socket(alive: None, monkeypatch: pytest.MonkeyPatch) -> None:
    with SocketServer() as server:
        _, door = door_client(server.port, mode="local")
        with pytest.raises(WebSocketDisconnect), door.websocket_connect("/s/demo-ab12/hmr"):
            pass
        _, door = door_client(server.port)
        monkeypatch.setattr("daedalus.extensions.api.pid_alive", lambda _: False)
        with pytest.raises(WebSocketDisconnect), door.websocket_connect("/s/demo-ab12/hmr"):
            pass
    assert not server.seen


def test_on_the_apps_address_only_a_sandboxed_page_with_no_door_may_open_one(alive: None) -> None:
    with SocketServer() as server:
        app = FakeApp(server.port)  # an https app with no name for the door: the sandbox
        site = TestClient(build_app(app, "tok"), base_url="https://app.example.test")  # type: ignore[arg-type]
        with pytest.raises(WebSocketDisconnect), site.websocket_connect("/s/demo-ab12/hmr", headers={"origin": "https://app.example.test"}):
            pass
        with site.websocket_connect("/s/demo-ab12/hmr", headers={"origin": "null"}) as socket:
            socket.send_text("a")
            assert socket.receive_text() == "echo:a"
        # With a door, a page lives there and its socket does too.
        app = FakeApp(server.port, services_public_url="https://s.example.test")
        site = TestClient(build_app(app, "tok"), base_url="https://app.example.test")  # type: ignore[arg-type]
        with pytest.raises(WebSocketDisconnect), site.websocket_connect("/s/demo-ab12/hmr", headers={"origin": "null"}):
            pass


def test_nothing_of_the_app_answers_a_socket_at_the_door(alive: None) -> None:
    with SocketServer() as server:
        _, door = door_client(server.port)
        for path in ("/ws/anything", "/api/terminals/x/socket", "/pages/x/y", "/app/"):
            with pytest.raises(WebSocketDisconnect), door.websocket_connect(path):
                pass
