"""The launcher bridge's client for the desktop routes: the token on every call, reads included, and the
launcher's three kinds of answer told apart — the desktop's state, no desktop (503), a refusal (409).

The launcher is a loopback HTTP server here; nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from daedalus.host import launcher_bridge

TOKEN = "f" * 64


class FakeLauncher:
    """Answers each ``(method, path)`` with the ``(status, body)`` a test set, and records what came."""

    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], tuple[int, Any]] = {}
        self.seen: list[tuple[str, str, str | None, Any]] = []
        self.content_type = "application/json"
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.seen.append((method, self.path, self.headers.get(launcher_bridge.HEADER), json.loads(raw) if raw else None))
                status, body = outer.answers.get((method, self.path), (404, {"error": {"message": "no such route"}}))
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", outer.content_type if isinstance(body, bytes) else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
                self.answer("GET")

            def do_POST(self) -> None:  # noqa: N802
                self.answer("POST")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.launcher = launcher_bridge.Launcher(port=self.server.server_address[1], token=TOKEN, pid=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake() -> Any:
    found = FakeLauncher()
    yield found
    found.close()


async def test_a_read_carries_the_token_and_returns_the_answer(fake: FakeLauncher) -> None:
    fake.answers[("GET", "/api/computer/apps")] = (200, {"apps": [{"id": "a", "windows": []}]})
    assert await launcher_bridge.computer(fake.launcher, "GET", "/api/computer/apps") == {"apps": [{"id": "a", "windows": []}]}
    # Window titles are personal, so even a read bears the token.
    assert fake.seen == [("GET", "/api/computer/apps", TOKEN, None)]


async def test_a_write_sends_its_body_as_json(fake: FakeLauncher) -> None:
    fake.answers[("POST", "/api/computer/grant")] = (200, {"lease": {"id": "l1"}})
    body = {"session": "s1", "scope": {"kind": "desktop"}, "ttl_ms": 600_000}
    assert await launcher_bridge.computer(fake.launcher, "POST", "/api/computer/grant", body) == {"lease": {"id": "l1"}}
    assert fake.seen[-1] == ("POST", "/api/computer/grant", TOKEN, body)


async def test_no_desktop_is_told_apart_with_the_launchers_reason(fake: FakeLauncher) -> None:
    fake.answers[("GET", "/api/computer")] = (503, {"available": False, "reason": "The desktop daemon is not running."})
    with pytest.raises(launcher_bridge.DesktopUnavailable, match="daemon is not running"):
        await launcher_bridge.computer(fake.launcher, "GET", "/api/computer")


async def test_a_refusal_keeps_the_daemons_code_and_data(fake: FakeLauncher) -> None:
    fake.answers[("POST", "/api/computer/grant")] = (409, {"error": {"code": 1203, "message": "protected", "data": {"app": "com.example.daedalus"}}})
    with pytest.raises(launcher_bridge.DesktopRefused) as caught:
        await launcher_bridge.computer(fake.launcher, "POST", "/api/computer/grant", {})
    assert (caught.value.code, caught.value.message, caught.value.data) == (1203, "protected", {"app": "com.example.daedalus"})


@pytest.mark.parametrize(("status", "words"), [(403, "refused"), (404, "older than this build"), (501, "native installation"), (500, "answered 500")])
async def test_everything_else_is_an_unavailable_launcher_that_never_names_the_token(fake: FakeLauncher, status: int, words: str) -> None:
    fake.answers[("GET", "/api/computer")] = (status, {"error": {"message": "x"}})
    with pytest.raises(launcher_bridge.LauncherUnavailable, match=words) as caught:
        await launcher_bridge.computer(fake.launcher, "GET", "/api/computer")
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(fake.launcher)


async def test_a_launcher_that_is_gone_is_unavailable(fake: FakeLauncher) -> None:
    fake.close()
    with pytest.raises(launcher_bridge.LauncherUnavailable, match="did not answer") as caught:
        await launcher_bridge.computer(fake.launcher, "GET", "/api/computer")
    assert TOKEN not in str(caught.value)


async def test_only_the_desktop_routes_and_two_methods_are_reachable(fake: FakeLauncher) -> None:
    with pytest.raises(ValueError):
        await launcher_bridge.computer(fake.launcher, "GET", "/api/action/upgrade")
    with pytest.raises(ValueError):
        await launcher_bridge.computer(fake.launcher, "GET", "/api/computerish")
    with pytest.raises(ValueError):
        await launcher_bridge.computer(fake.launcher, "DELETE", "/api/computer")
    assert fake.seen == []


async def test_a_preview_is_the_picture_with_the_token_and_the_query(fake: FakeLauncher) -> None:
    fake.content_type = "image/jpeg"
    fake.answers[("GET", "/api/computer/preview?display=2&max=480")] = (200, b"\xff\xd8 picture")
    assert await launcher_bridge.computer_preview(fake.launcher, {"display": "2", "max": "480"}) == b"\xff\xd8 picture"
    assert fake.seen == [("GET", "/api/computer/preview?display=2&max=480", TOKEN, None)]


async def test_a_preview_that_is_not_a_jpeg_or_too_large_is_not_passed_on(fake: FakeLauncher) -> None:
    fake.content_type = "text/html"
    fake.answers[("GET", "/api/computer/preview?window=7")] = (200, b"<html>")
    with pytest.raises(launcher_bridge.LauncherUnavailable, match="not a JPEG"):
        await launcher_bridge.computer_preview(fake.launcher, {"window": "7"})
    fake.content_type = "image/jpeg"
    fake.answers[("GET", "/api/computer/preview?window=7")] = (200, b"x" * (launcher_bridge.PREVIEW_MAX_BYTES + 1))
    with pytest.raises(launcher_bridge.LauncherUnavailable, match="larger than a preview"):
        await launcher_bridge.computer_preview(fake.launcher, {"window": "7"})


async def test_a_preview_refusal_and_no_desktop_are_the_other_routes_errors(fake: FakeLauncher) -> None:
    fake.answers[("GET", "/api/computer/preview?window=7")] = (409, {"error": {"code": 1203, "message": "protected", "data": {}}})
    with pytest.raises(launcher_bridge.DesktopRefused) as caught:
        await launcher_bridge.computer_preview(fake.launcher, {"window": "7"})
    assert caught.value.code == 1203
    fake.answers[("GET", "/api/computer/preview?display=1")] = (503, {"available": False, "reason": "The desktop daemon is not running."})
    with pytest.raises(launcher_bridge.DesktopUnavailable, match="not running"):
        await launcher_bridge.computer_preview(fake.launcher, {"display": "1"})
    with pytest.raises(launcher_bridge.LauncherUnavailable, match="no previews"):
        await launcher_bridge.computer_preview(fake.launcher, {"display": "9"})
