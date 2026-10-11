"""Custom pages: the store in the state volume, the API over it, and the pages served at /pages/<slug>/."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from daedalus.config import PagesConfig, RuntimeConfig, Settings
from daedalus.extensions.api import SESSION_COOKIE, build_app
from daedalus.extensions.api_proxy import frameable_policy, scoped_set_cookie, without_cookies
from daedalus.host.pages import PageError, PageStore, clean_path, listing_row
from daedalus.stores.database import Database

H = {"X-Daedalus-Token": "tok"}


# -- the store ----------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> PageStore:
    return PageStore(tmp_path / "pages")


@pytest.mark.parametrize("bad", ["", "/etc/passwd", "../x", "a/../../b", "a//b", "./a", "a/.", "C:/x", "a\\b", "a\x00b"])
def test_a_path_that_could_leave_the_page_is_refused(bad: str) -> None:
    with pytest.raises(PageError):
        clean_path(bad)


def test_ordinary_paths_pass_whatever_their_type() -> None:
    for good in ("index.html", "assets/app.js", "models/engine.glb", ".well-known/x", "a b/c d.wasm", "data/archive.tar.zst"):
        assert clean_path(good) == good


@pytest.mark.parametrize("slug", ["", "-x", "_kit", "Upper", "a/b", "a.b", "x" * 64, ".."])
async def test_a_slug_is_one_plain_path_segment(store: PageStore, slug: str) -> None:
    with pytest.raises(PageError):
        await store.create(slug, title="T")


async def test_create_write_publish_and_roll_back(store: PageStore) -> None:
    created = await store.create("tools", title={"en": "Tools", "ru": "Инструменты"}, icon="grid")
    assert created["version"] is None and created["draft"]["kind"] == "static"
    with pytest.raises(PageError) as refused:
        await store.publish("tools")
    assert refused.value.status == 409  # nothing to open yet: the entry is missing
    await store.write_file("tools", "index.html", b"<p>one</p>")
    await store.write_file("tools", "assets/app.js", b"console.log(1)")
    first = await store.publish("tools", "first")
    assert first["version"] == 1 and first["versions"][0]["files"] == 2
    await store.write_file("tools", "index.html", b"<p>two</p>")
    # The live version is a copy: editing the draft does not change what is served.
    assert store.version_file("tools", 1, "index.html").read_bytes() == b"<p>one</p>"
    second = await store.publish("tools")
    assert second["version"] == 2
    back = await store.rollback("tools", 1)
    assert back["version"] == 1
    served = store.served(back)
    assert served is not None and (served.root / "index.html").read_bytes() == b"<p>one</p>"
    with pytest.raises(PageError) as missing:
        await store.rollback("tools", 9)
    assert missing.value.status == 404
    row = listing_row(back)
    assert row == {"slug": "tools", "title": "Tools", "titles": {"en": "Tools", "ru": "Инструменты"}, "icon": "grid", "icon_path": None,
                   "path": "/app/p/tools", "version": 1, "updated_at": back["updated_at"]}


async def test_publish_numbers_past_an_orphan_left_by_a_crash(store: PageStore) -> None:
    await store.create("p", title="P")
    await store.write_file("p", "index.html", b"x")
    # A process killed after the rename and before the manifest: a version directory nobody lists.
    (store.folder("p") / "versions" / "1").mkdir(parents=True)
    published = await store.publish("p")
    assert published["version"] == 2
    assert not [p for p in (store.folder("p") / "versions").iterdir() if p.name.startswith(".")]


async def test_old_versions_are_pruned_but_the_live_one_is_kept(tmp_path: Path) -> None:
    store = PageStore(tmp_path / "pages", PagesConfig(keep_versions=2))
    await store.create("p", title="P")
    await store.write_file("p", "index.html", b"1")
    await store.publish("p")
    await store.rollback("p", 1)
    for _ in range(3):
        await store.publish("p")
    manifest = await store.require("p")
    # Publishing makes the newest live, so the two newest are kept and the rest are gone from disk too.
    assert [v["n"] for v in manifest["versions"]] == [3, 4]
    assert sorted(p.name for p in (store.folder("p") / "versions").iterdir()) == ["3", "4"]
    await store.rollback("p", 3)
    await store.publish("p")
    assert [v["n"] for v in (await store.require("p"))["versions"]] == [4, 5]


async def test_caps_refuse_a_write_and_leave_the_old_file(tmp_path: Path) -> None:
    store = PageStore(tmp_path / "pages", PagesConfig(max_page_bytes=10, max_files=2))
    await store.create("p", title="P")
    await store.write_file("p", "index.html", b"12345")
    with pytest.raises(PageError) as big:
        await store.write_file("p", "index.html", b"x" * 11)
    assert big.value.status == 413
    assert store.draft_file("p", "index.html").read_bytes() == b"12345"
    assert not [p for p in (store.folder("p") / "draft").iterdir() if p.name.endswith(".part")]
    await store.write_file("p", "b.css", b"1")
    with pytest.raises(PageError, match="max_files"):
        await store.write_file("p", "c.css", b"1")
    # Replacing a file is not a new file, and the room counts what it replaces.
    await store.write_file("p", "index.html", b"123456789")


async def test_zero_lifts_the_caps(tmp_path: Path) -> None:
    store = PageStore(tmp_path / "pages", PagesConfig(max_page_bytes=0, max_files=0))
    await store.create("p", title="P")
    for n in range(30):
        await store.write_file("p", f"f{n}.bin", os.urandom(2048))
    assert len(store.files("p")) == 30


async def test_a_planted_link_does_not_lead_out(store: PageStore, tmp_path: Path) -> None:
    await store.create("p", title="P")
    outside = tmp_path / "secret.txt"
    outside.write_text("no")
    (store.folder("p") / "draft" / "leak").symlink_to(outside)
    with pytest.raises(PageError, match="outside"):
        store.draft_file("p", "leak")
    # and a published copy leaves the link behind
    await store.write_file("p", "index.html", b"x")
    await store.publish("p")
    assert not (store.folder("p") / "versions" / "1" / "leak").exists()


async def test_a_draft_is_replaced_by_a_folder(store: PageStore, tmp_path: Path) -> None:
    await store.create("p", title="P")
    await store.write_file("p", "old.txt", b"old")
    source = tmp_path / "built"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("<p>built</p>")
    (source / "assets" / "app.js").write_text("1")
    assert await store.replace_draft("p", source) == {"files": 2, "bytes": len("<p>built</p>") + 1}
    assert {f["path"] for f in store.files("p")} == {"index.html", "assets/app.js"}
    with pytest.raises(PageError) as through:
        await store.write_file("p", "index.html/inner.js", b"x")
    assert through.value.status == 409


async def test_remove_keeps_nothing(store: PageStore) -> None:
    await store.create("p", title="P")
    await store.write_file("p", "index.html", b"x")
    await store.publish("p")
    assert await store.remove("p")
    assert not store.folder("p").exists() and await store.get("p") is None
    assert not await store.remove("p")
    assert [p.name for p in store.root.iterdir()] == []


async def test_targets_and_icons_are_checked(store: PageStore) -> None:
    await store.create("svc", title="S", kind="service", target=8105)
    with pytest.raises(PageError):
        await store.create("svc2", title="S", kind="service", target=70000)
    with pytest.raises(PageError):
        await store.create("lnk", title="L", kind="link", target="javascript:alert(1)")
    with pytest.raises(PageError):
        await store.create("ic", title="I", icon_path="<script>")
    made = await store.create("ic2", title="I", icon_path="M4 4h16v16H4z")
    assert made["icon_path"] == "M4 4h16v16H4z"
    updated = await store.update("svc", target="notebook")
    assert updated["draft"]["target"] == "notebook"


async def test_every_change_is_announced(tmp_path: Path) -> None:
    heard: list[tuple[str, str, int | None]] = []

    async def on_change(slug: str, change: str, version: int | None) -> None:
        heard.append((slug, change, version))

    store = PageStore(tmp_path / "pages", on_change=on_change)  # type: ignore[arg-type]
    await store.create("p", title="P")
    await store.write_file("p", "index.html", b"x")
    await store.publish("p")
    await store.update("p", title="Q")
    await store.rollback("p", 1)
    await store.remove("p")
    assert [c for _, c, _ in heard] == ["created", "draft", "published", "updated", "rolled_back", "removed"]
    assert heard[2] == ("p", "published", 1)


async def test_concurrent_writes_to_one_page_all_land(store: PageStore) -> None:
    await store.create("p", title="P")
    await asyncio.gather(*(store.write_file("p", f"f{n}.txt", b"x") for n in range(20)))
    assert len(store.files("p")) == 20


# -- the proxy's header handling ------------------------------------------------------------------


def test_header_rewrites() -> None:
    assert without_cookies("a=1; daedalus_session=x; b=2", ["daedalus_session"]) == "a=1; b=2"
    assert scoped_set_cookie("sid=1; Path=/; HttpOnly", "/pages/nb") == "sid=1; Path=/pages/nb/; HttpOnly"
    assert scoped_set_cookie("sid=1; Path=/api", "/pages/nb") == "sid=1; Path=/pages/nb/api"
    assert scoped_set_cookie("sid=1; path=/pages/nb/x", "/pages/nb") == "sid=1; path=/pages/nb/x"
    assert frameable_policy("default-src 'self'; frame-ancestors 'none'") == "default-src 'self'; frame-ancestors 'self'"


# -- the API and the serving ----------------------------------------------------------------------


class Upstream(BaseHTTPRequestHandler):
    """An agent's web app: echoes what reached it, sets a root cookie, refuses framing, redirects."""

    def _answer(self, status: int, body: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/go":
            self._answer(302, b"", {"Location": "/landed"})
            return
        body = f"path={self.path} prefix={self.headers.get('x-forwarded-prefix')} cookie={self.headers.get('cookie')}".encode()
        self._answer(200, body, {"Content-Type": "text/plain", "X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'", "Set-Cookie": "app=1; Path=/"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        self._answer(201, b"got " + str(len(self.rfile.read(length))).encode())

    def log_message(self, *args: Any) -> None:
        pass


class FakeServices:
    def __init__(self, port: int) -> None:
        self.port = port

    async def rows(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [{"name": "notebook", "status": "running", "pid": os.getpid(), "port": self.port, "session_id": "s1"}]


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event_type: str, payload: dict[str, Any], **_: Any) -> None:
        self.events.append((event_type, payload))


@pytest.fixture
def upstream() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port
    server.shutdown()


def _app(settings: Settings, db: Any, port: int) -> SimpleNamespace:
    return SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=SimpleNamespace(bus=FakeBus()), front=None,
                           extensions={"services": FakeServices(port)}, guard=None)


@pytest.fixture
async def site(settings: Settings, db: Database, upstream: int, tmp_path: Path) -> Any:
    kit = tmp_path / "repo" / "miniapp" / "dist" / "kit"
    kit.mkdir(parents=True)
    (kit / "kit.css").write_text(":root{--bg:#000}")
    (kit / "daedalus.js").write_text("window.Daedalus={}")
    settings.bot_repo_dir = tmp_path / "repo"
    app = _app(settings, db, upstream)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        yield client, app


async def _static_page(client: httpx.AsyncClient, slug: str = "demo") -> None:
    assert (await client.post("/api/pages", json={"slug": slug, "title": {"en": "Demo", "ru": "Демо"}, "icon": "grid"}, headers=H)).status_code == 200
    for path, body in (("index.html", b"<h1>live</h1>"), ("app.js", b"1"), ("model.glb", b"glTF"), ("blob.qqzz", b"?"), ("sub/index.html", b"sub")):
        r = await client.put(f"/api/pages/{slug}/draft/{path}", content=body, headers=H)
        assert r.status_code == 200, r.text


async def test_the_list_offers_only_published_pages_in_the_agreed_shape(site: Any) -> None:
    client, app = site
    assert (await client.get("/api/pages")).status_code == 401
    assert (await client.get("/api/pages", headers=H)).json() == {"pages": []}
    await _static_page(client)
    assert (await client.get("/api/pages", headers=H)).json() == {"pages": []}
    assert [p["slug"] for p in (await client.get("/api/pages?all=1", headers=H)).json()["pages"]] == ["demo"]
    published = (await client.post("/api/pages/demo/publish", json={"note": "first"}, headers=H)).json()
    assert published["version"] == 1 and published["versions"][0]["note"] == "first"
    pages = (await client.get("/api/pages", headers=H)).json()["pages"]
    assert pages == [{"slug": "demo", "title": "Demo", "titles": {"en": "Demo", "ru": "Демо"}, "icon": "grid", "icon_path": None,
                      "path": "/app/p/demo", "version": 1, "updated_at": pages[0]["updated_at"]}]
    assert ("pages.changed", {"slug": "demo", "change": "published", "version": 1}) in app.manager.bus.events


async def test_pages_need_the_sign_in_and_get_the_cookie_from_it(site: Any) -> None:
    client, _ = site
    await _static_page(client)
    await client.post("/api/pages/demo/publish", headers=H)
    refused = await client.get("/pages/demo/")
    assert refused.status_code == 401 and "Sign in" in refused.text
    signed = await client.post("/api/pages/_signin", headers=H)
    assert signed.status_code == 200 and SESSION_COOKIE in signed.headers["set-cookie"]
    page = await client.get("/pages/demo/")  # the cookie alone, as a frame's navigation sends it
    assert page.status_code == 200 and page.text == "<h1>live</h1>"
    assert page.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert "x-frame-options" not in page.headers


async def test_static_files_carry_their_types_and_fall_back_to_the_entry(site: Any) -> None:
    client, _ = site
    await _static_page(client)
    await client.post("/api/pages/demo/publish", headers=H)
    assert (await client.get("/pages/demo", headers=H, follow_redirects=False)).headers["location"] == "/pages/demo/"
    assert (await client.get("/pages/demo/app.js", headers=H)).headers["content-type"].startswith("text/javascript")
    assert (await client.get("/pages/demo/model.glb", headers=H)).headers["content-type"] == "model/gltf-binary"
    assert (await client.get("/pages/demo/blob.qqzz", headers=H)).headers["content-type"] == "application/octet-stream"
    assert (await client.get("/pages/demo/sub/", headers=H)).text == "sub"
    # An app with its own router: an address that names no file is the app.
    assert (await client.get("/pages/demo/settings/profile", headers=H)).text == "<h1>live</h1>"
    assert (await client.get("/pages/demo/missing.png", headers=H)).status_code == 404
    assert (await client.get("/pages/demo/%2e%2e/%2e%2e/page.json", headers=H)).status_code == 404
    assert (await client.get("/pages/nope/", headers=H)).status_code == 404
    assert (await client.post("/pages/demo/", headers=H)).status_code == 405


async def test_the_draft_is_served_on_request_and_stays_for_its_files(site: Any) -> None:
    client, _ = site
    await _static_page(client)
    await client.post("/api/pages/demo/publish", headers=H)
    await client.put("/api/pages/demo/draft/index.html", content=b"<h1>draft</h1>", headers=H)
    await client.put("/api/pages/demo/draft/app.js", content=b"2", headers=H)
    nav = {**H, "Sec-Fetch-Dest": "iframe"}
    assert (await client.get("/pages/demo/", headers=nav)).text == "<h1>live</h1>"
    draft = await client.get("/pages/demo/?draft=1", headers=nav)
    assert draft.text == "<h1>draft</h1>" and draft.headers["cache-control"] == "no-store"
    # The draft's own script asks by a relative address; the cookie the draft set carries the question.
    assert (await client.get("/pages/demo/app.js", headers={**H, "Sec-Fetch-Dest": "script"})).text == "2"
    # Opening the page again without the question is the live page, files and all.
    assert (await client.get("/pages/demo/", headers=nav)).text == "<h1>live</h1>"
    assert (await client.get("/pages/demo/app.js", headers={**H, "Sec-Fetch-Dest": "script"})).text == "1"


async def test_rollback_and_remove_over_the_api(site: Any) -> None:
    client, _ = site
    await _static_page(client)
    await client.post("/api/pages/demo/publish", headers=H)
    await client.put("/api/pages/demo/draft/index.html", content=b"<h1>v2</h1>", headers=H)
    await client.post("/api/pages/demo/publish", headers=H)
    assert (await client.get("/pages/demo/", headers=H)).text == "<h1>v2</h1>"
    detail = (await client.post("/api/pages/demo/rollback", json={"version": 1}, headers=H)).json()
    assert detail["version"] == 1 and [v["n"] for v in detail["versions"]] == [2, 1]
    assert (await client.get("/pages/demo/", headers=H)).text == "<h1>live</h1>"
    files = (await client.get("/api/pages/demo/files?version=2", headers=H)).json()["files"]
    assert {f["path"] for f in files} == {"index.html", "app.js", "model.glb", "blob.qqzz", "sub/index.html"}
    assert (await client.delete("/api/pages/demo", headers=H)).json() == {"removed": "demo"}
    assert (await client.get("/api/pages/demo", headers=H)).status_code == 404
    assert (await client.get("/pages/demo/", headers=H)).status_code == 404


async def test_writes_refuse_a_path_out_of_the_page(site: Any) -> None:
    client, _ = site
    await _static_page(client)
    r = await client.put("/api/pages/demo/draft/..%2F..%2Fescape", content=b"x", headers=H)
    assert r.status_code == 400


async def test_storage_round_trip(site: Any) -> None:
    client, _ = site
    await _static_page(client)
    assert (await client.get("/api/pages/demo/storage/notes/a", headers=H)).status_code == 404
    assert (await client.put("/api/pages/demo/storage/notes/a", json={"value": {"text": "привет", "n": [1, 2]}}, headers=H)).status_code == 200
    await client.put("/api/pages/demo/storage/other", json={"value": 3}, headers=H)
    got = (await client.get("/api/pages/demo/storage/notes/a", headers=H)).json()
    assert got["value"] == {"text": "привет", "n": [1, 2]}
    assert [k["key"] for k in (await client.get("/api/pages/demo/storage?prefix=notes/", headers=H)).json()["keys"]] == ["notes/a"]
    assert (await client.delete("/api/pages/demo/storage/notes/a", headers=H)).status_code == 200
    assert (await client.delete("/api/pages/demo/storage/notes/a", headers=H)).status_code == 404
    assert (await client.get("/api/pages/nope/storage", headers=H)).status_code == 404
    # A removed page takes its storage with it: a page made again under the slug starts empty.
    await client.delete("/api/pages/demo", headers=H)
    await _static_page(client)
    assert (await client.get("/api/pages/demo/storage", headers=H)).json() == {"keys": []}


async def test_the_kit_is_served_without_the_sign_in(site: Any) -> None:
    client, _ = site
    css = await client.get("/pages/_kit/kit.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert (await client.get("/pages/_kit/daedalus.js")).headers["content-type"].startswith("text/javascript")
    assert (await client.get("/pages/_kit/other.js")).status_code == 404


async def test_a_service_page_proxies_the_agents_app(site: Any) -> None:
    client, _ = site
    with socket.socket() as probe:  # a port that was free a moment ago and that nothing listens on
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    for slug, target in (("nb", "notebook"), ("raw", closed)):
        body = {"slug": slug, "title": "Notebook", "kind": "service", "target": target}
        assert (await client.post("/api/pages", json=body, headers=H)).status_code == 200
        await client.post(f"/api/pages/{slug}/publish", headers=H)
    r = await client.get("/pages/nb/tree?x=1", headers={**H, "Cookie": f"{SESSION_COOKIE}=secret; other=kept"})
    assert r.status_code == 200
    assert r.text == "path=/tree?x=1 prefix=/pages/nb cookie=other=kept"  # the site's own cookie stays home
    assert "x-frame-options" not in r.headers
    assert r.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert r.headers["set-cookie"] == "app=1; Path=/pages/nb/"
    assert (await client.get("/pages/nb/go", headers=H, follow_redirects=False)).headers["location"] == "/pages/nb/landed"
    big = os.urandom(3 << 20)
    posted = await client.post("/pages/nb/upload", content=big, headers=H)
    assert posted.status_code == 201 and posted.text == f"got {len(big)}"
    # A port that nothing listens on is a clear refusal, not a hang.
    assert (await client.get("/pages/raw/", headers=H)).status_code == 502


async def test_a_link_page_frames_the_site(site: Any) -> None:
    client, _ = site
    await client.post("/api/pages", json={"slug": "docs", "title": "Docs", "kind": "link", "target": "https://example.org/a?b=1&c=2"}, headers=H)
    await client.post("/api/pages/docs/publish", headers=H)
    r = await client.get("/pages/docs/", headers=H)
    assert r.status_code == 200 and 'src="https://example.org/a?b=1&amp;c=2"' in r.text


# -- the WebSocket relay --------------------------------------------------------------------------


class MemoryKv:
    """The two calls the session secret makes, for a test client that runs the app on its own loop."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def kv_get(self, key: str) -> str | None:
        return self.values.get(key)

    async def kv_set(self, key: str, value: str) -> None:
        self.values[key] = value


def test_a_service_pages_websocket_is_relayed(settings: Settings, tmp_path: Path) -> None:
    from websockets.sync.server import serve

    def echo(connection: Any) -> None:
        connection.send(f"path={connection.request.path} prefix={connection.request.headers.get('x-forwarded-prefix')}")
        for message in connection:
            connection.send(message if isinstance(message, bytes) else f"echo:{message}")

    with serve(echo, "127.0.0.1", 0) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.socket.getsockname()[1]
        app = _app(settings, MemoryKv(), port)
        with TestClient(build_app(app, "tok")) as client:  # type: ignore[arg-type]
            assert client.post("/api/pages", json={"slug": "live", "title": "Live", "kind": "service", "target": port}, headers=H).status_code == 200
            client.post("/api/pages/live/publish", headers=H)
            cookie = client.post("/api/pages/_signin", headers=H).cookies[SESSION_COOKIE]
            with client.websocket_connect("/pages/live/socket?room=1", headers={"cookie": f"{SESSION_COOKIE}={cookie}"}) as ws:
                assert ws.receive_text() == "path=/socket?room=1 prefix=/pages/live"
                ws.send_text("hi")
                assert ws.receive_text() == "echo:hi"
                ws.send_bytes(b"\x00\x01")
                assert ws.receive_bytes() == b"\x00\x01"
            # No cookie, or a socket opened from another site: refused before anything is relayed.
            client.cookies.clear()
            for headers in ({}, {"cookie": f"{SESSION_COOKIE}={cookie}", "origin": "https://elsewhere.example"}):
                with pytest.raises(Exception):  # noqa: B017, PT011 — the test client raises its own disconnect type
                    with client.websocket_connect("/pages/live/socket", headers=headers) as ws:
                        ws.receive_text()
        server.shutdown()
