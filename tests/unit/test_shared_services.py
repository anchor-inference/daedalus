"""Shared services: the site proxies /s/<slug>/… to the service's port — open to anyone, or to whoever holds the key."""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from fastapi.testclient import TestClient

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.api_services_door import ServicesDoor
from daedalus.extensions.services import SHARE_COOKIE_PREFIX


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/go":
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.end_headers()
            return
        body = f"hello from {self.path} prefix={self.headers.get('x-forwarded-prefix')}".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path.endswith(".html") else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = b"echo:" + self.rfile.read(length)
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


class FakeServices:
    """The share rows the API looks up, with the process being this very test (so pid_alive is true)."""

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows

    async def by_slug(self, slug: str) -> dict[str, Any] | None:
        return self.rows.get(slug)

    def share_allows(self, row: dict[str, Any], key: str | None) -> bool:
        return row["share_mode"] == "public" or (row["share_mode"] == "key" and key == row["share_key"])


class FakeManager:
    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []


class FakeApp:
    def __init__(self, port: int) -> None:
        self.settings = Settings(_env_file=None, telegram_bot_token="123:abc", owner_user_id=1)  # type: ignore[call-arg]
        self.config = RuntimeConfig()
        self.manager = FakeManager()
        self.front: Any = None
        base = {"status": "running", "pid": os.getpid(), "port": port, "share_key": None}
        self.extensions: dict[str, Any] = {
            "services": FakeServices(
                {
                    "demo-ab12": {**base, "share_mode": "public"},
                    "vault-cd34": {**base, "share_mode": "key", "share_key": "s3cret"},
                    "hidden-ef56": {**base, "share_mode": "local"},
                    "down-gh78": {**base, "share_mode": "public", "status": "stopped"},
                }
            )
        }


@pytest.fixture
def client() -> Any:
    server = HTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    api = build_app(FakeApp(server.server_port), "tok")  # type: ignore[arg-type]
    with TestClient(api) as c:
        yield c
    server.shutdown()


def test_public_share_proxies_without_any_login(client: TestClient) -> None:
    r = client.get("/s/demo-ab12/some/page?x=1")
    assert r.status_code == 200 and r.text == "hello from /some/page?x=1 prefix=/s/demo-ab12"
    r = client.post("/s/demo-ab12/api", content=b"payload")
    assert r.status_code == 201 and r.text == "echo:payload"
    r = client.get("/s/demo-ab12", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/s/demo-ab12/"


def test_shared_pages_are_kept_out_of_search_engines(client: TestClient) -> None:
    r = client.get("/s/demo-ab12/page")
    assert r.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    r = client.get("/s/demo-ab12/robots.txt")
    assert r.status_code == 200 and "Disallow: /" in r.text
    # a page is never cached: the share can be switched off and the next reload says so
    assert client.get("/s/demo-ab12/index.html").headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/s/demo-ab12/data.json").headers


def test_root_relative_redirects_stay_under_the_slug(client: TestClient) -> None:
    r = client.get("/s/demo-ab12/go", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/s/demo-ab12/landed"


def test_key_share_needs_the_key_once_then_a_cookie(client: TestClient) -> None:
    client = TestClient(ServicesDoor(client.app))  # type: ignore[arg-type]
    assert client.get("/s/vault-cd34/").status_code == 403
    assert client.get("/s/vault-cd34/?key=wrong").status_code == 403
    r = client.get("/s/vault-cd34/page?key=s3cret&a=b", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/s/vault-cd34/page?a=b"
    cookie = r.cookies.get(SHARE_COOKIE_PREFIX + "vault-cd34")
    assert cookie == "s3cret"
    r = client.get("/s/vault-cd34/page", cookies={SHARE_COOKIE_PREFIX + "vault-cd34": "s3cret"})
    assert r.status_code == 200 and r.text.startswith("hello from /page")
    # the key can also ride in a header, for scripts
    assert client.get("/s/vault-cd34/page", headers={"X-Share-Key": "s3cret"}).status_code == 200


def test_local_unknown_and_stopped_services_are_not_reachable(client: TestClient) -> None:
    assert client.get("/s/hidden-ef56/").status_code == 404
    assert client.get("/s/nope/").status_code == 404
    assert client.get("/s/down-gh78/").status_code == 503
