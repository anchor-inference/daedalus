"""A live link to one session: what its token reaches, and what it never does.

The door's matrix is the contract. A token opens its own session's routes, its terminals, browsers and notes,
a narrowed event stream and a model list without addresses or keys, at the services door only. Another
session, the settings, the secrets, the session list, a new session, deleting, sharing again: 404, as if
they did not exist. A revoked or expired link answers 410, and revoking ends a request already in flight.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import session_share
from daedalus.extensions.api import build_app
from daedalus.extensions.api_services_door import ServicesDoor
from daedalus.extensions.api_session_share import ROUTES, SCOPE_KEY, ShareDoor, route_of, shell, split
from daedalus.extensions.session_share import SessionShares, reach
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

OWNER = {"x-daedalus-token": "tok"}


class Rig:
    def __init__(self, manager: SessionManager, app: Any) -> None:
        self.manager = manager
        self.app = app

    @property
    def shares(self) -> SessionShares:
        return self.app.extensions[session_share.EXTENSION_KEY]  # type: ignore[no-any-return]

    async def session(self, title: str = "chat") -> str:
        state = await self.manager.create_session(title)
        return state.session.id

    def clients(self) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
        """The app's own port, and the services door in front of the same application."""
        api = build_app(self.app, "tok")
        return (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://app.test"),  # type: ignore[arg-type]
            httpx.AsyncClient(transport=httpx.ASGITransport(app=ServicesDoor(api)), base_url="http://door.test"),  # type: ignore[arg-type]
        )


@pytest.fixture
async def rig(settings: Settings, db: Database, config: RuntimeConfig) -> AsyncIterator[Rig]:
    configured = settings.model_copy(update={"native": False, "services_public_url": "https://door.example.org"})
    manager = SessionManager(configured, config, db=db)
    await manager.start()
    app = SimpleNamespace(settings=configured, config=manager.config, db=db, manager=manager, front=None, extensions={}, guard=None, create_session=manager.create_session)
    await session_share.install(app)  # type: ignore[arg-type]
    yield Rig(manager, app)
    await manager.close()


async def test_the_operator_makes_lists_and_revokes_a_link(rig: Rig) -> None:
    mine = await rig.session()
    app, _door = rig.clients()
    async with app:
        made = (await app.post(f"/api/sessions/{mine}/live-shares", headers=OWNER, json={"expires_hours": 1})).json()
        assert made["active"] and made["url"].startswith("https://door.example.org/v/") and made["url"].endswith("/")
        token = made["url"].split("/v/")[1].rstrip("/")
        assert len(token) >= 40
        listed = (await app.get(f"/api/sessions/{mine}/live-shares", headers=OWNER)).json()
        assert listed["reach"] == "outside" and [link["id"] for link in listed["links"]] == [made["id"]]
        assert (await app.get(f"/api/sessions/{mine}/live-shares")).status_code == 401
        after = (await app.delete(f"/api/sessions/{mine}/live-shares/{made['id']}", headers=OWNER)).json()
        assert after["links"][0]["active"] is False and after["links"][0]["revoked_at"] and after["links"][0]["url"] == ""
        assert (await app.post(f"/api/sessions/{mine}/live-shares", headers=OWNER, json={"expires_hours": 24 * 365})).status_code == 422


async def test_the_door_matrix(rig: Rig) -> None:
    mine = await rig.session("mine")
    other = await rig.session("other")
    link = await rig.shares.create(mine)
    token = link["url"].split("/v/")[1].rstrip("/")
    app, door = rig.clients()
    async with app, door:
        v = f"/v/{token}"
        # The session, its own routes, and the narrowed listings answer.
        page = await door.get(f"{v}/api/sessions/{mine}")
        assert page.status_code == 200 and page.json()["id"] == mine
        assert (await door.get(f"{v}/api/sessions/{mine}/files")).status_code == 200
        assert (await door.get(f"{v}/api/sessions/{mine}/notes")).status_code == 200
        assert (await door.get(f"{v}/api/share")).json()["session_id"] == mine
        assert (await door.get(f"{v}/api/auth/me")).json()["via"] == "share"
        picked = (await door.get(f"{v}/api/settings")).json()
        assert set(picked) == {"presets", "providers", "model"} and "base_url" not in repr(picked) and "api_key" not in repr(picked)
        assert (await door.get(f"{v}/api/commands")).json() == []
        # Another session does not exist, whatever is asked of it.
        for method, suffix in (("GET", ""), ("POST", "/messages"), ("POST", "/stop"), ("GET", "/files"), ("GET", "/stream"), ("GET", "/download")):
            answer = await door.request(method, f"{v}/api/sessions/{other}{suffix}", json={"text": "hi"})
            assert answer.status_code == 404, (method, suffix, answer.status_code)
        # Nothing past the session: the list, a new one, deleting, renaming, sharing, the secrets, settings' writes.
        for method, path in (
            ("GET", "/api/sessions"), ("POST", "/api/sessions"), ("DELETE", f"/api/sessions/{mine}"), ("PATCH", f"/api/sessions/{mine}"),
            ("POST", f"/api/sessions/{mine}/share"), ("GET", f"/api/sessions/{mine}/live-shares"), ("POST", f"/api/sessions/{mine}/live-shares"),
            ("POST", f"/api/sessions/{mine}/guest/links"), ("POST", f"/api/sessions/{mine}/fork"), ("POST", f"/api/sessions/{mine}/bypass"),
            ("POST", f"/api/sessions/{mine}/project"), ("GET", f"/api/sessions/{mine}/export/download"), ("GET", "/api/secrets"),
            ("PUT", "/api/settings"), ("GET", "/api/providers"), ("POST", "/api/terminals"), ("GET", "/api/terminals/layouts"),
            ("POST", f"/api/sessions/{mine}/command"), ("POST", f"/api/sessions/{mine}/btw"), ("POST", f"/api/sessions/{mine}/loop"),
            ("GET", "/api/notes/n_none"), ("GET", "/api/terminals/t_none"), ("GET", "/api/browsers/g_none"), ("POST", "/api/presence"),
            ("GET", "/api/../api/sessions"),
        ):
            answer = await door.request(method, f"{v}{path}", json={})
            assert answer.status_code == 404, (method, path, answer.status_code)
        # A wrong token, and the operator's own credentials in a visitor's request, open nothing.
        assert (await door.get(f"/v/{'x' * 43}/api/sessions/{mine}")).status_code == 404
        assert (await door.get(f"/v/{token}/api/sessions", headers=OWNER)).status_code == 404
        # The app's own port does not serve a link at all.
        assert (await app.get(f"{v}/api/sessions/{mine}")).status_code == 404
        assert (await app.get(f"{v}/")).status_code == 404
        # The page: the app's shell, its files under /v/, no referrer, no Telegram script.
        shell_page = await door.get(f"{v}/")
        assert shell_page.status_code == 200 and shell_page.headers["referrer-policy"] == "no-referrer" and shell_page.headers["cache-control"] == "no-store"
        assert "/v/assets/" in shell_page.text and "/app/assets/" not in shell_page.text and "telegram.org" not in shell_page.text
        asset = shell_page.text.split('src="/v/assets/')[1].split('"')[0]
        assert (await door.get(f"/v/assets/{asset}")).status_code == 200
        assert (await door.get("/v/assets/../index.html")).status_code == 404
        # Revoked: 410, everywhere the link reached.
        await rig.shares.revoke(mine, link["id"])
        gone = await door.get(f"{v}/api/sessions/{mine}")
        assert gone.status_code == 410 and gone.json()["code"] == "ended"


async def test_a_listing_is_narrowed_to_the_session_whatever_was_asked(rig: Rig) -> None:
    mine = await rig.session("mine")
    other = await rig.session("other")
    link = await rig.shares.create(mine)
    token = link["url"].split("/v/")[1].rstrip("/")
    seen: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def owner_of(kind: str, thing: str) -> str | None:
        return None

    door = ShareDoor(inner, shares=lambda: rig.shares, owner_of=owner_of, dist=rig.app.settings.bot_repo_dir / "miniapp" / "dist")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ServicesDoor(door)), base_url="http://door.test") as client:  # type: ignore[arg-type]
        await client.get(f"/v/{token}/api/terminals?owner_kind=session&owner_id={other}", headers={"cookie": "daedalus_session=x", "authorization": "tma x"})
        await client.get(f"/v/{token}/api/events?session={other}&after=5")
    terminals, events = seen
    assert terminals["path"] == "/api/terminals" and terminals["query_string"] == f"owner_kind=session&owner_id={mine}".encode()
    assert events["query_string"] == f"after=5&session={mine}".encode()
    assert not any(key in (b"cookie", b"authorization") for key, _ in terminals["headers"])
    assert terminals[SCOPE_KEY]["session_id"] == mine


async def test_revoking_ends_a_stream_already_open(rig: Rig) -> None:
    mine = await rig.session()
    link = await rig.shares.create(mine)
    token = link["url"].split("/v/")[1].rstrip("/")
    started = asyncio.Event()

    async def endless(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
        started.set()
        await asyncio.Event().wait()

    async def owner_of(kind: str, thing: str) -> str | None:
        return None

    door = ShareDoor(endless, shares=lambda: rig.shares, owner_of=owner_of, dist=rig.app.settings.bot_repo_dir / "miniapp" / "dist")
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    scope = {"type": "http", "method": "GET", "path": f"/v/{token}/api/sessions/{mine}/stream", "query_string": b"", "headers": [], "daedalus.api_services_door": True}
    task = asyncio.create_task(door(scope, receive, send))
    await asyncio.wait_for(started.wait(), 5)
    assert len(rig.shares._cuts[link["id"]]) == 1
    await rig.shares.revoke(mine, link["id"])
    await asyncio.wait_for(task, 2)
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert link["id"] not in rig.shares._cuts


async def test_an_expired_link_is_ended_and_a_request_not_answered_yet_hears_410(rig: Rig) -> None:
    mine = await rig.session()
    link = await rig.shares.create(mine, expires_hours=1)
    token = link["url"].split("/v/")[1].rstrip("/")
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
    await rig.app.db.execute("UPDATE session_shares SET expires_at = ? WHERE id = ?", (past, link["id"]))
    app, door = rig.clients()
    async with app, door:
        assert (await door.get(f"/v/{token}/api/share")).status_code == 410
        assert (await app.get(f"/api/sessions/{mine}/live-shares", headers=OWNER)).json()["links"][0]["active"] is False


async def test_visits_are_counted_by_the_browsers_id_with_the_name_it_gave(rig: Rig) -> None:
    mine = await rig.session()
    link = await rig.shares.create(mine)
    token = link["url"].split("/v/")[1].rstrip("/")
    _app, door = rig.clients()
    async with door:
        await door.get(f"/v/{token}/api/share", headers={"x-share-visitor": "abcdef0123456789", "x-share-name": "Ann%20Lee%0A"})
        await door.get(f"/v/{token}/api/share", headers={"x-share-visitor": "bad id!"})
    listed = await rig.shares.for_session(mine)
    entry = listed["links"][0]
    assert [v["name"] for v in entry["visitors"]] == ["Ann Lee"] and entry["last_seen_at"]


def test_reach_says_who_can_open_a_link() -> None:
    assert reach("") == "none"
    assert reach("http://localhost:8766") == "machine"
    assert reach("http://127.0.0.1:8766") == "machine"
    assert reach("http://172.16.0.5:8766") == "network"
    assert reach("http://desktop:8766") == "network"
    assert reach("https://abc.trycloudflare.com") == "outside"
    # A documentation range is no more reachable than a private one: only a global address counts.
    assert reach("http://203.0.113.9:8766") == "network"


def test_the_route_list_names_one_session_or_checks_what_it_names() -> None:
    for method, pattern, rule in ROUTES:
        names = set(pattern.groupindex)
        if rule == "session":
            assert names == {"sid"}, pattern.pattern
        elif rule in ("terminal", "browser", "note"):
            assert names & {"tid", "gid", "nid", "fid"}, pattern.pattern
        else:
            assert not names, pattern.pattern
        assert method in ("GET", "POST", "PUT", "PATCH", "DELETE", "WS")
    assert route_of("GET", "/api/sessions/a/../b") is None
    assert route_of("HEAD", "/api/sessions/abc") is not None
    assert split("/v/short/api") is None and split("/app/") is None
    token = "t" * 43
    assert split(f"/v/{token}") == (token, "/") and split(f"/v/{token}/api/x") == (token, "/api/x")


def test_the_page_is_the_apps_with_its_files_under_v() -> None:
    page = shell(
        '<html><head>\n<link rel="manifest" href="/app/manifest.webmanifest" />\n<script async src="https://telegram.org/js/telegram-web-app.js" onload="x()"></script>\n'
        '<script type="module" crossorigin src="/app/assets/index-1.js"></script><link rel="stylesheet" href="/app/assets/index-1.css"></head></html>'
    )
    assert 'src="/v/assets/index-1.js"' in page and 'href="/v/assets/index-1.css"' in page
    assert "manifest" not in page and "telegram" not in page and 'name="referrer" content="no-referrer"' in page
    json.dumps(page)
