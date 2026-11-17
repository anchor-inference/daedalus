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
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path
from queue import Queue
from typing import Any

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
            presets={"fake": ModelPresetConfig(provider="fake", model="fake-model", thinking=False, context_window=32000, max_output_tokens=2048)},
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
        sheet.get_by_role("button", name=word("Create live link", "Создать живую ссылку")).click()
        own.locator("[role=alertdialog]").get_by_role("button", name=word("Create live link", "Создать живую ссылку")).click()
        field = sheet.locator(".live-link input.field")
        expect(field).to_be_visible(timeout=10000)
        link = field.input_value()
        assert link.startswith(f"http://localhost:{host.door}/v/") and link.endswith("/"), link
        shot(own, "dialog")
        own.keyboard.press("Escape")
        share_token = link.split("/v/")[1].rstrip("/")

        # 2. A fresh context, with nothing of the operator's, opens the link.
        guest = browser.new_context(viewport={"width": 1440, "height": 900})
        assert not guest.cookies()
        visit = guest.new_page()
        refused: list[str] = []
        visit.on("response", lambda r: refused.append(f"{r.request.method} {r.url.split('/v/')[-1].split('/', 1)[-1]}") if r.status == 404 and "/v/" in r.url else None)
        visit.goto(f"{link}?lang={LANG}")
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
        expect(small.locator(".msg-visitor").last).to_be_visible(timeout=15000)
        expect(small.locator(".answer").last).to_contain_text("coffee two.")
        shot(small, "phone")
        phone.close()

        # 5. With the token in hand: another session, the settings, the secrets, the list. All refused.
        request = playwright.request.new_context(base_url=f"http://localhost:{host.door}")
        v = f"/v/{share_token}"
        assert request.get(f"{v}/api/sessions/{mine}").ok
        for method, path in (("GET", f"/api/sessions/{other}"), ("POST", f"/api/sessions/{other}/messages"), ("GET", "/api/sessions"),
                             ("POST", "/api/sessions"), ("GET", "/api/secrets"), ("PUT", "/api/settings"), ("DELETE", f"/api/sessions/{mine}"),
                             ("GET", f"/api/sessions/{mine}/live-shares"), ("POST", f"/api/sessions/{mine}/share")):
            answer = request.fetch(f"{v}{path}", method=method, data={"text": "hi"})
            assert answer.status == 404, (method, path, answer.status)
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
