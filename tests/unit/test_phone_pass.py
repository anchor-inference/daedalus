"""A phone reaches one service: by a code in a QR, spent once, that is no way into the app."""

from __future__ import annotations

import asyncio
import socket
import threading
from datetime import UTC, datetime, timedelta
from http.server import HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from daedalus import public_address
from daedalus.extensions import api_public_address, api_services_door
from daedalus.extensions.api import build_app
from daedalus.extensions.api_services_door import LAN_KEY, Lan, LanDoor, ServicesDoor
from daedalus.extensions.services import PHONE_COOKIE_PREFIX
from daedalus.extensions.tunnel_address import EXTENSION_KEY
from daedalus.stores import pairing
from daedalus.stores.database import Database
from tests.unit.test_services_origin import FakeApp, Upstream

SLUG = "demo-ab12"


async def test_a_code_is_spent_once_and_becomes_a_pass_for_that_service_alone(db: Database) -> None:
    code = await pairing.mint_phone(db, SLUG)
    assert await pairing.redeem_phone(db, code, "other-slug") is None, "one service's QR opens no other"
    issued = await pairing.redeem_phone(db, code, SLUG)
    assert issued and await pairing.phone_admitted(db, SLUG, issued)
    assert await pairing.redeem_phone(db, code, SLUG) is None, "spent"
    assert not await pairing.phone_admitted(db, "other-slug", issued)
    assert not await pairing.phone_admitted(db, SLUG, "guessed")
    assert not await pairing.phone_admitted(db, SLUG, None)


async def test_a_code_is_spent_by_one_request_however_many_arrive(db: Database) -> None:
    for _ in range(50):
        code = await pairing.mint_phone(db, SLUG)
        won = [p for p in await asyncio.gather(*(pairing.redeem_phone(db, code, SLUG) for _ in range(4))) if p]
        assert len(won) == 1


async def test_a_pass_from_the_wifi_is_no_good_through_the_public_address(db: Database) -> None:
    """Plain http on a home network can be read by anyone on it. What was read there must stay there."""
    code = await pairing.mint_phone(db, SLUG, lan=True)
    assert await pairing.redeem_phone(db, code, SLUG) is None, "a home network's code is spent there"
    issued = await pairing.redeem_phone(db, code, SLUG, lan=True)
    assert issued and await pairing.phone_admitted(db, SLUG, issued, lan=True)
    assert not await pairing.phone_admitted(db, SLUG, issued), "sniffed on the Wi-Fi, it opens nothing from outside"
    code = await pairing.mint_phone(db, SLUG)
    issued = await pairing.redeem_phone(db, code, SLUG)
    assert not await pairing.phone_admitted(db, SLUG, issued, lan=True)


async def test_nothing_readable_is_kept(db: Database) -> None:
    code = await pairing.mint_phone(db, SLUG)
    issued = await pairing.redeem_phone(db, code, SLUG)
    row = await db.fetchone("SELECT * FROM phone_passes")
    assert code not in str(dict(row)) and str(issued) not in str(dict(row))


async def test_a_phones_code_is_no_way_into_the_app(db: Database) -> None:
    code = await pairing.mint_phone(db, SLUG)
    assert await pairing.redeem(db, code) is None
    assert await pairing.outstanding(db) == 0, "the login page must not say that pairing is on"
    app_code = await pairing.mint(db)
    assert await pairing.redeem_phone(db, app_code, SLUG) is None, "nor the other way"
    await pairing.redeem(db, app_code)
    assert await pairing.redeem_phone(db, code, SLUG), "and spending an app code leaves a phone's alone"


async def test_a_code_and_a_pass_run_out(db: Database) -> None:
    code = await pairing.mint_phone(db, SLUG)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await db.execute("UPDATE phone_passes SET code_expires_at = ?", (past,))
    assert await pairing.redeem_phone(db, code, SLUG) is None
    code = await pairing.mint_phone(db, SLUG)
    issued = await pairing.redeem_phone(db, code, SLUG)
    await db.execute("UPDATE phone_passes SET expires_at = ?", (past,))
    assert not await pairing.phone_admitted(db, SLUG, issued)
    await pairing.mint_phone(db, SLUG)
    assert len(await db.fetchall("SELECT id FROM phone_passes")) == 1, "what ran out does not pile up"


async def test_a_phone_can_be_forgotten(db: Database) -> None:
    issued = await pairing.redeem_phone(db, await pairing.mint_phone(db, SLUG), SLUG)
    await pairing.forget_phones(db, SLUG)
    assert not await pairing.phone_admitted(db, SLUG, issued)




@pytest.fixture
def upstream() -> Any:
    Upstream.seen = []
    server = HTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def phone_pairing(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The door's side, with the store's own behaviour tested above."""
    state = {"code": "the-code", "pass": "the-pass", "spent": ""}

    async def redeem(_db: Any, code: str, slug: str, *, lan: bool = False) -> str | None:
        if code != state["code"] or state["spent"]:
            return None
        state["spent"] = slug
        state["lan"] = lan  # type: ignore[assignment]
        return state["pass"]

    async def admitted(_db: Any, slug: str, issued: str | None, *, lan: bool = False) -> bool:
        return issued == state["pass"] and state["spent"] == slug and state.get("lan") == lan

    monkeypatch.setattr(pairing, "redeem_phone", redeem)
    monkeypatch.setattr(pairing, "phone_admitted", admitted)
    return state


def door(upstream: HTTPServer, base: str = "https://s.example.test", wrap: Any = ServicesDoor) -> TestClient:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test")
    app.extensions["services"].rows[SLUG]["share_mode"] = "local"
    return TestClient(wrap(build_app(app, "tok")), base_url=base, follow_redirects=False)  # type: ignore[arg-type]


def test_a_private_service_stays_shut_without_a_pass(upstream: HTTPServer, phone_pairing: dict[str, str]) -> None:
    client = door(upstream)
    assert client.get(f"/s/{SLUG}/").status_code == 404
    client.cookies.set(PHONE_COOKIE_PREFIX + SLUG, "guessed")
    assert client.get(f"/s/{SLUG}/").status_code == 404
    assert not Upstream.seen


def test_a_qr_opens_a_private_service_for_that_phone_and_leaves_the_address(upstream: HTTPServer, phone_pairing: dict[str, str]) -> None:
    client = door(upstream)
    first = client.get(f"/s/{SLUG}/page?phone=the-code&x=1")
    assert first.status_code == 303 and first.headers["location"] == f"/s/{SLUG}/page?x=1"
    cookie = first.headers["set-cookie"]
    assert "HttpOnly" in cookie and f"Path=/s/{SLUG}" in cookie and "Secure" in cookie
    assert client.get(f"/s/{SLUG}/page?x=1").status_code == 200
    assert PHONE_COOKIE_PREFIX not in Upstream.seen[-1].get("cookie", ""), "the service never hears the pass"


def test_a_used_code_says_so(upstream: HTTPServer, phone_pairing: dict[str, str]) -> None:
    client = door(upstream)
    client.get(f"/s/{SLUG}/?phone=the-code")
    client.cookies.clear()
    answer = client.get(f"/s/{SLUG}/?phone=the-code")
    assert answer.status_code == 403 and "QR" in answer.text


def test_on_the_home_network_the_cookie_cannot_ask_for_https(upstream: HTTPServer, phone_pairing: dict[str, str]) -> None:
    """Plain http, with an https door beside it: a Secure cookie would be dropped and the phone would loop."""
    client = door(upstream, base="http://192.0.2.10:8766", wrap=LanDoor)
    answer = client.get(f"/s/{SLUG}/?phone=the-code")
    assert answer.status_code == 303 and "Secure" not in answer.headers["set-cookie"]


def test_the_door_tells_a_code_which_door_it_came_by(upstream: HTTPServer, phone_pairing: dict[str, str]) -> None:
    client = door(upstream, base="http://192.0.2.10:8766", wrap=LanDoor)
    client.get(f"/s/{SLUG}/?phone=the-code")
    assert phone_pairing["lan"] is True
    assert client.get(f"/s/{SLUG}/").status_code == 200
    # The same cookie carried to the other door: a pass sniffed on the Wi-Fi, tried through the tunnel.
    outside = door(upstream)
    outside.cookies.set(PHONE_COOKIE_PREFIX + SLUG, "the-pass")
    assert outside.get(f"/s/{SLUG}/").status_code == 404


def test_a_code_is_no_good_on_the_apps_own_address(upstream: HTTPServer, phone_pairing: dict[str, str]) -> None:
    app = FakeApp(upstream.server_port, services_public_url="https://s.example.test")
    app.extensions["services"].rows[SLUG]["share_mode"] = "local"
    site = TestClient(build_app(app, "tok"), base_url="https://app.example.test", follow_redirects=False)  # type: ignore[arg-type]
    answer = site.get(f"/s/{SLUG}/?phone=the-code")
    assert answer.status_code == 307 and answer.headers["location"].startswith("https://s.example.test/"), "it is sent to the door, unspent"
    assert not phone_pairing["spent"]


def test_the_home_networks_door_serves_what_the_door_serves_and_nothing_else(upstream: HTTPServer, phone_pairing: dict[str, str]) -> None:
    client = door(upstream, base="http://192.0.2.10:8766", wrap=LanDoor)
    for path in ("/api/auth/me", "/app/", "/pages/x/", "/c/x", "/"):
        assert client.get(path).status_code == 404, path


async def test_the_lan_door_marks_what_it_lets_in() -> None:
    seen: dict[str, Any] = {}

    async def inner(scope: Any, _receive: Any, _send: Any) -> None:
        seen.update(scope)

    await LanDoor(inner)({"type": "http", "path": "/s/x/"}, None, None)  # type: ignore[arg-type]
    assert seen[LAN_KEY] is True and seen[api_services_door.SCOPE_KEY] is True


def test_the_address_read_for_a_phone_is_a_home_address_or_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    class Probe:
        address = "192.0.2.5"

        def __enter__(self) -> Probe:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

        def connect(self, _: Any) -> None:
            pass

        def getsockname(self) -> tuple[str, int]:
            return (Probe.address, 1)

    monkeypatch.setattr(api_services_door.socket, "socket", lambda *_: Probe())
    assert api_services_door.lan_address() == "192.0.2.5"
    Probe.address = "127.0.0.1"
    assert api_services_door.lan_address() == ""
    Probe.address = "169.254.1.1"
    assert api_services_door.lan_address() == ""
    Probe.address = "8.8.8.8"
    assert api_services_door.lan_address() == "", "a machine with a public address has no home network to speak of"


async def test_closing_the_lan_door_ends_the_connections_it_holds() -> None:
    """A cancelled server stops accepting and goes on serving what it has: a phone's keep-alive outlived the door."""
    async def inner(_scope: Any, _receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    settings = type("S", (), {"services_door_port": 0})()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        settings.services_door_port = probe.getsockname()[1]
    lan = Lan(lambda door: uvicorn.Config(door, log_level="critical", lifespan="off"), inner)
    lan.base(settings, "127.0.0.1")  # type: ignore[arg-type]
    await asyncio.sleep(0.3)
    reader, writer = await asyncio.open_connection("127.0.0.1", lan.port)
    writer.write(b"GET /s/x/ HTTP/1.1\r\nHost: x\r\n\r\n")
    await writer.drain()
    assert b"204" in await asyncio.wait_for(reader.read(200), 3)
    lan.close()
    assert await asyncio.wait_for(reader.read(), 3) == b"", "the door closed it"
    writer.close()


async def test_the_lan_door_opens_on_one_address_and_closes_by_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """On loopback here, since the tests may not reach anything else: what is proved is the opening and closing."""
    served: list[int] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            served.append(1)
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

    settings = type("S", (), {"services_door_port": 0})()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        settings.services_door_port = probe.getsockname()[1]
    lan = Lan(lambda door: uvicorn.Config(door, log_level="warning", lifespan="off"), inner)
    base = lan.base(settings, "127.0.0.1")  # type: ignore[arg-type]
    assert base == f"http://127.0.0.1:{settings.services_door_port}"
    assert lan.base(settings, "127.0.0.1") == base, "a second QR keeps the door and its port"  # type: ignore[arg-type]
    await asyncio.sleep(0.2)
    reader, writer = await asyncio.open_connection("127.0.0.1", lan.port)
    writer.write(b"GET /s/x/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    await writer.drain()
    await reader.read()
    writer.close()
    assert served
    assert lan.closer is not None and lan.closer.when() - asyncio.get_running_loop().time() > 3500, "it closes itself in an hour"
    lan.close()
    await asyncio.sleep(0.1)
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", lan.port)


# -- the route ---------------------------------------------------------------------------------------


class Services:
    def __init__(self, mode: str) -> None:
        self.row = {"share_mode": mode, "share_slug": SLUG, "share_key": "k", "port": 3000}

    async def get(self, _session: str, name: str) -> dict[str, Any] | None:
        return self.row if name == "web" else None

    async def ensure_slug(self, _session: str, _name: str) -> str:
        return SLUG


minted: list[bool] = []


def phone_route(monkeypatch: pytest.MonkeyPatch, *, native: bool, lan: str = "", mode: str = "local", url: str = "") -> TestClient:
    app = FakeApp(1, native=native, services_public_url=url, miniapp_public_url="")
    app.extensions["services"] = Services(mode)
    app.extensions[EXTENSION_KEY] = object()
    monkeypatch.setattr(api_services_door, "lan_address", lambda: lan)
    monkeypatch.setattr(api_services_door.Lan, "base", lambda self, _settings, host: f"http://{host}:8766")

    async def mint(_db: Any, _slug: str, *, lan: bool = False) -> str:
        minted.append(lan)
        return "code/1"

    monkeypatch.setattr(pairing, "mint_phone", mint)
    api = FastAPI()
    api_public_address.register(api, app, lambda: {}, door_config=lambda _: None)  # type: ignore[arg-type]
    return TestClient(api)


def kinds(answer: Any) -> dict[str, str]:
    return {way["kind"]: way["url"] for way in answer.json()["ways"]}


def test_a_private_service_gets_a_code_on_each_way_the_home_network_first(monkeypatch: pytest.MonkeyPatch) -> None:
    client = phone_route(monkeypatch, native=True, lan="192.0.2.10", url="https://x.example.test")
    answer = client.post("/api/sessions/s/services/web/phone")
    assert [w["kind"] for w in answer.json()["ways"]] == ["wifi", "tunnel"]
    wifi = urlsplit(kinds(answer)["wifi"])
    assert wifi.netloc == "192.0.2.10:8766" and parse_qs(wifi.query)["phone"] == ["code/1"], "the code survives the address"
    assert answer.json()["ways"][0]["qr"], "drawn by the host"


def test_a_public_service_needs_no_code(monkeypatch: pytest.MonkeyPatch) -> None:
    client = phone_route(monkeypatch, native=True, lan="192.0.2.10", url="https://x.example.test", mode="public")
    assert kinds(client.post("/api/sessions/s/services/web/phone"))["tunnel"] == f"https://x.example.test/s/{SLUG}/"


def test_a_key_never_goes_into_a_qr(monkeypatch: pytest.MonkeyPatch) -> None:
    """It lasts, and on the Wi-Fi it would travel as plain http. A code does neither."""
    client = phone_route(monkeypatch, native=True, lan="192.0.2.10", url="https://x.example.test", mode="key")
    ways = kinds(client.post("/api/sessions/s/services/web/phone"))
    assert all("key=" not in url and "phone=" in url for url in ways.values())


def test_each_way_gets_a_code_for_its_own_door(monkeypatch: pytest.MonkeyPatch) -> None:
    minted.clear()
    client = phone_route(monkeypatch, native=True, lan="192.0.2.10", url="https://x.example.test")
    client.post("/api/sessions/s/services/web/phone")
    assert minted == [True, False]


def test_no_tunnel_means_the_home_network_alone_and_this_machines_own_name_is_no_way(monkeypatch: pytest.MonkeyPatch) -> None:
    client = phone_route(monkeypatch, native=True, lan="192.0.2.10", url="http://localhost:8766")
    assert list(kinds(client.post("/api/sessions/s/services/web/phone"))) == ["wifi"]
    client = phone_route(monkeypatch, native=True, lan="")
    assert client.post("/api/sessions/s/services/web/phone").json()["ways"] == []


def test_a_server_has_its_address_and_no_home_network(monkeypatch: pytest.MonkeyPatch) -> None:
    client = phone_route(monkeypatch, native=False, lan="192.0.2.10", url="https://s.example.test")
    assert list(kinds(client.post("/api/sessions/s/services/web/phone"))) == ["address"]


def test_a_service_with_no_port_has_nothing_to_open(monkeypatch: pytest.MonkeyPatch) -> None:
    client = phone_route(monkeypatch, native=True, lan="192.0.2.10")
    assert client.post("/api/sessions/s/services/other/phone").status_code == 404


def test_the_address_is_published_for_qr_too(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FakeApp(1, native=True)
    public_address.publish(app.settings, services="https://x.example.test")
    assert public_address.services_base(app.settings) == "https://x.example.test"


async def test_changing_a_services_terms_ends_what_phones_held(db: Database, tmp_path: Any) -> None:
    from types import SimpleNamespace

    from daedalus.extensions.services import Services as Real

    real = Real.__new__(Real)
    real.app = SimpleNamespace(db=db, settings=SimpleNamespace())
    rows = {"share_mode": "key", "share_slug": SLUG, "share_key": "k", "port": 3000, "status": "running"}

    async def get(_session: str, _name: str) -> dict[str, Any]:
        return dict(rows)

    real.get = get  # type: ignore[method-assign]
    real.view = lambda row: row  # type: ignore[method-assign]
    original = db.execute

    async def only_passes(sql: str, params: Any = ()) -> None:
        if "phone_passes" in sql:
            await original(sql, params)

    db.execute = only_passes  # type: ignore[method-assign]
    for mode, rotate in (("public", False), ("key", True), ("local", False)):
        issued = await pairing.redeem_phone(db, await pairing.mint_phone(db, SLUG), SLUG)
        await real.share("s", "web", mode, rotate_key=rotate)
        assert not await pairing.phone_admitted(db, SLUG, issued), mode
        rows["share_mode"] = "key"
    issued = await pairing.redeem_phone(db, await pairing.mint_phone(db, SLUG), SLUG)
    await real.share("s", "web", "key")
    assert await pairing.phone_admitted(db, SLUG, issued), "the same terms again change nothing"
    db.execute = original  # type: ignore[method-assign]
