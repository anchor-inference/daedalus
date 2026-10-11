"""The agent's page tools against a real page store in a temporary folder, the page check's own server
driven by a fake browser that speaks to it over loopback, and the desk the tools reach it through."""

from __future__ import annotations

import base64
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import PagesConfig
from daedalus.extensions.api_page_check import CheckResult, CheckServer, Exchange, Shot, bind_free, drive
from daedalus.extensions.pages import PageDesk
from daedalus.host.filesystem import LocalFS
from daedalus.host.pages import PageError, PageStore, store_for
from daedalus.tools import pages as page_tools

TEMPLATES = Path(__file__).resolve().parents[2] / "skills" / "daedalus-pages" / "templates"
RANGE = (8100, 8119)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event_type: str, payload: dict[str, Any], **_: Any) -> None:
        self.events.append((event_type, payload))


class FakeDesk:
    """The desk's contract as the tools use it, over a real store."""

    def __init__(self, root: Path, rows: list[dict[str, Any]] | None = None, check: CheckResult | None = None) -> None:
        self.store = PageStore(root / "pages", PagesConfig())
        self.templates_dir = TEMPLATES
        self.rows = rows or []
        self.result = check
        self.checked: list[tuple[str, bool, str]] = []

    def services_range(self) -> tuple[int, int]:
        return RANGE

    async def service_rows(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return self.rows

    async def check(self, slug: str, *, draft: bool, session_id: str) -> CheckResult:
        self.checked.append((slug, draft, session_id))
        if self.result is None:
            raise PageError("this installation has no browser", 503)
        return self.result


class FakeServices:
    """The parts of a session's services the page tools touch: its folder, its walls, its files."""

    def __init__(self, workspace: Path, desk: Any) -> None:
        self.workspace_dir = workspace
        self.fs = LocalFS()
        self.max_tool_output_chars = 60_000
        self.extra = {"manager": SimpleNamespace(service_hooks={"pages": desk}), "vision": None}
        self.protected = workspace / "secret"

    def resolve(self, path: str | None, *, write: bool = False) -> Path:
        candidate = Path(path or ".")
        return candidate if candidate.is_absolute() else self.workspace_dir / candidate

    def is_protected(self, path: Path) -> bool:
        return path == self.protected or self.protected in path.parents


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "ws"
    folder.mkdir()
    return folder


@pytest.fixture
def desk(tmp_path: Path) -> FakeDesk:
    return FakeDesk(tmp_path)


@pytest.fixture
def context() -> Any:
    return SimpleNamespace(session_id="s1", metadata={"tool_call_id": "c1"})


@pytest.fixture(autouse=True)
def services(monkeypatch: pytest.MonkeyPatch, workspace: Path, desk: FakeDesk) -> FakeServices:
    made = FakeServices(workspace, desk)
    monkeypatch.setattr("daedalus.tools.pages.services_for", lambda _: made)
    monkeypatch.setattr("daedalus.tools._common.services_for", lambda _: made)
    return made


async def call(tool: Any, context: Any, **arguments: Any) -> Any:
    return await tool().invoke(context, arguments)


# -- creating -----------------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["blank", "dashboard", "split", "viewer", "embed-service", "form"])
async def test_every_template_scaffolds_a_phone_correct_kit_page(context: Any, desk: FakeDesk, template: str) -> None:
    result = await call(page_tools.page_create, context, slug=f"t-{template}", title={"en": "Orders & co", "ru": "Заказы"}, template=template, icon="board")
    assert not result.is_error, result.content
    files = [row["path"] for row in await desk.store.list_files(f"t-{template}")]
    assert "index.html" in files and "README.md" not in files
    text = desk.store.draft_file(f"t-{template}", "index.html").read_text("utf-8")
    assert "{{" not in text, "every placeholder is filled"
    assert "Orders &amp; co" in text, "the title is escaped for the markup it lands in"
    assert 'name="viewport" content="width=device-width' in text
    assert "/pages/_kit/kit.css" in text and "/pages/_kit/daedalus.js" in text
    assert "ru:" in text and "en:" in text, "the page's own words come in both languages"
    manifest = await desk.store.require(f"t-{template}")
    assert manifest["session_id"] == "s1" and manifest["version"] is None and manifest["icon"] == "board"


def test_the_shipped_templates_are_the_ones_the_tool_describes() -> None:
    described = page_tools.page_create().definition.description
    shipped = sorted(p.name for p in TEMPLATES.iterdir() if p.is_dir())
    assert shipped == ["blank", "dashboard", "embed-service", "form", "split", "viewer"]
    for name in shipped:
        assert name in described


async def test_an_unknown_template_creates_nothing(context: Any, desk: FakeDesk) -> None:
    result = await call(page_tools.page_create, context, slug="x", title="X", template="carousel")
    assert result.is_error and "blank" in result.content
    assert await desk.store.get("x") is None


async def test_a_service_page_points_into_the_services_range_or_at_a_service(context: Any, desk: FakeDesk) -> None:
    ok = await call(page_tools.page_create, context, slug="nb", title="Notebook", kind="service", target="8105")
    assert not ok.is_error, ok.content
    assert (await desk.store.require("nb"))["draft"]["target"] == 8105
    assert await desk.store.list_files("nb") == [], "a service page has no files to scaffold"
    for bad in (8765, 22, True, None, "rm -rf"):
        refused = await call(page_tools.page_create, context, slug="bad", title="Bad", kind="service", target=bad)
        assert refused.is_error, bad
    named = await call(page_tools.page_create, context, slug="jupyter", title="Jupyter", kind="service", target="notebook")
    assert not named.is_error and "No service named 'notebook'" in named.content
    desk.rows = [{"name": "grafana", "status": "running", "port": 8110}]
    known = await call(page_tools.page_create, context, slug="grafana", title="Grafana", kind="service", target="grafana")
    assert not known.is_error and "No service named" not in known.content


async def test_a_link_page_takes_an_http_address(context: Any, desk: FakeDesk) -> None:
    assert not (await call(page_tools.page_create, context, slug="docs", title="Docs", kind="link", target="https://example.org/docs")).is_error
    assert (await call(page_tools.page_create, context, slug="ftp", title="Ftp", kind="link", target="ftp://example.org")).is_error
    assert (await call(page_tools.page_create, context, slug="bad-kind", title="X", kind="widget")).is_error


# -- the draft's files --------------------------------------------------------------------------


async def test_write_read_list_and_delete_a_draft_file(context: Any, desk: FakeDesk) -> None:
    await call(page_tools.page_create, context, slug="p", title="P")
    written = await call(page_tools.page_write, context, slug="p", path="js/app.js", content="console.log('привет')")
    assert not written.is_error and "js/app.js" in written.content
    read = await call(page_tools.page_read, context, slug="p", path="js/app.js")
    assert "console.log('привет')" in read.content
    listed = await call(page_tools.page_files, context, slug="p")
    assert "js/app.js" in listed.content and "index.html" in listed.content
    assert not (await call(page_tools.page_delete, context, slug="p", path="js")).is_error
    assert "js/app.js" not in (await call(page_tools.page_files, context, slug="p")).content
    assert (await call(page_tools.page_delete, context, slug="p", path="js")).is_error


@pytest.mark.parametrize("bad", ["../x", "/etc/passwd", "a/../../b", "a\\b", ""])
async def test_a_path_out_of_the_page_is_refused_everywhere(context: Any, desk: FakeDesk, tmp_path: Path, bad: str) -> None:
    await call(page_tools.page_create, context, slug="p", title="P")
    assert (await call(page_tools.page_write, context, slug="p", path=bad, content="x")).is_error
    assert (await call(page_tools.page_read, context, slug="p", path=bad)).is_error
    assert (await call(page_tools.page_delete, context, slug="p", path=bad)).is_error
    assert not (tmp_path / "x").exists()


async def test_reading_a_binary_file_says_so(context: Any, desk: FakeDesk) -> None:
    await call(page_tools.page_create, context, slug="p", title="P")
    await desk.store.write_file("p", "model.glb", b"glTF\x00\x01\x02")
    assert "binary file" in (await call(page_tools.page_read, context, slug="p", path="model.glb")).content


async def test_a_build_folder_is_imported_with_its_nested_files_and_without_links(context: Any, desk: FakeDesk, workspace: Path) -> None:
    await call(page_tools.page_create, context, slug="app", title="App")
    dist = workspace / "dist"
    (dist / "assets" / "deep").mkdir(parents=True)
    (dist / "index.html").write_text("<p>built</p>")
    (dist / "assets" / "app-1a2b.js").write_text("1")
    (dist / "assets" / "deep" / "model.glb").write_bytes(os.urandom(3 << 20))
    (workspace / "outside.txt").write_text("not mine")
    os.symlink(workspace / "outside.txt", dist / "assets" / "link.txt")
    result = await call(page_tools.page_import, context, slug="app", source_path="dist", replace=True)
    assert not result.is_error, result.content
    assert "Imported 3 files" in result.content and "1 link was left out" in result.content
    paths = sorted(row["path"] for row in await desk.store.list_files("app"))
    assert paths == ["assets/app-1a2b.js", "assets/deep/model.glb", "index.html"], "the template's own index.html is replaced too"
    assert desk.store.draft_file("app", "assets/deep/model.glb").stat().st_size == 3 << 20
    # The next build's hashed names replace the last build's rather than piling up beside them.
    (dist / "assets" / "app-1a2b.js").unlink()
    (dist / "assets" / "app-9z8y.js").write_text("2")
    await call(page_tools.page_import, context, slug="app", source_path=str(dist), replace=True)
    assert sorted(row["path"] for row in await desk.store.list_files("app")) == ["assets/app-9z8y.js", "assets/deep/model.glb", "index.html"]


async def test_an_import_lands_under_dest_and_merges_without_replace(context: Any, desk: FakeDesk, workspace: Path) -> None:
    await call(page_tools.page_create, context, slug="p", title="P")
    (workspace / "data").mkdir()
    (workspace / "data" / "a.csv").write_text("a,b")
    (workspace / "logo.svg").write_text("<svg/>")
    assert not (await call(page_tools.page_import, context, slug="p", source_path="data", dest="static/data")).is_error
    assert not (await call(page_tools.page_import, context, slug="p", source_path="logo.svg", dest="static")).is_error
    paths = sorted(row["path"] for row in await desk.store.list_files("p"))
    assert paths == ["index.html", "static/data/a.csv", "static/logo.svg"]
    assert (await call(page_tools.page_import, context, slug="p", source_path="data", dest="../up")).is_error
    assert (await call(page_tools.page_import, context, slug="p", source_path="nowhere")).is_error


async def test_an_import_refuses_a_protected_file_before_writing_anything(context: Any, desk: FakeDesk, workspace: Path, services: FakeServices) -> None:
    await call(page_tools.page_create, context, slug="p", title="P")
    services.protected.mkdir()
    (services.protected / "key.txt").write_text("sk-123")
    (workspace / "a.txt").write_text("fine")
    refused = await call(page_tools.page_import, context, slug="p", source_path=".", dest="all")
    assert refused.is_error and "protected" in refused.content
    assert [row["path"] for row in await desk.store.list_files("p")] == ["index.html"]
    assert (await call(page_tools.page_import, context, slug="p", source_path="secret/key.txt")).is_error


# -- publishing ---------------------------------------------------------------------------------


async def test_publish_rollback_list_and_the_rail_step(context: Any, desk: FakeDesk) -> None:
    await call(page_tools.page_create, context, slug="orders", title={"en": "Orders", "ru": "Заказы"})
    published = await call(page_tools.page_publish, context, slug="orders", note="first")
    assert not published.is_error and "version 1" in published.content
    assert "SettingsChange" in published.content and "page:orders" in published.content and "PageShow" in published.content
    await call(page_tools.page_write, context, slug="orders", path="index.html", content="<p>two</p>")
    assert "the draft has changed since" in (await call(page_tools.page_list, context)).content
    await call(page_tools.page_publish, context, slug="orders", note="second")
    listed = (await call(page_tools.page_list, context)).content
    assert 'orders "Orders" · static · live v2' in listed and "(versions 1, 2)" in listed and "/app/p/orders" in listed
    assert not (await call(page_tools.page_rollback, context, slug="orders", version=1)).is_error
    assert (await desk.store.require("orders"))["version"] == 1
    assert (await call(page_tools.page_rollback, context, slug="orders", version=9)).is_error
    assert desk.store.version_file("orders", 2, "index.html").read_text() == "<p>two</p>"
    assert "<p>two</p>" in (await call(page_tools.page_read, context, slug="orders", path="index.html", version=2)).content


async def test_a_published_page_is_removed_only_with_the_operators_yes(context: Any, desk: FakeDesk) -> None:
    await call(page_tools.page_create, context, slug="draft-only", title="D")
    assert not (await call(page_tools.page_remove, context, slug="draft-only")).is_error
    await call(page_tools.page_create, context, slug="live", title="L")
    await call(page_tools.page_publish, context, slug="live")
    refused = await call(page_tools.page_remove, context, slug="live")
    assert refused.is_error and "AskUser" in refused.content and "confirm=true" in refused.content
    assert await desk.store.get("live") is not None
    assert not (await call(page_tools.page_remove, context, slug="live", confirm=True)).is_error
    assert await desk.store.get("live") is None
    assert "no custom pages" in (await call(page_tools.page_list, context)).content


async def test_show_answers_where_the_card_opens(context: Any, desk: FakeDesk) -> None:
    await call(page_tools.page_create, context, slug="orders", title={"en": "Orders", "ru": "Заказы"})
    shown = await call(page_tools.page_show, context, slug="orders")
    assert not shown.is_error and "/app/p/orders" in shown.content and "draft" in shown.content
    assert shown.metadata["path"] == "/app/p/orders"
    drafted = await call(page_tools.page_show, context, slug="orders", draft=True)
    assert drafted.metadata["path"] == "/app/p/orders?draft=1"
    assert (await call(page_tools.page_show, context, slug="nope")).is_error


async def test_without_the_desk_every_tool_says_pages_are_unavailable(context: Any, services: FakeServices) -> None:
    services.extra["manager"] = SimpleNamespace(service_hooks={})
    for tool in page_tools.TOOLS:
        made = tool()
        # Every required argument given a plain value: the refusal has to come before any of them is read.
        arguments = {name: ("x" if name != "version" else 1) for name in made.definition.parameters.required}
        result = await made.invoke(context, arguments)
        assert result.is_error and "not available" in result.content, made.name


# -- the check, as the tool reports it ----------------------------------------------------------


def _report(*, handshake: bool = True, scroll: int = 390, viewport_meta: bool = True) -> dict[str, Any]:
    return {
        "handshake": {"version": "1.0.0", "at": 120} if handshake else None, "loaded": True, "load_ms": 300,
        "measure": {"reachable": True, "title": "Orders", "width": 390, "scroll_width": scroll, "viewport_meta": viewport_meta,
                    "bridge": "1.0.0" if handshake else None, "kit": True, "elements": 40, "text_chars": 200,
                    "wide": [{"el": "table.prices", "right": 620, "width": 600}] if scroll > 390 else [], "small": 2, "small_examples": ["button#x 18x18"]},
    }


def _result(**report: Any) -> CheckResult:
    result = CheckResult(slug="orders", kind="static", draft=True)
    for view, width, height in (("phone", 390, 844), ("desktop", 1440, 900)):
        for scheme in ("dark", "light"):
            result.runs.append({"name": f"{view}-{scheme}", "view": view, "scheme": scheme, "width": width, "height": height, "report": _report(**report), "navigation_error": None})
            result.shots.append(Shot(f"{view}-{scheme}", view, scheme, width, height, b"\xff\xd8jpeg", "image/jpeg"))
    result.console = [{"level": "error", "text": "Uncaught TypeError: x is undefined", "url": "http://127.0.0.1:8101/pages/orders/app.js", "line": 3, "count": 1}] * 2
    result.failed = [{"method": "GET", "url": "https://cdn.example.org/lib.js", "failed": "net::ERR_NAME_NOT_RESOLVED"}]
    result.exchanges = [Exchange("GET", "/pages/orders/missing.png", 404), Exchange("GET", "/api/pages/orders/storage/note", 401), Exchange("GET", "/pages/orders/app.js", 200)]
    return result


async def test_check_reports_what_is_broken_and_what_the_vision_model_saw(context: Any, desk: FakeDesk, workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await call(page_tools.page_create, context, slug="orders", title="Orders")
    desk.result = _result(scroll=620)
    questions: list[str] = []

    async def fake_look(vision: Any, manager: Any, data: bytes, mime: str, question: str, **_: Any) -> tuple[str, str]:
        questions.append(question)
        return "A list of orders; the price table is cut off on the right.", "vision-model"

    monkeypatch.setattr(page_tools, "vision_look", fake_look)
    result = await call(page_tools.page_check, context, slug="orders")
    assert not result.is_error, result.content
    text = result.content
    assert desk.checked == [("orders", True, "s1")]
    assert "advice" in text
    assert "shook hands (daedalus.js 1.0.0)" in text
    assert "OVERFLOWS sideways" in text and "table.prices" in text
    assert "Small tap targets" in text
    assert "Uncaught TypeError: x is undefined" in text and "×2" in text
    assert "/pages/orders/missing.png → 404" in text
    assert "answered 401 because the check carries no sign-in" in text
    assert "ERR_NAME_NOT_RESOLVED" in text
    assert text.count("the price table is cut off") == 4 and len(questions) == 4
    assert any("390x844 (phone, light colour scheme)" in q for q in questions)
    for name in ("phone-dark", "phone-light", "desktop-dark", "desktop-light"):
        assert (workspace / ".page-check" / "orders" / f"{name}.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert result.metadata["screenshots"][0] == ".page-check/orders/phone-dark.jpg"


async def test_check_says_when_the_page_fits_and_without_the_bridge(context: Any, desk: FakeDesk) -> None:
    await call(page_tools.page_create, context, slug="orders", title="Orders")
    desk.result = _result(handshake=False, viewport_meta=False)
    text = (await call(page_tools.page_check, context, slug="orders", look=False)).content
    assert "Phone width: fits" in text
    assert "Bridge: not used" in text
    assert 'No <meta name="viewport"' in text
    assert "what a vision model saw" not in text


async def test_check_without_a_browser_is_an_answer_not_a_crash(context: Any, desk: FakeDesk) -> None:
    await call(page_tools.page_create, context, slug="orders", title="Orders")
    result = await call(page_tools.page_check, context, slug="orders")
    assert result.is_error and "no browser" in result.content


# -- the check's own server, driven over loopback -----------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class LoopbackBrowser:
    """The browser service as the check drives it, played by an HTTP client: it opens the check's
    address, fetches what a page would, and posts the report the harness's script would post."""

    def __init__(self) -> None:
        self.client = httpx.AsyncClient()
        self.calls: list[str] = []
        self.sizes: list[tuple[int, int]] = []
        self.closed = False
        self.harness = ""
        self.page_body = ""

    async def open(self, owner: Any, *, fresh: bool, actor: str, viewport: dict[str, int]) -> dict[str, Any]:
        assert fresh, "the check never uses the agent's own profile"
        return {"group": {"id": "s-s1-x"}, "tab": {"id": "t1"}, "created": True}

    def origin(self, *, actor: str) -> dict[str, Any]:
        return {"actor": "agent"}

    async def resize(self, group: str, w: int, h: int) -> dict[str, int]:
        self.sizes.append((w, h))
        return {"w": w, "h": h}

    async def close_group(self, group: str, *, actor: str, reason: str = "closed") -> None:
        self.closed = True
        await self.client.aclose()

    async def call(self, group: str, method: str, params: dict[str, Any] | None = None, *, what: str, timeout: float = 10.0) -> Any:
        self.calls.append(method)
        params = params or {}
        if method == "page.navigate":
            url = params["url"]
            base = url.split("/?", 1)[0]
            answer = await self.client.get(url)
            assert answer.status_code == 200
            self.harness = answer.text
            run = int(url.rsplit("run=", 1)[1].split("&", 1)[0])
            page = await self.client.get(f"{base}/pages/orders/")
            self.page_body = page.text
            await self.client.get(f"{base}/pages/orders/missing.js")
            await self.client.get(f"{base}/api/pages/orders/storage/note")
            await self.client.get(f"{base}/pages/_kit/kit.css")
            scheme = "light" if "scheme=light" in url else "dark"
            posted = await self.client.post(f"{base}/__report?run={run}", content=json.dumps({"run": run, "scheme": scheme, "handshake": {"version": "1.0.0"}, "measure": {"width": 390, "scroll_width": 390}}))
            assert posted.status_code == 204
            return {"url": url, "status": 200}
        if method == "page.screenshot":
            return {"format": "jpeg", "width": 390, "height": 844, "data_b64": base64.b64encode(b"\xff\xd8shot").decode()}
        if method == "page.logs":
            return {"entries": [{"level": "error", "text": "boom"}]}
        if method == "page.network":
            return {"requests": []}
        raise AssertionError(method)


async def test_the_check_serves_the_draft_without_credentials_and_drives_four_looks(tmp_path: Path) -> None:
    store = PageStore(tmp_path / "pages")
    await store.create("orders", title="Orders")
    await store.write_file("orders", "index.html", b"<!DOCTYPE html><p>draft body</p>")
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "kit.css").write_text(":root{--bg:#000}")
    manifest = await store.require("orders")

    async def no_service(manifest: dict[str, Any], served: Any) -> int:
        raise PageError("not a service page")

    server = CheckServer(store, manifest, draft=True, kit_dir=kit, resolve_port=no_service)
    port = _free_port()
    await server.start(bind_free(port, port, "127.0.0.1", set()))
    browser = LoopbackBrowser()
    result = CheckResult(slug="orders", kind="static", draft=True)
    try:
        async with httpx.AsyncClient() as stranger:
            # Without the token the address is nothing; without the cookie the page is nothing.
            assert (await stranger.get(f"http://127.0.0.1:{port}/")).status_code == 404
            assert (await stranger.get(f"http://127.0.0.1:{port}/pages/orders/")).status_code == 404
        await drive(browser, SimpleNamespace(), "agent:s1", server, result)
    finally:
        await server.stop()
    assert browser.page_body == "<!DOCTYPE html><p>draft body</p>"
    assert "daedalus: 1" in browser.harness and '"src": "/pages/orders/"' in browser.harness, "the frame plays the app's side of the bridge"
    assert browser.sizes == [(390, 844), (1440, 900)]
    assert [r["name"] for r in result.runs] == ["phone-dark", "phone-light", "desktop-dark", "desktop-light"]
    assert all(r["report"] and r["report"]["handshake"] for r in result.runs)
    assert [r["report"]["scheme"] for r in result.runs] == ["dark", "light", "dark", "light"]
    assert len(result.shots) == 4 and result.shots[0].data == b"\xff\xd8shot"
    assert result.console == [{"level": "error", "text": "boom"}] and result.failed == []
    statuses = {(e.path, e.status) for e in server.exchanges}
    assert ("/pages/orders/missing.js", 404) in statuses
    assert ("/api/pages/orders/storage/note", 401) in statuses
    assert ("/pages/_kit/kit.css", 200) in statuses
    assert browser.closed, "the throwaway browser is closed after the check"
    with pytest.raises(httpx.ConnectError):
        async with httpx.AsyncClient() as late:
            await late.get(f"http://127.0.0.1:{port}/")


async def test_a_page_with_nothing_published_is_checked_only_as_a_draft(tmp_path: Path) -> None:
    store = PageStore(tmp_path / "pages")
    manifest = await store.create("orders", title="Orders")

    async def port(manifest: dict[str, Any], served: Any) -> int:
        return 0

    with pytest.raises(PageError):
        CheckServer(store, manifest, draft=False, kit_dir=tmp_path, resolve_port=port)


def test_a_busy_range_is_a_refusal_not_an_exit() -> None:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    taken = int(holder.getsockname()[1])
    try:
        with pytest.raises(PageError):
            bind_free(taken, taken, "127.0.0.1", set())
        with pytest.raises(PageError):
            bind_free(taken + 1, taken + 1, "127.0.0.1", {taken + 1})
    finally:
        holder.close()


# -- the desk -----------------------------------------------------------------------------------


def _app(tmp_path: Path, extensions: dict[str, Any] | None = None) -> Any:
    settings = SimpleNamespace(services_port_range="8100-8119", skills_dir=tmp_path / "skills", bot_repo_dir=tmp_path / "repo", state_dir=tmp_path / "state")
    return SimpleNamespace(settings=settings, config=SimpleNamespace(pages=PagesConfig()), db=None, extensions=extensions or {},
                           manager=SimpleNamespace(bus=FakeBus(), service_hooks={}, live_state=lambda session_id: None))


async def test_the_desk_shares_the_apps_store_and_keeps_checks_inside_the_services_range(tmp_path: Path) -> None:
    class Services:
        async def rows(self, session_id: str | None = None) -> list[dict[str, Any]]:
            return [{"name": "nb", "status": "running", "pid": os.getpid(), "port": 8107}, {"name": "api", "status": "running", "pid": os.getpid(), "port": 8765}]

    app = _app(tmp_path, {"services": Services()})
    made = PageDesk(app, check_host="127.0.0.1")
    assert made.store is store_for(app), "the tools and the API write through one store and one set of locks"
    assert made.templates_dir == tmp_path / "skills" / "daedalus-pages" / "templates"
    assert made.services_range() == (8100, 8119)
    served = SimpleNamespace(target="nb")
    assert await made.service_port({"session_id": "s1"}, served) == 8107
    for target in (8765, "api"):
        with pytest.raises(PageError):
            await made.service_port({"session_id": "s1"}, SimpleNamespace(target=target))
    with pytest.raises(PageError):
        await made.service_port({"session_id": "s1"}, SimpleNamespace(target="stopped"))
    await made.store.create("orders", title="Orders")
    with pytest.raises(PageError) as refused:
        await made.check("orders", draft=True, session_id="s1")
    assert refused.value.status == 503 and "no browser" in str(refused.value)
