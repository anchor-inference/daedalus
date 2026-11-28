"""A live link to one session: what its token reaches, and what it never does.

The door's matrix is the contract. A token opens its own session's routes, its terminals, browsers and notes,
a narrowed event stream and a model list without addresses or keys, at the services door only. Another
session, the settings, the secrets, the session list, a new session, deleting, sharing again: 404, as if
they did not exist. A revoked or expired link answers 410, and revoking ends a request already in flight. A password locks every
route but the page until the visitor holds the link's own pass; the link's policy narrows the model picks,
the terminals and, once its budget is spent, whatever would start a turn.
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
            ("GET", f"/api/sessions/{mine}/live-shares/options"), ("PATCH", f"/api/sessions/{mine}/live-shares/{link['id']}"),
            ("POST", f"/api/sessions/{mine}/vision"), ("PUT", f"/api/sessions/{mine}/subagent-models"),
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


ROLES = [
    {"id": "main", "label": "Conversation", "pickable": True, "current": {"preset": "a", "effort": "", "account": ""}, "presets": [
        {"id": "a", "label": "A", "efforts": ["low", "high"], "accounts": [{"id": "one", "name": "One"}, {"id": "two", "name": "Two"}], "billing": "subscription"},
        {"id": "b", "label": "B", "efforts": ["low"], "accounts": [], "billing": "metered"},
        {"id": "c", "label": "C", "efforts": [], "accounts": [], "billing": "metered"},
    ]},
    {"id": "vision", "label": "Pictures", "pickable": False, "current": {"preset": "b", "effort": "", "account": ""}, "presets": [
        {"id": "b", "label": "B", "efforts": ["low"], "accounts": [], "billing": "metered"},
    ]},
]
POLICY = {"roles": {"main": {"preset": "a", "effort": "low", "account": "one", "allowed": [{"preset": "b", "accounts": [], "efforts": []}]}},
          "budget_usd": 2.5, "terminals": "container"}


@pytest.fixture
def roles(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from daedalus.host import share_roles

    async def describe(app: Any, session_id: str) -> list[dict[str, Any]]:
        return ROLES

    monkeypatch.setattr(share_roles, "describe", describe)
    return ROLES


def token_of(link: dict[str, Any]) -> str:
    return str(link["url"].split("/v/")[1].rstrip("/"))


def test_a_policy_is_checked_field_by_field() -> None:
    from daedalus.extensions.share_policy import PolicyError, allows, presets_allowed, validate

    kept = validate(POLICY, ROLES, "container")
    assert kept["roles"]["main"]["allowed"] == [{"preset": "b", "accounts": [], "efforts": []}] and kept["terminals"] == "container"
    assert validate({}, ROLES, "host") == {} and validate({"terminals": "all"}, ROLES, "host") == {}
    for bad, field in (
        ({"roles": {"nobody": {"preset": "a"}}}, "policy.roles.nobody"),
        ({"roles": {"main": {"preset": "z"}}}, "policy.roles.main.preset"),
        ({"roles": {"main": {"preset": "a", "effort": "max"}}}, "policy.roles.main.effort"),
        ({"roles": {"main": {"preset": "a", "account": "three"}}}, "policy.roles.main.account"),
        ({"roles": {"main": {"preset": "a", "allowed": [{"preset": "b", "efforts": ["high"]}]}}}, "policy.roles.main.allowed[0].efforts"),
        ({"roles": {"main": {"preset": "a", "allowed": [{"preset": "a", "accounts": ["x"]}]}}}, "policy.roles.main.allowed[0].accounts"),
        ({"roles": {"vision": {"preset": "a"}}}, "policy.roles.vision.preset"),
        ({"budget_usd": -1}, "policy.budget_usd"), ({"budget_usd": float("nan")}, "policy.budget_usd"), ({"budget_usd": True}, "policy.budget_usd"),
        ({"budget_weekly_pct": 101}, "policy.budget_weekly_pct"), ({"terminals": "some"}, "policy.terminals"), ({"surprise": 1}, "policy.surprise"),
    ):
        with pytest.raises(PolicyError) as caught:
            validate(bad, ROLES, "container")
        assert caught.value.field == field, (bad, caught.value.field)
    with pytest.raises(PolicyError, match="policy.terminals"):
        validate({"terminals": "container"}, ROLES, "host")
    assert presets_allowed(kept, "main") == {"a", "b"} and presets_allowed(kept, "vision") is None
    assert allows(kept, "main", "a", "low", "one") and allows(kept, "main", "b", "low", "") and allows(kept, "main", "a", None, None)
    assert not allows(kept, "main", "a", "high", "one") and not allows(kept, "main", "a", None, "two") and not allows(kept, "main", "c", None, None)
    assert allows(kept, "vision", "anything", "max", "x")
    # A row that does not parse is the strictest link, never one without limits.
    from daedalus.extensions.share_policy import parse, terminals
    assert terminals(parse("{broken")) == "none" and presets_allowed(parse("[]"), "main") == set()


async def test_the_operator_sets_a_policy_and_reads_it_back_without_the_password(rig: Rig, roles: list[dict[str, Any]]) -> None:
    mine = await rig.session()
    app, _door = rig.clients()
    async with app:
        options = (await app.get(f"/api/sessions/{mine}/live-shares/options", headers=OWNER)).json()
        assert options["roles"] == roles and options["place"] in ("container", "host") and options["withheld"] == []
        assert options["terminals"] == (["all", "container", "none"] if options["place"] == "container" else ["all", "none"])
        made = await app.post(f"/api/sessions/{mine}/live-shares", headers=OWNER, json={"label": "for Ann\n", "password": "open sesame", "policy": POLICY})
        assert made.status_code == 200, made.text
        link = made.json()
        assert link["label"] == "for Ann" and link["has_password"] is True and link["spent"] == {"usd": 0.0, "weekly_pct": 0.0}
        assert link["policy"]["budget_usd"] == 2.5 and "open sesame" not in made.text and "scrypt" not in made.text
        refused = await app.post(f"/api/sessions/{mine}/live-shares", headers=OWNER, json={"policy": {"roles": {"main": {"preset": "z"}}}})
        assert refused.status_code == 400 and "policy.roles.main.preset" in refused.json()["detail"]
        changed = (await app.patch(f"/api/sessions/{mine}/live-shares/{link['id']}", headers=OWNER, json={"clear_password": True, "label": "x"})).json()
        assert changed["has_password"] is False and changed["label"] == "x"
        assert (await app.patch(f"/api/sessions/{mine}/live-shares/shr_none", headers=OWNER, json={"label": "y"})).status_code == 404
        assert (await app.patch(f"/api/sessions/{mine}/live-shares/{link['id']}", json={"label": "y"})).status_code == 401
        listed = (await app.get(f"/api/sessions/{mine}/live-shares", headers=OWNER)).text
        assert "password_hash" not in listed and "scrypt" not in listed


async def test_a_password_locks_everything_but_the_page_until_it_is_typed(rig: Rig, roles: list[dict[str, Any]]) -> None:
    mine = await rig.session()
    link = await rig.shares.create(mine, password="open sesame")
    other = await rig.shares.create(mine, password="open sesame")
    token, v = token_of(link), f"/v/{token_of(link)}"
    _app, door = rig.clients()
    async with door:
        locked = await door.get(f"{v}/api/share")
        assert locked.status_code == 401 and locked.json()["code"] == "locked"
        assert (await door.get(f"{v}/api/sessions/{mine}")).status_code == 401
        assert (await door.get(f"{v}/")).status_code == 200
        wrong = await door.post(f"{v}/api/share/unlock", json={"password": "nope"})
        assert wrong.status_code == 403 and wrong.json()["code"] == "wrong" and "set-cookie" not in wrong.headers
        opened = await door.post(f"{v}/api/share/unlock", json={"password": "open sesame"})
        assert opened.status_code == 200
        cookie = opened.headers["set-cookie"]
        assert f"Path=/v/{token}/" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" in cookie
        value = cookie.split(";")[0].split("=", 1)[1]
        assert value and value not in token and token not in value
        carried = {"cookie": f"share_pass={value}"}
        assert (await door.get(f"{v}/api/share", headers=carried)).status_code == 200
        # A planted cookie of the same name beside the right one does not lock the visitor out, and alone opens nothing.
        assert (await door.get(f"{v}/api/share", headers={"cookie": f"share_pass=planted; share_pass={value}"})).status_code == 200
        assert (await door.get(f"{v}/api/share", headers={"cookie": "share_pass=planted"})).status_code == 401
        # The pass is this link's alone: the same password on another link is another pass.
        assert (await door.get(f"/v/{token_of(other)}/api/share", headers=carried)).status_code == 401
        # A new password ends the pass handed out before.
        await rig.shares.update(mine, link["id"], password="another")
        assert (await door.get(f"{v}/api/share", headers=carried)).status_code == 401
        # A revoked link unlocks nothing, even with the right password.
        await rig.shares.revoke(mine, link["id"])
        gone = await door.post(f"{v}/api/share/unlock", json={"password": "another"})
        assert gone.status_code == 410 and "set-cookie" not in gone.headers


async def test_guessing_is_slowed_for_the_link_whoever_guesses(rig: Rig) -> None:
    mine = await rig.session()
    link = await rig.shares.create(mine, password="right")
    v = f"/v/{token_of(link)}"
    _app, door = rig.clients()
    async with door:
        codes = [(await door.post(f"{v}/api/share/unlock", json={"password": f"guess{i}"})).status_code for i in range(session_share.FREE_TRIES)]
        assert codes == [403] * session_share.FREE_TRIES
        slow = await door.post(f"{v}/api/share/unlock", json={"password": "right"})
        assert slow.status_code == 429 and slow.json()["code"] == "slow" and slow.json()["retry_after"] >= 1
        # Many at once are judged one at a time: none of them gets past the wait.
        many = await asyncio.gather(*(door.post(f"{v}/api/share/unlock", json={"password": "right"}) for _ in range(5)))
        assert {answer.status_code for answer in many} == {429}
        assert (await door.post(f"{v}/api/share/unlock", json={"password": "x" * 5000})).status_code == 413


async def test_a_socket_opened_before_the_password_changed_is_closed(rig: Rig) -> None:
    mine = await rig.session()
    link = await rig.shares.create(mine)
    token = token_of(link)
    accepted = asyncio.Event()

    async def endless(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "websocket.accept"})
        accepted.set()
        await asyncio.Event().wait()

    async def owner_of(kind: str, thing: str) -> str | None:
        return mine if kind == "terminal" else "container"

    door = ShareDoor(endless, shares=lambda: rig.shares, owner_of=owner_of, dist=rig.app.settings.bot_repo_dir / "miniapp" / "dist")
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await queue.put({"type": "websocket.connect"})
    scope = {"type": "websocket", "path": f"/v/{token}/ws/terminals/t1", "query_string": b"", "headers": [], "daedalus.api_services_door": True}
    task = asyncio.create_task(door(scope, queue.get, send))
    await asyncio.wait_for(accepted.wait(), 5)
    await rig.shares.update(mine, link["id"], password="new one")
    await asyncio.wait_for(task, 2)
    assert sent[-1] == {"type": "websocket.close", "code": 4401}
    # And a new socket without the pass is refused the same way.
    queue2: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await queue2.put({"type": "websocket.connect"})
    sent.clear()
    await door(scope, queue2.get, send)
    assert sent == [{"type": "websocket.close", "code": 4401}]


async def test_the_visitor_sees_the_limits_and_only_the_presets_it_may_pick(rig: Rig, roles: list[dict[str, Any]]) -> None:
    from daedalus.extensions.share_policy import validate

    mine = await rig.session()
    presets = dict(rig.manager.config.presets)
    first, *rest = list(presets) or ["none"]
    if not rest:
        pytest.skip("the test configuration has a single preset")
    narrowed = {"roles": {"main": {"preset": first, "effort": "", "account": "", "allowed": []}}}
    roles_here = [{"id": "main", "presets": [{"id": pid, "efforts": ["low"], "accounts": []} for pid in presets]}]
    link = await rig.shares.create(mine, policy=validate(narrowed, roles_here, "container"))
    v = f"/v/{token_of(link)}"
    _app, door = rig.clients()
    async with door:
        limits = (await door.get(f"{v}/api/share")).json()["limits"]
        assert limits["roles"]["main"]["preset"] == first and limits["budget"] == {"usd": None, "weekly_pct": None} and limits["terminals"] == "all"
        picked = (await door.get(f"{v}/api/settings")).json()
        assert list(picked["presets"]) == [first]
        refused = await door.post(f"{v}/api/sessions/{mine}/model", json={"preset": rest[0]})
        assert refused.status_code == 403 and refused.json()["code"] == "not_allowed"
        for body in ({"clear": True}, {"provider": "x", "model": "y"}, {"reasoning_effort": "low"}):
            assert (await door.post(f"{v}/api/sessions/{mine}/model", json=body)).status_code == 403, body
        assert (await door.post(f"{v}/api/sessions/{mine}/model", json={"preset": first})).status_code == 200


async def test_terminals_follow_the_links_setting(rig: Rig) -> None:
    mine = await rig.session()
    link = await rig.shares.create(mine)
    token = token_of(link)
    seen: list[dict[str, Any]] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def owner_of(kind: str, thing: str) -> str | None:
        if kind == "terminal_env":
            return "host" if thing == "t_host" else "container"
        return mine if kind == "terminal" else None

    door = ShareDoor(inner, shares=lambda: rig.shares, owner_of=owner_of, dist=rig.app.settings.bot_repo_dir / "miniapp" / "dist", app=rig.app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ServicesDoor(door)), base_url="http://door.test") as client:  # type: ignore[arg-type]
        v = f"/v/{token}"
        assert (await client.get(f"{v}/api/terminals/t_host")).status_code == 200
        await rig.shares.update(mine, link["id"], policy={"terminals": "container"})
        assert (await client.get(f"{v}/api/terminals/t_host")).status_code == 404
        assert (await client.get(f"{v}/api/terminals/t_box")).status_code == 200
        await client.get(f"{v}/api/terminals?env=host")
        assert b"env=container" in seen[-1]["query_string"] and b"env=host" not in seen[-1]["query_string"]
        await rig.shares.update(mine, link["id"], policy={"terminals": "none"})
        before = len(seen)
        listing = await client.get(f"{v}/api/terminals")
        assert listing.json()["terminals"] == [] and len(seen) == before
        for path in ("/api/terminals/t_box", "/api/terminals/t_box/commands"):
            assert (await client.get(f"{v}{path}")).status_code == 404
        assert (await client.post(f"{v}/api/terminals/t_box/ticket")).status_code == 404


async def test_a_spent_budget_refuses_what_starts_a_turn(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    from daedalus.host import share_spend

    mine = await rig.session()
    link = await rig.shares.create(mine)
    v = f"/v/{token_of(link)}"

    async def exhausted(app: Any, share_id: str) -> str | None:
        return "usd" if share_id == link["id"] else None

    monkeypatch.setattr(share_spend, "exhausted", exhausted)
    _app, door = rig.clients()
    async with door:
        for suffix in ("/messages", "/retry", "/answer", "/asks/answer", "/steer/m1"):
            answer = await door.post(f"{v}/api/sessions/{mine}{suffix}", json={"text": "hi"})
            assert answer.status_code == 402 and answer.json()["code"] == "budget", suffix
        assert (await door.post(f"{v}/api/sessions/{mine}/stop")).status_code == 200
        assert (await door.get(f"{v}/api/sessions/{mine}")).status_code == 200


def test_the_attachments_are_on_the_list() -> None:
    assert route_of("GET", "/api/sessions/abc/attachments") is not None
