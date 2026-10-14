"""Settings → Components: the SearXNG card goes from not installed to running, and back out again.

Driven in both languages, on a desktop window and on a phone, in the dark and the light: the card
offers one install button with its size, says where the instance answers once it runs, and offers the
removal behind a confirmation. Nothing may run off the side of the screen at any point.

``SHOTS`` names a folder to keep pictures of the phone card in, for a person to look at.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from api_stub import DEFAULT_APP, expect_app
from playwright.sync_api import Page, expect, sync_playwright
from screenshots import COMPONENTS, UNHANDLED, stub

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")

WORDS = {
    "en": {"name": "SearXNG", "missing": "not installed", "running": "running", "install": "Install (≈ 40 MB)", "remove": "Remove",
           "at": "Running at http://127.0.0.1:8768.", "unlocks": "better web search", "failed": "failed"},
    "ru": {"name": "SearXNG", "missing": "не установлено", "running": "работает", "install": "Установить (≈ 40 МБ)", "remove": "Удалить",
           "at": "Работает по адресу http://127.0.0.1:8768.", "unlocks": "лучший веб-поиск", "failed": "сбой"},
}

MISSING = {
    "id": "searxng", "state": "missing", "detail": "", "detail_key": "comp.detail.searxng.missing", "detail_args": {},
    "installable": True, "how": "service", "fix": "", "download_bytes": 41_943_040, "disk_bytes": 0, "requires_restart": False,
    "installed_count": 0, "total_count": 0, "skills": [], "enables": ["websearch"], "progress": None, "service": None, "removable": False,
}
RUNNING = {
    **MISSING, "state": "installed", "installable": False, "detail_key": "comp.detail.searxng.running",
    "detail_args": {"url": "http://127.0.0.1:8768", "error": "", "seconds": "0"}, "disk_bytes": 136_314_880, "removable": True,
    "service": {"state": "running", "url": "http://127.0.0.1:8768", "error": "", "retry_in": 0, "version": "2026.9.8+3fdc6d753"},
}
FAILED = {
    **RUNNING, "detail_key": "comp.detail.searxng.failed", "detail_args": {"url": "", "error": "exited with code 1", "seconds": "8"},
    "service": {"state": "failed", "url": "", "error": "exited with code 1", "retry_in": 8, "version": "2026.9.8+3fdc6d753"},
}


class Host:
    """The components endpoints, with SearXNG in whatever state the scene has put it."""

    def __init__(self) -> None:
        self.searxng = dict(MISSING)
        self.calls: list[str] = []

    def view(self) -> dict[str, object]:
        others = [c for c in COMPONENTS["components"] if c["id"] != "searxng"]  # type: ignore[index]
        return {**COMPONENTS, "busy": "", "components": [others[0], self.searxng, *[dict(c, progress=None, state="missing" if c["state"] == "installing" else c["state"]) for c in others[1:]]]}

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        if rel == "/api/components" and request.method == "GET":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(self.view()))
        if rel == "/api/components/searxng/install" and request.method == "POST":
            self.calls.append("install")
            self.searxng = dict(RUNNING)
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"id": "searxng", "state": "queued", "step": "starting", "step_key": "", "error": "", "restart_required": False}))
        if rel == "/api/components/searxng/remove" and request.method == "POST":
            self.calls.append("remove")
            self.searxng = dict(MISSING)
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(MISSING))
        return stub(route)


def no_overflow(page: Page, where: str) -> None:
    sideways = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    assert sideways <= 1, f"{where}: the page scrolls sideways by {sideways}px"
    card = page.locator(".comp-card", has=page.locator(".comp-head b", has_text="SearXNG")).first
    box = card.bounding_box()
    width = page.viewport_size["width"]  # type: ignore[index]
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, f"{where}: the card runs off the side: {box}"
    for button in card.locator("button").all():
        b = button.bounding_box()
        assert b is not None and b["x"] + b["width"] <= box["x"] + box["width"] + 1, f"{where}: a button sticks out of the card"


def scene(browser, lang: str, viewport: dict[str, int], scheme: str, mobile: bool) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} {viewport['width']}x{viewport['height']} {scheme}"
    host = Host()
    context = browser.new_context(viewport=viewport, color_scheme=scheme, is_mobile=mobile, has_touch=mobile, device_scale_factor=2)
    page = context.new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/settings/components?token=t&scheme={scheme}&lang={lang}")
    card = page.locator(".comp-card", has=page.locator(".comp-head b", has_text=words["name"])).first
    expect(card).to_be_visible(timeout=15000)
    card.scroll_into_view_if_needed()
    expect(card.locator(".comp-chip")).to_have_text(words["missing"])
    expect(card.locator(".comp-tag")).to_have_text(words["unlocks"])
    expect(card.get_by_role("button", name=words["remove"])).to_have_count(0)
    install = card.get_by_role("button", name=words["install"])
    expect(install).to_have_count(1)
    no_overflow(page, where + " before")
    if SHOTS and mobile:
        card.screenshot(path=str(Path(SHOTS) / f"searxng-missing-{lang}-{scheme}.png"))

    install.click()
    expect(card.locator(".comp-chip")).to_have_text(words["running"])
    expect(card.locator(".comp-detail")).to_have_text(words["at"])
    expect(card.get_by_role("button", name=words["install"])).to_have_count(0)
    no_overflow(page, where + " running")
    if SHOTS and mobile:
        expect(page.locator(".toast")).to_have_count(0, timeout=10000)  # the "installing" toast, gone by itself
        card.screenshot(path=str(Path(SHOTS) / f"searxng-running-{lang}-{scheme}.png"))

    # A failure is a word and a reason on the card, not an "installed" over a dead instance.
    host.searxng = dict(FAILED)
    page.reload()
    card = page.locator(".comp-card", has=page.locator(".comp-head b", has_text=words["name"])).first
    expect(card.locator(".comp-chip")).to_have_text(words["failed"], timeout=15000)
    expect(card.locator(".comp-detail")).to_contain_text("exited with code 1")
    no_overflow(page, where + " failed")

    card.get_by_role("button", name=words["remove"]).click()
    dialog = page.get_by_role("alertdialog")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name=words["remove"]).click()
    expect(card.locator(".comp-chip")).to_have_text(words["missing"])
    assert host.calls == ["install", "remove"], host.calls
    no_overflow(page, where + " after")
    context.close()


def run() -> int:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            scene(browser, lang, {"width": 1440, "height": 900}, "dark", False)
            for scheme in ("dark", "light"):
                scene(browser, lang, {"width": 390, "height": 844}, scheme, True)
        browser.close()
    print("searxng component: install, running, failed and removal, en and ru, desktop and phone: passed")
    return UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run())
