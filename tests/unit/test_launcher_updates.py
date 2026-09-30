"""The app tells the operator about a newer desktop launcher, and echoes the launcher's boot id.

The launcher reports what it found in its own status (``desktop/release.go``); the app reads that
through the launcher bridge and posts one entry per release to its notification centre. The boot
id is how the launcher tells this process's answer from one by a stack a crashed launcher left on
the same port (``desktop/ready.go``).
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

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import EXTENSIONS, launcher_updates
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.notifications import RecordingNotifications


class _Launcher:
    """A launcher's loopback page answering /api/status with whatever the test sets."""

    def __init__(self) -> None:
        self.status: dict[str, Any] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — the stdlib's name
                body = json.dumps(outer.status).encode()
                self.send_response(200 if self.path == "/api/status" else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def launcher(tmp_path: Path) -> Any:
    fake = _Launcher()
    state = tmp_path / "data" / "state"
    state.mkdir(parents=True)
    (tmp_path / "data" / "launcher.json").write_text(json.dumps({"port": fake.port, "token": "t", "pid": os.getpid()}))
    yield SimpleNamespace(fake=fake, state=state)
    fake.close()


def _app(state: Path) -> Any:
    return SimpleNamespace(settings=SimpleNamespace(state_dir=state), notifications=RecordingNotifications())


async def test_a_newer_launcher_is_announced_once_with_the_command(launcher: Any) -> None:
    app = _app(launcher.state)
    command = "'/home/someone/Daedalus/daedalus-desktop' upgrade --data '/home/someone/Daedalus/data'"
    launcher.fake.status = {"busy": "", "upgrade": {"from": "desktop-v0.12.0", "to": "desktop-v0.13.0", "url": "https://example.invalid/notes", "command": command}}
    assert await launcher_updates.check_once(app) is not None
    [draft] = app.notifications.drafts
    assert draft.category == "system" and draft.kind == "launcher_upgrade"
    assert draft.title == "Daedalus 0.13.0 is available"
    assert command in draft.body and "0.12.0" in draft.body and "verified backup" in draft.body and "ext4" in draft.body
    # Once per release, across restarts too: the record is in the state directory.
    assert await launcher_updates.check_once(_app(launcher.state)) is None
    assert await launcher_updates.check_once(app) is None
    assert len(app.notifications.drafts) == 1
    launcher.fake.status["upgrade"] = {**launcher.fake.status["upgrade"], "to": "desktop-v0.14.0"}
    assert await launcher_updates.check_once(app) is not None
    assert app.notifications.drafts[-1].title == "Daedalus 0.14.0 is available"


async def test_the_announcement_is_in_the_operators_language(launcher: Any) -> None:
    app = _app(launcher.state)
    app.notifications.locale = "ru-RU"
    launcher.fake.status = {"busy": "", "upgrade": {"from": "desktop-v0.12.0", "to": "desktop-v0.13.0", "command": "daedalus-desktop upgrade"}}
    assert await launcher_updates.check_once(app) is not None
    [draft] = app.notifications.drafts
    assert draft.title == "Вышел Daedalus 0.13.0"
    assert "daedalus-desktop upgrade" in draft.body and "проверенной резервной копией" in draft.body


async def test_nothing_is_announced_without_an_offer_or_a_launcher(launcher: Any, tmp_path: Path) -> None:
    app = _app(launcher.state)
    launcher.fake.status = {"busy": ""}
    assert await launcher_updates.check_once(app) is None
    launcher.fake.status = {"upgrade": {"to": "not-a-release"}}
    assert await launcher_updates.check_once(app) is None
    elsewhere = tmp_path / "other" / "state"
    elsewhere.mkdir(parents=True)
    assert await launcher_updates.check_once(_app(elsewhere)) is None
    assert app.notifications.drafts == []


def test_the_extension_is_installed() -> None:
    assert "daedalus.extensions.launcher_updates" in EXTENSIONS


@pytest.fixture
async def api_app(settings: Settings, db: Database) -> Any:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={}, guard=None)
    app.notifications = RecordingNotifications()
    yield app
    await manager.close()


async def test_the_boot_id_is_on_the_health_check_answers_only(api_app: Any) -> None:
    # The launcher's DAEDALUS_BOOT_ID, as Settings reads it.
    api_app.settings.boot_id = "boot-123"
    api = build_app(api_app, "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        # The two answers the launcher's health check reads carry it...
        for path in ("/app", "/app/"):
            response = await client.get(path)
            assert response.headers.get("X-Daedalus-Boot") == "boot-123", (path, response.status_code)
        # ...and nothing else: not the API, not the public shared pages.
        for path in ("/api/status", "/c/a-slug", "/c/a-slug/transcript", "/c/a-slug/media/x", "/s/a-slug", "/app/c/a-slug", "/nowhere"):
            assert "X-Daedalus-Boot" not in (await client.get(path)).headers, path


async def test_without_a_launcher_there_is_no_boot_header(api_app: Any) -> None:
    api_app.settings.boot_id = ""
    api = build_app(api_app, "tok")  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
        assert "X-Daedalus-Boot" not in (await client.get("/api/status")).headers


def test_the_boot_id_is_read_from_its_own_name_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A bare BOOT_ID some other program exported is not the launcher's: without its own name the
    settings would echo it, and the launcher would take a stranger's answer for its stack's."""
    monkeypatch.delenv("DAEDALUS_BOOT_ID", raising=False)
    monkeypatch.setenv("BOOT_ID", "not-ours")
    assert Settings(_env_file=None, state_dir=tmp_path, owner_user_id=1).boot_id == ""  # type: ignore[call-arg]


def test_the_boot_id_comes_from_the_launchers_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DAEDALUS_BOOT_ID", " boot-456 ")
    assert Settings(_env_file=None, state_dir=tmp_path, owner_user_id=1).boot_id == "boot-456"  # type: ignore[call-arg]
