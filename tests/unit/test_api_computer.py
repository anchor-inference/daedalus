"""The Computer control's routes: native only, through the launcher, refusals said in words.

The launcher is a loopback HTTP server answering what each test sets; the routes are mounted on a
bare FastAPI with an authentication that lets everything through, and a session manager that knows
one session. What is pinned: outside the native application and without a launcher the state is
``available: false`` with a sentence; the launcher's 503 is the same; its 409 reaches the app as 409
with the refusal table's sentence; protected apps never reach the app; a grant names an existing
session and a bounded number of minutes and is passed on as the launcher's contract says; the macOS
settings links come only on macOS; and the launcher's token is in no answer.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from daedalus.computer import refusals
from daedalus.extensions import api_computer

TOKEN = "e" * 64

STATE = {
    "available": True,
    "platform": {"os": "darwin", "session_type": "quartz", "units": "points"},
    "permissions": {"screen": "denied", "accessibility": "granted", "input_monitoring": "unknown"},
    "secure": {"locked": False},
    "lease": {"active": False},
    "human": {"paused": False},
    "displays": [{"id": "1", "name": "Built-in", "bounds": {"x": 0, "y": 0, "w": 1440, "h": 900}, "primary": True, "pixels_per_unit": 2, "scale": 2}],
    "limits": {"max_lease_ms": 14_400_000},
    "version": "0.1.0",
}

APPS = {
    "apps": [
        {"id": "com.example.editor", "name": "Editor", "reachable": True, "in_scope": False,
         "windows": [{"id": "10", "title": "notes.txt", "focused": False, "in_scope": False, "order": 1}]},
        {"id": "com.example.daedalus", "name": "Daedalus", "protected": True, "reachable": True, "in_scope": False,
         "windows": [{"id": "11", "title": "Daedalus", "focused": True, "in_scope": False, "order": 0}]},
    ],
    "foreground": {"id": "11", "title": "Daedalus", "focused": True, "in_scope": False, "order": 0},
}


class FakeLauncher:
    def __init__(self) -> None:
        self.answers: dict[tuple[str, str], tuple[int, Any]] = {}
        self.seen: list[tuple[str, str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if self.headers.get("X-Daedalus-Desktop") != TOKEN:
                    status, body = 403, {"error": {"message": "no token"}}
                else:
                    outer.seen.append((method, self.path, json.loads(raw) if raw else None))
                    status, body = outer.answers.get((method, self.path), (200, {}))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
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
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Manager:
    async def get_state(self, session_id: str) -> object | None:
        return object() if session_id == "s1" else None


def mount(state_dir: Path, *, native: bool = True) -> FastAPI:
    api = FastAPI()
    app = SimpleNamespace(settings=SimpleNamespace(native=native, state_dir=state_dir), manager=Manager())

    async def auth() -> dict[str, Any]:
        return {"user_id": 1}

    api_computer.register(api, app, auth)  # type: ignore[arg-type]
    return api


@pytest.fixture
def launcher(tmp_path: Path) -> Any:
    fake = FakeLauncher()
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    (tmp_path / "data" / "launcher.json").write_text(json.dumps({"port": fake.port, "token": TOKEN, "pid": os.getpid()}))
    yield SimpleNamespace(fake=fake, state=state)
    fake.close()


async def ask(api: FastAPI, method: str, path: str, body: Any = None) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        return await client.request(method, path, json=body)


async def test_outside_the_native_app_there_is_nothing_to_drive(launcher: Any) -> None:
    api = mount(launcher.state, native=False)
    answer = (await ask(api, "GET", "/api/computer")).json()
    assert answer["native"] is False and answer["available"] is False and "desktop app" in answer["reason"]
    assert answer["reason_words"] == {"key": "cu.reason.not_native", "vars": {}}
    assert (await ask(api, "POST", "/api/computer/stop", {})).status_code == 503
    assert launcher.fake.seen == []


async def test_without_a_launcher_the_state_says_so(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    answer = (await ask(mount(state), "GET", "/api/computer")).json()
    assert answer == {"native": True, "available": False, "reason": api_computer.NO_LAUNCHER, "reason_words": {"key": "cu.reason.no_launcher", "vars": {}}}


async def test_the_launchers_503_is_unavailable_with_its_reason(launcher: Any) -> None:
    launcher.fake.answers[("GET", "/api/computer")] = (503, {"available": False, "reason": "This build carries no desktop daemon."})
    answer = (await ask(mount(launcher.state), "GET", "/api/computer")).json()
    assert answer == {"native": True, "available": False, "reason": "This build carries no desktop daemon.", "reason_words": {"key": "cu.reason.no_daemon", "vars": {}}}


async def test_the_state_on_macos_carries_the_settings_links_and_no_token(launcher: Any) -> None:
    launcher.fake.answers[("GET", "/api/computer")] = (200, STATE)
    response = await ask(mount(launcher.state), "GET", "/api/computer")
    answer = response.json()
    assert answer["available"] is True and answer["os"] == "darwin" and answer["session_type"] == "quartz"
    assert answer["permissions"]["screen"] == "denied"
    assert answer["lease"] == {"active": False} and answer["human"] == {"paused": False}
    assert answer["settings_links"] == {
        "screen": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    }
    assert TOKEN not in response.text


async def test_elsewhere_there_are_no_settings_links(launcher: Any) -> None:
    launcher.fake.answers[("GET", "/api/computer")] = (200, {**STATE, "platform": {"os": "linux", "session_type": "x11", "units": "pixels"}})
    answer = (await ask(mount(launcher.state), "GET", "/api/computer")).json()
    assert answer["os"] == "linux" and answer["session_type"] == "x11" and "settings_links" not in answer


async def test_protected_apps_and_their_foreground_window_never_reach_the_app(launcher: Any) -> None:
    launcher.fake.answers[("GET", "/api/computer/apps")] = (200, APPS)
    answer = (await ask(mount(launcher.state), "GET", "/api/computer/apps")).json()
    assert [a["id"] for a in answer["apps"]] == ["com.example.editor"]
    assert "foreground" not in answer
    launcher.fake.answers[("GET", "/api/computer/apps")] = (200, {**APPS, "foreground": APPS["apps"][0]["windows"][0]})
    assert (await ask(mount(launcher.state), "GET", "/api/computer/apps")).json()["foreground"]["id"] == "10"


async def test_a_grant_is_passed_on_in_the_launchers_terms(launcher: Any) -> None:
    launcher.fake.answers[("POST", "/api/computer/grant")] = (200, {"lease": {"id": "l1", "session": "s1"}})
    response = await ask(mount(launcher.state), "POST", "/api/computer/grant", {"session_id": "s1", "scope": {"kind": "window", "window": "10"}, "minutes": 30})
    assert response.status_code == 200 and response.json() == {"lease": {"id": "l1", "session": "s1"}}
    assert launcher.fake.seen[-1] == ("POST", "/api/computer/grant", {"session": "s1", "scope": {"kind": "window", "window": "10"}, "ttl_ms": 1_800_000})


@pytest.mark.parametrize("body", [
    {"session_id": "s1", "scope": {"kind": "desktop"}, "minutes": 0},
    {"session_id": "s1", "scope": {"kind": "desktop"}, "minutes": 241},
    {"session_id": "s1", "scope": {"kind": "window"}, "minutes": 10},
    {"session_id": "s1", "scope": {"kind": "everything"}, "minutes": 10},
    {"session_id": "s1", "scope": {"kind": "desktop"}, "minutes": 10, "ttl_ms": 99},
])
async def test_a_grant_out_of_bounds_is_a_form_error(launcher: Any, body: dict[str, Any]) -> None:
    assert (await ask(mount(launcher.state), "POST", "/api/computer/grant", body)).status_code == 422
    assert launcher.fake.seen == []


async def test_a_grant_names_a_session_that_exists(launcher: Any) -> None:
    response = await ask(mount(launcher.state), "POST", "/api/computer/grant", {"session_id": "nope", "scope": {"kind": "desktop"}, "minutes": 10})
    assert response.status_code == 404
    assert launcher.fake.seen == []


async def test_a_refusal_reaches_the_app_with_its_explanation(launcher: Any) -> None:
    data = {"app": "com.example.daedalus"}
    launcher.fake.answers[("POST", "/api/computer/grant")] = (409, {"error": {"code": 1203, "message": "that app is one the daemon never touches", "data": data}})
    response = await ask(mount(launcher.state), "POST", "/api/computer/grant", {"session_id": "s1", "scope": {"kind": "app", "app": "com.example.daedalus"}, "minutes": 10})
    assert response.status_code == 409
    explanation = refusals.explain(1203, "that app is one the daemon never touches", data)
    words = {"key": "cu.refusal.protected", "vars": {}}
    assert response.json() == {"detail": explanation, "error": {"code": 1203, "message": "that app is one the daemon never touches", "data": data, "explanation": explanation, "words": words}}


async def test_revoke_stop_resume_and_permissions_go_through(launcher: Any) -> None:
    api = mount(launcher.state)
    launcher.fake.answers[("POST", "/api/computer/revoke")] = (200, {"revoked": True})
    launcher.fake.answers[("POST", "/api/computer/stop")] = (200, {"stopped": True})
    launcher.fake.answers[("POST", "/api/computer/resume")] = (200, {"resumed": True})
    launcher.fake.answers[("POST", "/api/computer/permissions")] = (200, {"which": "screen", "state": "denied", "permissions": {}, "hint": "Open System Settings"})
    assert (await ask(api, "POST", "/api/computer/revoke", {"session_id": "s1"})).json() == {"revoked": True}
    assert (await ask(api, "POST", "/api/computer/revoke", {})).json() == {"revoked": True}
    assert (await ask(api, "POST", "/api/computer/stop", {})).json() == {"stopped": True}
    assert (await ask(api, "POST", "/api/computer/resume", {})).json() == {"resumed": True}
    assert (await ask(api, "POST", "/api/computer/permissions", {"which": "screen"})).json()["state"] == "denied"
    assert (await ask(api, "POST", "/api/computer/permissions", {"which": "camera"})).status_code == 422
    assert [(m, p, b) for m, p, b in launcher.fake.seen] == [
        ("POST", "/api/computer/revoke", {"session": "s1"}),
        ("POST", "/api/computer/revoke", {}),
        ("POST", "/api/computer/stop", {}),
        ("POST", "/api/computer/resume", {}),
        ("POST", "/api/computer/permissions", {"which": "screen"}),
    ]


async def test_a_launcher_that_lost_its_desktop_midway_is_503_not_a_crash(launcher: Any) -> None:
    launcher.fake.answers[("POST", "/api/computer/stop")] = (503, {"available": False, "reason": "The desktop daemon stopped."})
    response = await ask(mount(launcher.state), "POST", "/api/computer/stop", {})
    assert response.status_code == 503 and response.json()["detail"] == "The desktop daemon stopped."


@pytest.mark.parametrize(("code", "data", "key"), [
    (1200, {"reason": "expired"}, "cu.refusal.no_lease.expired"),
    (1200, {"reason": "something newer"}, "cu.refusal.no_lease.none"),
    (1204, {"reason": "lock_screen"}, "cu.refusal.secure.lock_screen"),
    (1204, None, "cu.refusal.secure.other"),
    (1205, {"which": "accessibility"}, "cu.refusal.permission.accessibility"),
    (1206, {"limit": "lease_actions"}, "cu.refusal.rate.actions"),
    (1206, {"retry_after_ms": 400}, "cu.refusal.rate"),
    (1101, None, "cu.refusal.human"),
])
def test_the_operators_words_pick_the_key_by_code_and_reason(code: int, data: Any, key: str) -> None:
    assert refusals.words(code, data) == {"key": key, "vars": {}}
    assert key in refusals.every_key()


def test_an_unknown_code_keeps_its_number_for_the_operator() -> None:
    assert refusals.words(4242, {"reason": "new"}) == {"key": "cu.refusal.unknown", "vars": {"code": 4242}}


def test_every_key_the_host_can_send_is_in_both_languages_of_the_app() -> None:
    # The app shows a key it does not know as the English explanation, so a missing entry would not
    # break the page; it would quietly put English back on the Russian one, which is the defect the
    # keys exist to end.
    table = (Path(__file__).resolve().parents[2] / "miniapp" / "src" / "i18n.ts").read_text(encoding="utf-8")
    keys = refusals.every_key() | {w["key"] for w in (api_computer.NOT_NATIVE_WORDS, api_computer.NO_LAUNCHER_WORDS, api_computer.NO_DAEMON_WORDS)}
    for key in sorted(keys):
        line = next((row for row in table.splitlines() if row.strip().startswith(f'"{key}":')), "")
        assert "en:" in line and "ru:" in line, key
