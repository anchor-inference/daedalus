"""A live link, end to end: a real host, a scripted model, a real browser, and a visitor with no credential.

The operator makes the link in the share dialog. A fresh browser context, which holds nothing of the
operator's, opens it on the services door, gives a name, sends a message and watches the answer stream
in; the Files tab shows what the agent wrote. With the link's token in hand the API refuses another
session, the settings, the secrets and the session list. The operator's header counts the visitor. Then
the link is revoked and the open page loses the conversation at once.

    CHROMIUM=... .venv/bin/python tests/browser/check_session_share.py
    LANG_UI=ru SHOTS=/some/dir .venv/bin/python tests/browser/check_session_share.py

``SHOTS`` names a folder for the screenshots (none are written without it).
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from queue import Queue
from typing import Any

import httpx
from playwright.sync_api import expect, sync_playwright
from protocore.contracts.llm import ProviderDelta, ProviderDeltaKind

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from daedalus.app import Application  # noqa: E402
from daedalus.config import ModelConfig, ModelPresetConfig, ProviderConfig, RuntimeConfig, Settings  # noqa: E402
from daedalus.providers import modelsdev  # noqa: E402
from tests.unit.test_session_runner import ScriptedProvider  # noqa: E402

CHROMIUM = os.environ.get("CHROMIUM") or None
LANG = os.environ.get("LANG_UI", "en")
SHOTS = Path(os.environ["SHOTS"]) if os.environ.get("SHOTS") else None
ANSWER = ["The menu ", "is written ", "to menu.md: ", "bread three euros, ", "coffee two."]
MEDIA_REF = re.compile(r"!\[[^\]\n]{0,500}\]\(daedalus-media:[0-9a-f-]{36}\)")


def png(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    """A real picture of one colour, small enough to read from the page's own bytes and big enough to be seen."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(colour) * width
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")


class SlowProvider(ScriptedProvider):
    """The agent's turns from a script, its answer in pieces with pauses, so the page can be seen streaming.
    A request without the agent's tools (a title, a suggestion) gets a short text and leaves the script alone."""

    def __init__(self) -> None:
        super().__init__([])
        self.started = threading.Event()

    async def stream_with_tools(self, request: Any) -> AsyncIterator[ProviderDelta]:  # type: ignore[override]
        if not any(tool.name == "Write" for tool in request.tools):
            yield ProviderDelta(kind=ProviderDeltaKind.text, content="Menu")
            yield ProviderDelta(kind=ProviderDeltaKind.usage, usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0})
            yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")
            return
        if self.script and "tool" in self.script[0]:
            async for delta in super().stream_with_tools(request):
                yield delta
            return
        if self.script and self.script[0].get("echo_media"):
            # The answer that places the file AttachMedia prepared: the line it returned is the only way a
            # picture reaches the answer, and its id is made by the host, so it is read back from the request.
            self.requests.append(request)
            self.script.pop(0)
            found = MEDIA_REF.search(repr(request.messages))
            yield ProviderDelta(kind=ProviderDeltaKind.text, content=f"Here it is: {found.group(0) if found else 'no media line came back'}")
            yield ProviderDelta(kind=ProviderDeltaKind.usage, usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0})
            yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")
            return
        self.requests.append(request)
        if self.script:
            self.script.pop(0)
        self.started.set()
        for piece in ANSWER:
            yield ProviderDelta(kind=ProviderDeltaKind.text, content=piece)
            await asyncio.sleep(0.6)
        yield ProviderDelta(kind=ProviderDeltaKind.usage, usage={"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0})
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")


def free_port() -> int:
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        # The agent hands 8100–8119 to its own previews; never photograph someone else's.
        if not 8100 <= port <= 8119:
            return port


class Host:
    """A whole host in a thread: its own state, an API port and a services door port, a scripted model."""

    def __init__(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="session-share-browser-")
        self.port = free_port()
        self.door = free_port()
        self.ready: Queue[tuple[str, str] | Exception] = Queue(maxsize=1)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.provider = SlowProvider()

    def _run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 — handed to the waiting thread
            if self.ready.empty():
                self.ready.put(exc)
            else:
                raise

    async def _serve(self) -> None:
        root = Path(self.scratch.name)
        settings = Settings(_env_file=None, state_dir=root / "state", workspaces_dir=root / "workspaces", bot_repo_dir=ROOT,  # type: ignore[call-arg]
                            core_repo_dir=ROOT.parent / "protocore-exp", owner_user_id=1, telegram_bot_token="", native=True,
                            api_port=self.port, services_door_port=self.door, api_host="127.0.0.1", services_public_host="localhost")
        config = RuntimeConfig(
            seeded=["claude-subscription", "openai-anthropic-keys", "more-provider-endpoints"],
            providers={"fake": ProviderConfig(kind="openai_compat", base_url="http://127.0.0.1:1/v1",
                                              pricing={"fake-model": {"input": 0.1, "output": 0.1, "cache_hit": 0.1, "input_limit": 128000,
                                                                      "limit_source": "scripted provider contract"}})},
            # A model that thinks, so the link can give the visitor efforts of the conversation's own model.
            presets={"fake": ModelPresetConfig(provider="fake", model="fake-model", thinking=True, reasoning_effort="high", context_window=32000, max_output_tokens=2048)},
            model=ModelConfig(preset="fake"),
        )
        config.save(settings.config_path)

        async def no_catalog() -> dict[str, Any]:
            return {}

        modelsdev.fetch_catalog = no_catalog  # type: ignore[assignment]
        app = Application(settings)
        try:
            await app.start()
            assert app.manager is not None
            old = app.manager.providers.get("fake")
            self.provider.endpoint = old.endpoint  # type: ignore[misc]
            app.manager.providers._providers["fake"] = self.provider  # type: ignore[assignment]
            self.ready.put((f"http://127.0.0.1:{self.port}", str(app.extensions["api_token"])))
            await asyncio.to_thread(self.stop.wait)
            await old.aclose()
        finally:
            await app.shutdown()

    def __enter__(self) -> tuple[str, str]:
        self.thread.start()
        reply = self.ready.get(timeout=90)
        if isinstance(reply, Exception):
            raise reply
        base, token = reply
        for _ in range(200):
            try:
                with urllib.request.urlopen(f"{base}/app/", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("the host did not serve the app")
        return base, token

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join(timeout=60)
        self.scratch.cleanup()


def word(en: str, ru: str) -> str:
    return ru if LANG == "ru" else en


def shot(page: Any, name: str) -> None:
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"session-share-{name}-{LANG}.png"))


def attached_files(browser: Any, host: Host, base: str, token: str, api: Any) -> None:
    """A visitor sees what the conversation carries: the files and pictures the operator attached to a
    message, the file the agent sent, and the picture it placed in its answer. Each is loaded by the
    visitor's own page, under the link's prefix, and counted as shown only when the browser decoded it."""
    gallery = api("POST", "/api/sessions", {"title": word("Gallery", "Галерея")})["id"]
    headers = {"x-daedalus-token": token}
    put = httpx.post(f"{base}/api/sessions/{gallery}/files/upload", headers=headers, timeout=30,
                     files=[("files", ("sent-chart.png", png(48, 32, (200, 60, 60)), "image/png")),
                            ("files", ("sent-report.csv", b"item,price\nbread,3\ncoffee,2\n", "text/csv"))])
    assert put.status_code == 200, put.text
    script = [
        {"tool": "SendFile", "args": {"path": "sent-report.csv"}},
        {"tool": "SendFile", "args": {"path": "sent-chart.png"}},
        {"tool": "AttachMedia", "args": {"items": [{"path": "sent-chart.png", "alt": "chart"}]}},
        {"echo_media": True},
    ]
    host.provider.script = script
    sent = httpx.post(f"{base}/api/sessions/{gallery}/upload", headers=headers, timeout=30, data={"text": "Here are my pictures"},
                      files=[("files", ("photo.png", png(64, 48, (30, 90, 200)), "image/png")),
                             ("files", ("notes.txt", b"call the baker at nine\n", "text/plain"))])
    assert sent.status_code == 200, sent.text
    for _ in range(300):
        time.sleep(0.2)
        if not host.provider.script and api("GET", f"/api/sessions/{gallery}")["status"] == "idle":
            break
    else:
        raise AssertionError("the gallery's run did not finish")
    link = api("POST", f"/api/sessions/{gallery}/live-shares", {})["url"]

    guest = browser.new_context(viewport={"width": 1440, "height": 900})
    page = guest.new_page()
    refused: list[str] = []
    page.on("response", lambda r: refused.append(f"{r.request.method} {r.url.split('/v/')[-1].split('/', 1)[-1]}") if r.status >= 400 and "/v/" in r.url else None)
    page.goto(f"{link}?lang={LANG}")
    expect(page.locator(".chat-title")).to_have_text(word("Gallery", "Галерея"), timeout=15000)

    def decoded(selector: str) -> list[int]:
        return page.eval_on_selector_all(selector, "els => els.map(e => e.complete ? e.naturalWidth : 0)")

    # The picture the operator attached: a thumbnail above their message.
    expect(page.locator(".msg-thumb.ready img")).to_have_count(1, timeout=15000)
    page.wait_for_function("() => [...document.querySelectorAll('.msg-thumb.ready img')].every(e => e.complete && e.naturalWidth > 0)", timeout=10000)
    assert decoded(".msg-thumb.ready img") == [64], decoded(".msg-thumb.ready img")
    # The file they attached: a card that opens the file in the panel with its words in it.
    card = page.locator(".msg-file", has_text="notes.txt")
    expect(card).to_have_count(1)
    card.click()
    expect(page.locator(".panel")).to_contain_text("call the baker at nine", timeout=10000)
    # The file the agent sent: a card whose download link is under the link's prefix and answers.
    sent_card = page.locator(".artifact[data-path='sent-report.csv']")
    expect(sent_card).to_have_count(1, timeout=10000)
    # The picture the agent placed in its answer: drawn, not a retry button.
    expect(page.locator(".inline-media, .inline-media-error")).to_have_count(1, timeout=15000)
    assert page.locator(".inline-media-error").count() == 0, f"the placed picture shows a retry button; routes the door refused: {sorted(set(refused))}"
    expect(page.locator(".inline-media img")).to_have_count(1)
    page.wait_for_function("() => [...document.querySelectorAll('.inline-media img')].every(e => e.complete && e.naturalWidth > 0)", timeout=10000)
    assert decoded(".inline-media img") == [48], decoded(".inline-media img")
    # Every download link on the page, as the browser would follow it, is under the link and answers.
    hrefs = page.eval_on_selector_all(".artifact a[href], .inline-file a[href]", "els => els.map(e => e.href)")
    assert hrefs, "no download link was drawn"
    for href in hrefs:
        assert f"/v/{link.split('/v/')[1].rstrip('/')}/api/" in href, href
        got = page.evaluate("href => fetch(href, {credentials: 'omit'}).then(r => r.status)", href)
        assert got == 200, (href, got)
    # The Attachments tab lists all of it, and each row's address answers for the visitor too.
    open_attachments(page)
    rows = page.locator("[data-tool-page=attachments] [data-attachment] a.tool-row-main")
    expect(rows.first).to_be_visible(timeout=10000)
    assert rows.count() >= 4, rows.count()
    for href in rows.evaluate_all("els => els.map(e => e.href)"):
        assert "/v/" in href, f"an attachment row points outside the link: {href}"
        got = page.evaluate("href => fetch(href, {credentials: 'omit'}).then(r => r.status)", href)
        assert got == 200, (href, got)
    unexpected = sorted({entry for entry in refused if not entry.startswith("POST api/presence")})
    assert not unexpected, f"the visitor's page asked for routes the door refused: {unexpected}"
    guest.close()


def open_attachments(page: Any) -> None:
    """The Attachments tab of the visitor's panel, through the menu a visitor has."""
    page.get_by_role("button", name=word("Session actions", "Действия с сессией")).click()
    page.get_by_role("menuitem", name=word("Project files", "Файлы проекта")).click()
    panel = page.locator(".panel")
    expect(panel).to_be_visible()
    panel.locator(".panel-add").click()
    page.locator("[data-tool=attachments]").click()


def main() -> None:
    host = Host()
    with host as (base, token), sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        operator = playwright.request.new_context(base_url=base, extra_http_headers={"x-daedalus-token": token})

        def api(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            response = operator.fetch(path, method=method, data=body)
            assert response.ok, f"{method} {path}: {response.status} {response.text()}"
            return response.json()

        mine = api("POST", "/api/sessions", {"title": word("Bakery menu", "Меню пекарни")})["id"]
        other = api("POST", "/api/sessions", {"title": "Private"})["id"]
        host.provider.script = [
            {"tool": "Write", "args": {"path": "menu.md", "content": "# Menu\n\nBread 3 EUR\nCoffee 2 EUR\n"}},
            {"text": "streamed"},
        ]

        # 1. The operator makes the link in the share dialog.
        desk = browser.new_context(viewport={"width": 1440, "height": 900})
        own = desk.new_page()
        own.goto(f"{base}/app/agents/{mine}?token={token}&lang={LANG}")
        own.get_by_role("button", name=word("Session actions", "Действия с сессией")).click()
        own.get_by_role("menuitem", name=re.compile(word("^Share", "^Поделиться"))).click()
        sheet = own.locator(".sheet")
        expect(sheet.locator(".live-warning")).to_be_visible()
        expect(sheet.locator(".live-reach")).to_have_attribute("data-reach", "machine")
        # The dialog's button opens the link's own window; its decision makes the link (no password, every
        # role at the session's value but the conversation's, whose own model is given Low beside its High),
        # and the window then shows the link beside its settings.
        sheet.get_by_role("button", name=word("Create live link", "Создать живую ссылку")).click()
        window = own.locator("[data-live-window]")
        main_role = window.locator(".live-role[data-role='main']")
        main_role.get_by_role("radio", name=word("From a set", "Из набора")).click(timeout=15000)
        own_row = main_role.locator(".live-allowed-row[data-preset='fake']")
        expect(own_row.locator(".live-chipbtn.own")).to_have_text(word("High", "Высокое"))
        own_row.locator(".live-chipbtn", has_text=word("Low", "Низкое")).click()
        shot(own, "window-efforts")
        window.locator(".live-save").click(timeout=15000)
        expect(window.locator(".live-win-row.on")).to_have_count(1, timeout=10000)
        own.keyboard.press("Escape")
        field = own.locator(".live-links .live-link input.field")
        expect(field).to_be_visible(timeout=10000)
        link = field.input_value()
        assert link.startswith(f"http://localhost:{host.door}/v/") and link.endswith("/"), link
        shot(own, "dialog")
        own.keyboard.press("Escape")
        share_token = link.split("/v/")[1].rstrip("/")
        kept = api("GET", f"/api/sessions/{mine}/live-shares")["links"][0]["policy"]["roles"]["main"]
        assert kept == {"preset": "fake", "effort": "high", "account": "", "allowed": [{"preset": "fake", "accounts": [], "efforts": ["high", "low"]}]}, kept

        # 2. A fresh context, with nothing of the operator's, opens the link.
        guest = browser.new_context(viewport={"width": 1440, "height": 900})
        assert not guest.cookies()
        visit = guest.new_page()
        refused: list[str] = []
        visit.on("response", lambda r: refused.append(f"{r.request.method} {r.url.split('/v/')[-1].split('/', 1)[-1]}") if r.status == 404 and "/v/" in r.url else None)
        # The operator puts a password on the link first: the page asks for it before it shows anything,
        # a wrong one is refused, and the right one opens the conversation. Everything after this point,
        # the stream and the downloads included, rides on the pass the browser was given.
        made = api("GET", f"/api/sessions/{mine}/live-shares")["links"][0]["id"]
        api("PATCH", f"/api/sessions/{mine}/live-shares/{made}", {"password": "four small words"})
        visit.goto(f"{link}?lang={LANG}")
        lock = visit.locator("[data-unlock]")
        expect(lock).to_be_visible(timeout=15000)
        expect(visit.locator(".visit-bar")).to_have_count(0)
        lock.locator("input").fill("not the password")
        lock.locator("button[type=submit]").click()
        expect(lock.locator("[role=alert]")).to_be_visible(timeout=10000)
        lock.locator("input").fill("four small words")
        lock.locator("button[type=submit]").click()
        expect(visit.locator(".visit-bar")).to_be_visible(timeout=15000)
        expect(visit.locator(".chat-title")).to_have_text(word("Bakery menu", "Меню пекарни"), timeout=15000)
        visit.locator(".visit-name-btn").click()
        visit.locator(".visit-name input").fill("Ann")
        visit.locator(".visit-name button[type=submit]").click()
        expect(visit.locator(".visit-name-btn")).to_contain_text("Ann")

        # 3. A message from the visitor, and the answer streaming in.
        visit.locator(".composer .composer-field").fill(word("Write the menu to a file.", "Запиши меню в файл."))
        visit.locator(".composer .roundbtn.primary").click()
        assert host.provider.started.wait(30), "the scripted model was never asked for the answer"
        partial = visit.locator(".answer").last
        expect(partial).to_contain_text("The menu", timeout=10000)
        early = partial.inner_text()
        assert "coffee two." not in early, f"the answer arrived whole, not streamed: {early!r}"
        expect(visit.locator(".answer").last).to_contain_text("coffee two.", timeout=20000)
        mark = visit.locator(".msg-visitor").last
        expect(mark).to_have_text(word("Guest · Ann", "Гость · Ann"))
        assert host.provider.requests[-1].extra.get("reasoning_effort") == "high", host.provider.requests[-1].extra

        # 3b. The visitor's effort control offers exactly what the link gives the conversation's own model,
        # the pick is kept, shown, and the next turn runs on it; the operator's own effort is not touched.
        # The first turn ends before the second is sent: the answer's last words arrive before the run has
        # finished, and a message sent into a run that is ending joins that run instead of starting its own.
        for _ in range(300):
            if api("GET", f"/api/sessions/{mine}")["status"] == "idle":
                break
            time.sleep(0.1)
        else:
            raise AssertionError("the visitor's first turn did not end")
        visit.locator(".composer button.model-select").click()
        segment = visit.locator(".model-effort-seg")
        expect(segment).to_be_visible(timeout=10000)
        offered = [text.strip() for text in segment.locator("[role=radio]").all_inner_texts()]
        assert offered == [word("Low", "Низкое"), word("High", "Высокое")], offered
        picked: list[Any] = []
        visit.on("response", lambda r: picked.append(r) if r.url.endswith(f"/api/sessions/{mine}/model") else None)
        segment.get_by_role("radio", name=word("Low", "Низкое")).click()
        deadline = time.monotonic() + 10
        while not picked and time.monotonic() < deadline:
            visit.wait_for_timeout(50)
        assert picked and picked[0].status == 200, [r.status for r in picked]
        assert picked[0].json()["share_pick"] == {"preset": "fake", "effort": "low", "account": ""}, picked[0].json()
        visit.keyboard.press("Escape")
        expect(visit.locator(".composer .model-select .model-effort")).to_have_text(word("Low", "Низкое"), timeout=10000)
        shot(visit, "visitor-effort")
        asked_before = len(host.provider.requests)
        host.provider.started.clear()
        visit.locator(".composer .composer-field").fill(word("And the prices?", "А цены?"))
        visit.locator(".composer .roundbtn.primary").click()
        assert host.provider.started.wait(30), "the visitor's second turn never reached the model"
        expect(visit.locator(".answer").last).to_contain_text("coffee two.", timeout=20000)
        assert len(host.provider.requests) > asked_before and host.provider.requests[-1].extra.get("reasoning_effort") == "low", host.provider.requests[-1].extra
        assert api("GET", f"/api/sessions/{mine}").get("reasoning_effort") in (None, "", "high"), "the visitor's pick reached the operator's model"

        # 4. The right panel: the file the agent wrote, opened from the Files tab.
        visit.get_by_role("button", name=word("Session actions", "Действия с сессией")).click()
        visit.get_by_role("menuitem", name=word("Project files", "Файлы проекта")).click()
        expect(visit.locator(".panel")).to_be_visible()
        expect(visit.locator(".panel").get_by_text("menu.md").first).to_be_visible(timeout=10000)
        shot(visit, "visitor")
        # The same link on a phone: the conversation with its own bar, and the visitor's mark on the message.
        phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        small = phone.new_page()
        small.goto(f"{link}?lang={LANG}")
        # Another browser holds no pass: the phone is asked too.
        small.locator("[data-unlock] input").fill("four small words")
        small.locator("[data-unlock] button[type=submit]").click()
        expect(small.locator(".msg-visitor").last).to_be_visible(timeout=15000)
        expect(small.locator(".answer").last).to_contain_text("coffee two.")
        shot(small, "phone")
        phone.close()

        # 5. With the token in hand: another session, the settings, the secrets, the list. All refused.
        request = playwright.request.new_context(base_url=f"http://localhost:{host.door}")
        assert request.get(f"/v/{share_token}/api/sessions/{mine}").status == 401, "a locked link answered without its password"
        assert request.post(f"/v/{share_token}/api/share/unlock", data={"password": "four small words"}).ok
        v = f"/v/{share_token}"
        assert request.get(f"{v}/api/sessions/{mine}").ok
        for method, path in (("GET", f"/api/sessions/{other}"), ("POST", f"/api/sessions/{other}/messages"), ("GET", "/api/sessions"),
                             ("POST", "/api/sessions"), ("GET", "/api/secrets"), ("PUT", "/api/settings"), ("DELETE", f"/api/sessions/{mine}"),
                             ("GET", f"/api/sessions/{mine}/live-shares"), ("POST", f"/api/sessions/{mine}/share")):
            answer = request.fetch(f"{v}{path}", method=method, data={"text": "hi"})
            assert answer.status == 404, (method, path, answer.status)
        # An effort the link does not give is refused by the door, whichever way it is asked.
        for body in ({"thinking": True, "reasoning_effort": "xhigh"}, {"thinking": False}, {"thinking": True, "reasoning_effort": "medium"}):
            answer = request.post(f"{v}/api/sessions/{mine}/model", data=body)
            assert answer.status == 403 and answer.json()["code"] == "not_allowed", (body, answer.status, answer.text())
        settings = request.get(f"{v}/api/settings").json()
        assert "base_url" not in str(settings) and "127.0.0.1:1" not in str(settings), settings
        # And the operator's own port does not serve the link at all.
        assert playwright.request.new_context(base_url=base).get(f"{v}/api/sessions/{mine}").status == 404
        # The message is the visitor's in the record too, not only on the screen.
        messages = api("GET", f"/api/sessions/{mine}")["messages"]
        assert any(m.get("visitor") == {"name": "Ann"} for m in messages if m["role"] == "user"), "the visitor's message is not marked"

        # The operator's header says the link is live and who used it.
        own.reload()
        chip = own.locator("[data-live-link]")
        # Two: the desktop browser and the phone, each with its own random id.
        expect(chip).to_contain_text(word("2 visitors", "2 гостя"), timeout=20000)
        shot(own, "header")

        # 5b. What the conversation carries — pictures, files, what the agent sent — reaches the visitor too.
        attached_files(browser, host, base, token, api)

        # 6. Revoked: the open page loses the conversation at once.
        listed = api("GET", f"/api/sessions/{mine}/live-shares")
        assert listed["links"][0]["connections"] >= 1, "the visitor's page holds no stream open"
        began = time.monotonic()
        api("DELETE", f"/api/sessions/{mine}/live-shares/{listed['links'][0]['id']}")
        expect(visit.locator(".visit-gone")).to_be_visible(timeout=10000)
        took = time.monotonic() - began
        expect(visit.locator(".composer")).to_have_count(0)
        assert request.get(f"{v}/api/sessions/{mine}").status == 410
        shot(visit, "ended")
        unexpected = [entry for entry in refused if not entry.startswith(("POST api/presence",))]
        print(f"ok: live link end to end ({LANG}); revoked page closed in {took:.1f} s; routes the page asked and was refused: {sorted(set(unexpected)) or 'none'}")
        browser.close()


if __name__ == "__main__":
    main()
