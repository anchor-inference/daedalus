"""A shared service's page lives on another origin, and a neighbour's request is no use to it.

The headers in these requests are the ones a browser sends in each situation, as measured in Chromium:
same origin, another port of the same host, and a sandboxed page. ``tests/browser/check_services_origin.py``
runs the same claims with real script in a real browser.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from daedalus import public_address
from daedalus.config import RuntimeConfig, Settings
from daedalus.doctor import _shared_services
from daedalus.extensions.api import SESSION_COOKIE, build_app, server_config, session_cookie_value
from daedalus.extensions.api_proxy import ORIGIN_WIDE_HEADERS, SANDBOX_POLICY, scoped_set_cookie
from daedalus.extensions.api_services_door import SCOPE_KEY, ServicesDoor, open_door
from daedalus.extensions.dialog_share import DialogShares
from daedalus.extensions.services import Services
from daedalus.gateway.origin import cookie_may_act

APP_HOST = "app.example.test"
SITE_HEADERS = {"sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}


class Upstream(BaseHTTPRequestHandler):
    """A server an agent wrote, which tries everything a response can try."""

    seen: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802
        Upstream.seen.append({k.lower(): v for k, v in self.headers.items()})
        body = b"<script>fetch('/api/auth/me')</script>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "sandbox allow-scripts allow-same-origin")
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=planted; Path=/; Domain=example.test")
        self.send_header("Clear-Site-Data", '"cookies"')
        self.send_header("Service-Worker-Allowed", "/")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET  # noqa: N815

    def log_message(self, *args: Any) -> None:
        pass


class FakeDb:
    def __init__(self) -> None:
        self.kv = {"session_secret": "seed"}

    async def kv_get(self, key: str, default: Any = None) -> Any:
        return self.kv.get(key, default)

    async def kv_set(self, key: str, value: Any) -> None:
        self.kv[key] = value


class FakeServices:
    def __init__(self, port: int) -> None:
        base = {"status": "running", "pid": os.getpid(), "port": port, "share_key": None}
        self.rows = {"demo-ab12": {**base, "share_mode": "public"}, "vault-cd34": {**base, "share_mode": "key", "share_key": "s3cret"}}

    async def by_slug(self, slug: str) -> dict[str, Any] | None:
        return self.rows.get(slug)

    def share_allows(self, row: dict[str, Any], key: str | None) -> bool:
        return row["share_mode"] == "public" or key == row["share_key"]


class FakeManager:
    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []


class FakeApp:
    def __init__(self, port: int, **settings: Any) -> None:
        options = {"miniapp_public_url": f"https://{APP_HOST}/app", "services_door_port": 8766, **settings}
        self.settings = Settings(_env_file=None, telegram_bot_token="123:abc", owner_user_id=1, **options)  # type: ignore[call-arg]
        self.config = RuntimeConfig()
        self.manager = FakeManager()
        self.front: Any = None
        self.db = FakeDb()
        self.extensions: dict[str, Any] = {"services": FakeServices(port)}


def operator_cookie(app: FakeApp) -> dict[str, str]:
    secret = hashlib.sha256(f"session:tok:{app.db.kv['session_secret']}".encode()).digest()
    return {SESSION_COOKIE: session_cookie_value(secret, 1)}


@pytest.fixture
def upstream() -> Any:
    Upstream.seen = []
    server = HTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


def signed_in(app: FakeApp, door: bool = False) -> TestClient:
    api = build_app(app, "tok")  # type: ignore[arg-type]
    client = TestClient(ServicesDoor(api) if door else api, base_url=f"http://{APP_HOST}")
    client.cookies.update(operator_cookie(app))
    return client


# -- the rule itself --------------------------------------------------------------------------------


def rule(method: str = "POST", origin: str | None = None, site: str | None = None, mode: str | None = "cors", dest: str | None = "empty", host: str = "h:8765", public: str = "") -> bool:
    return cookie_may_act(method=method, origin=origin, fetch_site=site, fetch_mode=mode, fetch_dest=dest, host_header=host, public_url=public)


def test_the_app_itself_may_act() -> None:
    assert rule(origin="http://h:8765", site="same-origin")
    assert rule(origin="http://h:8765")  # plain http to a LAN address: no Sec-Fetch-Site at all
    assert rule(method="GET", site="same-origin", origin=None)


def test_a_proxy_that_rewrites_host_is_recognised_by_the_public_address() -> None:
    assert rule(origin="https://app.example.test", site="same-origin", host="127.0.0.1:8765", public="https://app.example.test")
    assert rule(origin="https://app.example.test", host="127.0.0.1:8765", public="https://app.example.test")


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_neighbour_may_not_act(method: str) -> None:
    assert not rule(method=method, origin="http://h:8766", site="same-site")
    assert not rule(method=method, origin="http://h:8766")  # the same, over http to a LAN address
    assert not rule(method=method, origin="https://s.example.test", site="same-site", host="app.example.test", public="https://app.example.test")


def test_a_sandboxed_page_is_null_and_null_is_nobody() -> None:
    assert not rule(origin="null", site="cross-site")
    assert not rule(origin="null")


def test_a_neighbours_reads_and_frames_are_refused_and_a_link_in_is_not() -> None:
    assert not rule(method="GET", site="same-site")
    assert not rule(method="GET", site="same-site", mode="navigate", dest="iframe")
    assert rule(method="GET", site="same-site", mode="navigate", dest="document")
    assert rule(method="GET", site="cross-site", mode="navigate", dest="document")
    assert not rule(method="POST", origin="http://h:8766", site="same-site", mode="navigate", dest="document")


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_only_a_page_that_names_its_framers_may_be_framed(site: str) -> None:
    frame = {"method": "GET", "site": site, "mode": "navigate", "dest": "iframe"}
    assert cookie_may_act(method="GET", origin=None, fetch_site=site, fetch_mode="navigate", fetch_dest="iframe", host_header="h:8765", public_url="", frameable=True)
    assert not rule(**frame)
    assert not cookie_may_act(method="POST", origin=None, fetch_site=site, fetch_mode="navigate", fetch_dest="iframe", host_header="h:8765", public_url="", frameable=True)


def test_with_neither_header_it_is_not_a_browser() -> None:
    assert rule(method="POST", origin=None, site=None, mode=None, dest=None)


# -- the guard in front of the app ------------------------------------------------------------------


def test_the_side_panel_can_open_the_app_in_its_frame_and_nothing_else_is_framed(upstream: HTTPServer) -> None:
    client = signed_in(FakeApp(upstream.server_port))
    frame = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"}
    assert client.get("/app/panel", headers=frame).status_code != 403
    assert client.get("/pages/some-page/", headers=frame).status_code == 403
    assert client.get("/api/auth/me", headers=frame).status_code == 403


def test_a_neighbours_post_is_refused_before_the_route_runs(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port)
    client = signed_in(app)
    answer = client.post("/api/auth/sessions/revoke", headers={"origin": "https://s.example.test", "sec-fetch-site": "same-site", **SITE_HEADERS})
    assert answer.status_code == 403
    assert app.db.kv["session_secret"] == "seed"


def test_the_apps_own_post_goes_through(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port)
    client = signed_in(app)
    answer = client.post("/api/auth/sessions/revoke", headers={"origin": f"https://{APP_HOST}", "sec-fetch-site": "same-origin", **SITE_HEADERS})
    assert answer.status_code == 200 and app.db.kv["session_secret"] != "seed"


def test_a_caller_with_its_own_credential_is_not_judged_by_its_origin(upstream: HTTPServer) -> None:
    client = signed_in(FakeApp(upstream.server_port))
    # The side panel's case: a foreign origin, a credential it set itself, a cookie it did not choose.
    headers = {"origin": "chrome-extension://abc", "sec-fetch-site": "cross-site", "x-daedalus-token": "tok", **SITE_HEADERS}
    assert client.get("/api/auth/me", headers=headers).json()["via"] == "token"


def test_no_cookie_means_nothing_to_guard(upstream: HTTPServer) -> None:
    client = TestClient(build_app(FakeApp(upstream.server_port), "tok"), base_url=f"http://{APP_HOST}")  # type: ignore[arg-type]
    headers = {"origin": "https://elsewhere.test", "sec-fetch-site": "cross-site", **SITE_HEADERS}
    assert client.post("/api/auth/logout", headers=headers).status_code == 200


def test_the_session_cookie_is_host_only(upstream: HTTPServer) -> None:
    # A sibling host must never receive it, and a Domain is the only way it could.
    client = signed_in(FakeApp(upstream.server_port))
    answer = client.post("/api/auth/sessions/revoke", headers={"origin": f"https://{APP_HOST}", "sec-fetch-site": "same-origin", **SITE_HEADERS})
    cookie = answer.headers["set-cookie"].lower()
    assert cookie.startswith(SESSION_COOKIE) and "samesite=lax" in cookie and "httponly" in cookie and "domain" not in cookie


def test_a_proxy_aimed_at_the_wrong_port_is_told_so_rather_than_served(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test")
    client = TestClient(build_app(app, "tok"), base_url="https://s.example.test")  # type: ignore[arg-type]
    answer = client.get("/s/demo-ab12/", follow_redirects=False)
    assert answer.status_code == 421 and "SERVICES_DOOR_PORT" in answer.text


# -- /s/ with a door --------------------------------------------------------------------------------


def test_the_door_answers_shared_services_and_nothing_else(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test")
    door = signed_in(app, door=True)
    assert door.get("/s/demo-ab12/").status_code == 200
    # The operator's own cookie, on a request that really has it, reaches nothing.
    for path in ("/api/auth/me", "/app/", "/pages/x/", "/c/x", "/", "/s", "/ss/x"):
        assert door.get(path).status_code == 404, path
    assert "Disallow: /" in door.get("/robots.txt").text


def test_a_path_cannot_climb_out_of_the_door(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test")
    door = ServicesDoor(build_app(app, "tok"))  # type: ignore[arg-type]
    answers: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        answers.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    secret = hashlib.sha256(b"session:tok:seed").digest()
    cookie = f"{SESSION_COOKIE}={session_cookie_value(secret, 1)}".encode()
    # Starlette is not a browser and uvicorn does not resolve a path: this is what arrives.
    scope = {"type": "http", "method": "GET", "path": "/s/../api/auth/me", "raw_path": b"/s/../api/auth/me", "query_string": b"", "headers": [(b"host", b"x"), (b"cookie", cookie)], "scheme": "http", "server": ("x", 80), "client": ("9.9.9.9", 1), "root_path": "", "http_version": "1.1", "app": None}
    asyncio.run(door(scope, receive, send))  # type: ignore[arg-type]
    assert answers[0]["status"] == 404
    assert scope[SCOPE_KEY] is True


def test_a_socket_is_refused_at_the_door(upstream: HTTPServer) -> None:
    door = TestClient(ServicesDoor(build_app(FakeApp(upstream.server_port, services_public_url="https://s.example.test"), "tok")))  # type: ignore[arg-type]
    with pytest.raises(Exception) as refused:
        with door.websocket_connect("/ws/anything"):
            pass
    assert getattr(refused.value, "code", None) == 1008


def test_a_page_at_the_door_has_a_real_origin_and_none_of_the_apps_things(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test")
    answer = signed_in(app, door=True).get("/s/demo-ab12/", headers={"cookie": f"{SESSION_COOKIE}=secret; daedalus_second_factor=x; theirs=1"})
    assert SANDBOX_POLICY not in answer.headers.get_list("content-security-policy")
    assert Upstream.seen[-1]["cookie"] == "theirs=1"


def test_the_app_sends_a_request_to_the_door_with_method_and_key_intact(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test")
    client = signed_in(app)
    for method, path in (("GET", "/s/vault-cd34/a%20b?key=s3cret&x=1"), ("POST", "/s/demo-ab12/form")):
        answer = client.request(method, path, follow_redirects=False)
        assert answer.status_code == 307
        assert answer.headers["location"] == "https://s.example.test" + path
    assert Upstream.seen == [], "nothing runs on the app's address when there is a door"


def test_a_key_becomes_a_cookie_at_the_door_and_stays_host_only(upstream: HTTPServer) -> None:
    door = signed_in(FakeApp(upstream.server_port, services_public_url="https://s.example.test"), door=True)
    answer = door.get("/s/vault-cd34/?key=s3cret", follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"] == "/s/vault-cd34/"
    cookie = answer.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "path=/s/vault-cd34" in cookie and "domain" not in cookie


# -- /s/ with no door: the sandbox ------------------------------------------------------------------


def test_without_a_door_the_page_is_served_in_a_sandbox(upstream: HTTPServer) -> None:
    app = FakeApp(upstream.server_port)  # https app, no name for the door
    answer = signed_in(app).get("/s/demo-ab12/")
    policies = answer.headers.get_list("content-security-policy")
    assert answer.status_code == 200 and SANDBOX_POLICY in policies
    assert "allow-same-origin" not in SANDBOX_POLICY and "allow-top-navigation" not in SANDBOX_POLICY and "escape-sandbox" not in SANDBOX_POLICY
    # The server's own policy stays beside it, and being a second policy it can only narrow.
    assert len(policies) == 2


def test_sandboxed_answers_that_are_not_html_are_sandboxed_too(upstream: HTTPServer) -> None:
    # An SVG runs script when opened directly; what a page is, is not for the content type to decide.
    answer = signed_in(FakeApp(upstream.server_port)).get("/s/demo-ab12/picture.svg")
    assert SANDBOX_POLICY in answer.headers.get_list("content-security-policy")


def test_a_sandboxed_page_can_still_read_its_own_backend(upstream: HTTPServer) -> None:
    client = signed_in(FakeApp(upstream.server_port))
    client.cookies.clear()  # a sandboxed page's requests carry none, measured
    answer = client.get("/s/demo-ab12/data.json", headers={"origin": "null"})
    assert answer.headers["access-control-allow-origin"] == "null"
    assert "access-control-allow-credentials" not in answer.headers
    client.cookies.clear()  # the server's own Set-Cookie, which the sandbox would not send either
    preflight = client.options("/s/demo-ab12/data.json", headers={"origin": "null", "access-control-request-method": "POST", "access-control-request-headers": "content-type"})
    assert preflight.status_code == 204 and preflight.headers["access-control-allow-headers"] == "content-type"
    assert Upstream.seen[-1].get("origin") == "null" and len(Upstream.seen) == 1, "a preflight is ours to answer"


def test_a_foreign_origin_gets_no_cors_from_a_share(upstream: HTTPServer) -> None:
    client = signed_in(FakeApp(upstream.server_port))
    client.cookies.clear()
    assert "access-control-allow-origin" not in client.get("/s/demo-ab12/data.json", headers={"origin": "https://elsewhere.test"}).headers


def test_in_the_sandbox_a_key_travels_in_the_address(upstream: HTTPServer) -> None:
    # A sandboxed page's own requests carry no cookie, so a key in one would serve the first page and
    # refuse every script of it.
    client = signed_in(FakeApp(upstream.server_port))
    client.cookies.clear()
    first = client.get("/s/vault-cd34/?key=s3cret&x=1", follow_redirects=False)
    assert first.status_code == 303 and first.headers["location"] == "/s/vault-cd34~s3cret/?x=1"
    assert "set-cookie" not in first.headers
    assert client.get("/s/vault-cd34~s3cret/app.js").status_code == 200
    assert Upstream.seen[-1]["x-forwarded-prefix"] == "/s/vault-cd34~s3cret"
    client.cookies.clear()
    assert client.get("/s/vault-cd34~wrong/app.js").status_code == 403
    assert client.get("/s/vault-cd34/app.js").status_code == 403


@pytest.mark.parametrize("door", [False, True])
def test_a_response_speaks_only_for_itself(upstream: HTTPServer, door: bool) -> None:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test" if door else "")
    answer = signed_in(app, door=door).get("/s/demo-ab12/")
    assert not ORIGIN_WIDE_HEADERS & {name.lower() for name in answer.headers}
    cookie = answer.headers["set-cookie"].lower()
    assert "domain" not in cookie and "path=/s/demo-ab12/" in cookie


def test_a_cookie_is_kept_under_the_prefix() -> None:
    assert scoped_set_cookie("a=1; Path=/; Domain=example.test; HttpOnly", "/s/x") == "a=1; Path=/s/x/; HttpOnly"
    assert scoped_set_cookie("a=1; domain=.example.test", "/s/x") == "a=1"


# -- the address -------------------------------------------------------------------------------------


def settings_for(**values: Any) -> Settings:
    return Settings(_env_file=None, telegram_bot_token="", owner_user_id=1, **values)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        # A server with a domain and nothing done yet: the sandbox, never a guessed address.
        ({"miniapp_public_url": "https://a.example.test/app/", "services_public_host": "192.0.2.1"}, ""),
        ({"miniapp_public_url": "https://a.example.test", "services_public_url": "https://s.example.test/"}, "https://s.example.test"),
        ({"miniapp_public_url": "https://a.example.test", "services_public_url": "https://a.example.test:8443"}, "https://a.example.test:8443"),
        # A bare address and a laptop: the next port, with nothing to arrange.
        ({"miniapp_public_url": "http://192.0.2.1:8765", "services_public_host": "192.0.2.1"}, "http://192.0.2.1:8766"),
        ({"miniapp_public_url": "", "services_public_host": "localhost"}, "http://localhost:8766"),
        ({"miniapp_public_url": "", "services_public_host": ""}, ""),
        # The app's own origin is no separate origin, whatever the settings claim.
        ({"miniapp_public_url": "https://a.example.test/app", "services_public_url": "https://a.example.test/s"}, ""),
        ({"miniapp_public_url": "http://192.0.2.1:8765", "services_public_host": "192.0.2.1", "services_door_port": 0}, ""),
    ],
)
def test_where_the_door_is_in_each_kind_of_installation(values: dict[str, Any], expected: str) -> None:
    assert public_address.services_base(settings_for(**values)) == expected


def test_a_port_someone_holds_leaves_the_bot_running_and_the_shares_sandboxed() -> None:
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    settings = settings_for(services_public_host="localhost", services_door_port=held.getsockname()[1], api_host="127.0.0.1")
    assert public_address.services_base(settings)
    assert open_door(settings, server_config(ServicesDoor(lambda *_: None), lifespan="off")) is None  # type: ignore[arg-type]
    assert public_address.services_base(settings) == ""
    held.close()


async def test_the_door_serves_on_its_port_and_goes_when_it_is_cancelled() -> None:
    settings = settings_for(services_public_host="localhost", services_door_port=free_port(), api_host="127.0.0.1")

    async def api(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"reached"})

    opened = open_door(settings, server_config(ServicesDoor(api), lifespan="off"))  # type: ignore[arg-type]
    assert opened is not None
    server, task = opened
    while not server.started:
        await asyncio.sleep(0.01)
    address = f"http://127.0.0.1:{settings.services_door_port}"
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"{address}/s/demo/")).text == "reached"
        assert (await client.get(f"{address}/api/auth/me")).status_code == 404
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(httpx.ConnectError):
        await httpx.AsyncClient().get(address)


async def test_the_doctor_says_which_of_the_two_this_installation_has() -> None:
    sandboxed = await _shared_services(SimpleNamespace(settings=settings_for(miniapp_public_url="https://a.example.test")))  # type: ignore[arg-type]
    assert not sandboxed[0].ok and "sandbox" in sandboxed[0].message
    door = await _shared_services(SimpleNamespace(settings=settings_for(miniapp_public_url="https://a.example.test", services_public_url="https://s.example.test")))  # type: ignore[arg-type]
    assert door[0].ok and "https://s.example.test" in door[0].message


def test_a_new_address_reaches_links_without_a_restart() -> None:
    settings = settings_for(miniapp_public_url="")
    app: Any = SimpleNamespace(settings=settings, db=None, manager=None)
    services, dialogs = Services(app), DialogShares(app)
    row = {"share_mode": "public", "share_slug": "demo", "port": 8100}
    dialog = {"share_mode": "public", "share_slug": "d"}
    assert services.share_url(row) == "<public-url>/s/demo/"
    public_address.publish(settings, app="https://tunnel.example.test/app", services="https://door.example.test/")
    assert services.share_url(row) == "https://door.example.test/s/demo/"
    assert dialogs.share_url(dialog) == "https://tunnel.example.test/c/d"
    assert services.view({**row, "name": "n", "command": "c", "cwd": "/", "status": "stopped", "started_at": "t", "log_path": "l"})["share"]["sandboxed"] is False
    public_address.publish(settings, services="")
    assert services.share_url(row) == "https://tunnel.example.test/s/demo/"
    assert services.view({**row, "name": "n", "command": "c", "cwd": "/", "status": "stopped", "started_at": "t", "log_path": "l"})["share"]["sandboxed"] is True


# -- who the client is -------------------------------------------------------------------------------


async def client_address(port: int, trusted: str, sent: str) -> tuple[str, str]:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        body = f"{scope['client'][0]} {scope['scheme']}".encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    server = uvicorn.Server(server_config(app, trusted_proxies=trusted, host="127.0.0.1", port=port, lifespan="off"))
    server.config.setup_event_loop = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient() as client:
            answer = await client.get(f"http://127.0.0.1:{port}/", headers={"x-forwarded-for": sent, "x-forwarded-proto": "https"})
        client_host, scheme = answer.text.split()
        return client_host, scheme
    finally:
        server.should_exit = True
        await task


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_local_client_cannot_name_its_own_address() -> None:
    # A tunnel's client connects from loopback, which is what uvicorn trusts by default: every visitor
    # would pick a fresh address and never meet the per-address limit.
    assert asyncio.run(client_address(free_port(), "", "203.0.113.9")) == ("127.0.0.1", "http")


def test_a_named_proxy_is_believed_and_a_visitors_own_claim_is_not() -> None:
    # nginx appends the peer it saw, so the address a visitor forged stands to the left of the real one.
    assert asyncio.run(client_address(free_port(), "127.0.0.1", "203.0.113.9")) == ("203.0.113.9", "https")
    assert asyncio.run(client_address(free_port(), "127.0.0.1", "198.51.100.1, 203.0.113.9")) == ("203.0.113.9", "https")


def test_the_server_never_falls_back_on_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    config = server_config(lambda *_: None)  # type: ignore[arg-type]
    assert config.proxy_headers is False and config.forwarded_allow_ips == ""
