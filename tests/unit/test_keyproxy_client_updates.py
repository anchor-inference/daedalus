"""The host's side of the imitated clients: the notification, its Update button, and the Settings routes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import EXTENSIONS
from daedalus.extensions import keyproxy_clients as kc
from daedalus.extensions.api import build_app
from daedalus.extensions.notifications import ActionRefused, ActionRequest
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.notifications import RecordingNotifications

NATIVE_BASE = "http://127.0.0.1:3201"
REAL_CLIENT = httpx.AsyncClient


class Notifications(RecordingNotifications):
    def __init__(self) -> None:
        super().__init__()
        self.resolved: list[tuple[str, str, str]] = []
        self.resolvers: dict[str, Any] = {}

    async def resolve(self, request_ref: str, resolution: str, *, via: str) -> int:
        self.resolved.append((request_ref, resolution, via))
        return 1

    def register_resolver(self, prefix: str, resolver: Any) -> None:
        self.resolvers[prefix] = resolver


class FakeProxy:
    """The key proxy's /clients routes, holding one Claude Code profile."""

    def __init__(self) -> None:
        self.version, self.latest, self.logged_in = "2.1.296", "2.1.310", True
        self.previous: str | None = None
        self.reverted: dict[str, Any] | None = None
        self.seen: list[httpx.Request] = []

    def row(self) -> dict[str, Any]:
        return {
            "name": "claude", "label": "Claude Code", "upstream": "claude", "logged_in": self.logged_in, "version": self.version, "source": "default",
            "latest": self.latest, "stale": self.latest > self.version, "kept_on_update": ["stainless_package_version", "betas"],
            "field_labels": {"version": "version", "stainless_package_version": "X-Stainless-Package-Version", "betas": "anthropic-beta"},
            "previous_version": self.previous, "reverted": self.reverted, "pending": False,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.headers.get("x-daedalus-token") != "tok":
            return httpx.Response(403, json={"error": "this endpoint answers the agent only"})
        path = request.url.path
        if path in ("/clients", "/clients/check"):
            return httpx.Response(200, json={"writable": True, "clients": [self.row()]})
        if path == "/clients/claude/update":
            body = json.loads(request.content or b"{}")
            self.previous, self.version = self.version, body.get("version") or self.latest
            return httpx.Response(200, json={"client": self.row(), "changed": {"version": {"from": self.previous, "to": self.version}}, "kept": {"betas": "x"}})
        if path == "/clients/claude/revert":
            if self.previous is None:
                return httpx.Response(409, json={"error": "Claude Code has no earlier profile to go back to"})
            self.version, self.previous = self.previous, None
            return httpx.Response(200, json={"client": self.row(), "reverted": {"automatic": False}})
        return httpx.Response(404, json={"error": f"unknown upstream {path.split('/')[1]!r}"})


@pytest.fixture
def proxy(monkeypatch: pytest.MonkeyPatch) -> FakeProxy:
    fake = FakeProxy()
    monkeypatch.setenv("KEYPROXY_BASE_URL", NATIVE_BASE)
    monkeypatch.setattr(kc.httpx, "AsyncClient", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(fake)))
    return fake


def _app(tmp_path: Path, *, through_proxy: bool = True) -> Any:
    base = f"{NATIVE_BASE}/claude/v1" if through_proxy else "https://api.example/v1"
    return SimpleNamespace(
        config=SimpleNamespace(providers={"claude": SimpleNamespace(base_url=base, api_key="")}),
        settings=SimpleNamespace(state_dir=str(tmp_path)),
        notifications=Notifications(),
        extensions={"api_token": "tok"},
    )


async def test_a_new_release_is_offered_once_with_an_update_button(proxy: FakeProxy, tmp_path: Path) -> None:
    app = _app(tmp_path)
    assert await kc.check_once(app) == ["keyproxy:claude:2.1.310"]
    draft = app.notifications.drafts[0]
    assert draft.title == "Claude Code in the key proxy is 2.1.296; 2.1.310 is out" and draft.link == kc.LINK
    assert [a.id for a in draft.actions] == ["update", "open"] and draft.request_ref == "keyproxy:claude:2.1.310"
    assert "X-Stainless-Package-Version, anthropic-beta" in draft.body, "the fields an update keeps are named"
    # The same release is not offered again, after a restart either: the operator who dismissed it stays undisturbed.
    assert await kc.check_once(app) == []
    assert await kc.check_once(_app(tmp_path)) == []

    # The button updates to the release it offered, and says so in the entry.
    outcome = await kc.resolver(app)(ActionRequest("keyproxy:claude:2.1.310", "keyproxy", "claude", "2.1.310", "update", None, "notification", {}))  # type: ignore[arg-type]
    assert outcome.resolution == "Updated to 2.1.310" and proxy.version == "2.1.310"
    with pytest.raises(ActionRefused):
        await kc.resolver(app)(ActionRequest("keyproxy:claude:2.1.310", "keyproxy", "claude", "2.1.310", "dismiss", None, "notification", {}))  # type: ignore[arg-type]

    # A newer release replaces the open offer rather than piling up beside it.
    proxy.latest = "2.1.311"
    assert await kc.check_once(app) == ["keyproxy:claude:2.1.311"]
    assert app.notifications.resolved == [], "the answered offer is not answered a second time"
    # One that was never answered is closed when a newer release replaces it.
    proxy.latest = "2.1.312"
    assert await kc.check_once(app) == ["keyproxy:claude:2.1.312"]
    assert app.notifications.resolved == [("keyproxy:claude:2.1.311", "Replaced by 2.1.312", "keyproxy")]


async def test_a_client_without_a_login_is_not_announced_and_an_automatic_revert_is(proxy: FakeProxy, tmp_path: Path) -> None:
    app = _app(tmp_path)
    proxy.logged_in = False
    assert await kc.check_once(app) == [] and app.notifications.drafts == []
    proxy.logged_in, proxy.latest = True, "2.1.296"
    proxy.reverted = {"from": "2.1.310", "at": 1000.0, "reason": "HTTP 400: claude_code_version_too_old", "automatic": True}
    await kc.check_once(app)
    assert [d.tone for d in app.notifications.drafts] == ["warning"] and "went back from 2.1.310" in app.notifications.drafts[0].title
    assert "claude_code_version_too_old" in app.notifications.drafts[0].body
    await kc.check_once(app)
    assert len(app.notifications.drafts) == 1, "one revert is reported once"


async def test_nothing_is_asked_without_a_proxy_and_an_old_proxy_is_named(proxy: FakeProxy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (await kc.describe(_app(tmp_path, through_proxy=False)))["state"] == "none" and proxy.seen == []
    old = FakeProxy()
    monkeypatch.setattr(kc.httpx, "AsyncClient", lambda **_: REAL_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "unknown upstream 'clients'"}))))
    described = await kc.describe(_app(tmp_path))
    assert described["state"] == "outdated" and "recreate" in described["error"] and old.seen == []


async def test_the_settings_routes(proxy: FakeProxy, tmp_path: Path, settings: Settings, db: Database) -> None:
    raw = RuntimeConfig().model_dump(mode="json")
    for entry in raw["providers"].values():
        if entry["base_url"].startswith("http://keyproxy:3200"):
            entry["base_url"] = NATIVE_BASE + entry["base_url"][len("http://keyproxy:3200") :]
    config = RuntimeConfig.model_validate(raw)
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={"api_token": "tok"}, guard=None, notifications=Notifications())
    async with REAL_CLIENT(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as http:  # type: ignore[arg-type]
        headers = {"x-daedalus-token": "tok"}
        assert (await http.get("/api/keyproxy/clients")).status_code == 401
        listed = (await http.get("/api/keyproxy/clients", headers=headers)).json()
        assert listed["reachable"] is True and listed["clients"][0]["stale"] is True
        checked = await http.post("/api/keyproxy/clients/check", headers=headers)
        assert checked.status_code == 200 and app.notifications.drafts, "a check from Settings announces what it found too"
        updated = await http.post("/api/keyproxy/clients/claude/update", headers=headers, json={})
        assert updated.status_code == 200 and updated.json()["client"]["version"] == "2.1.310"
        # The offer is closed when the update came from Settings rather than from its own button.
        assert app.notifications.resolved[-1][:1] == ("keyproxy:claude:2.1.310",) and app.notifications.resolved[-1][2] == "settings"
        assert (await http.post("/api/keyproxy/clients/claude/update", headers=headers, json={"version": "../x"})).status_code == 422
        assert (await http.post("/api/keyproxy/clients/Bad/update", headers=headers)).status_code == 400
        assert (await http.post("/api/keyproxy/clients/claude/revert", headers=headers)).json()["client"]["version"] == "2.1.296"
        again = await http.post("/api/keyproxy/clients/claude/revert", headers=headers)
        assert again.status_code == 409 and "no earlier profile" in again.json()["detail"]
    await manager.close()


def test_the_extension_is_installed() -> None:
    assert "daedalus.extensions.keyproxy_clients" in EXTENSIONS
