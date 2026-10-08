"""The desktop application's update, from the rail's button to "Install and restart", with no command.

Updating the desktop application used to take terminal commands. Now a new release lights a blue
button at the foot of the desktop rail, above the menu and Settings; it opens a dialog that downloads
the release, shows how far it got, and then installs it and lets the window close. Settings → About
shows the application's version and checks for a release on demand. The launcher still reports the
command it would have run, and the app must not draw it anywhere: the check looks for it in the whole
document, attributes included.

On a 390 px phone there is no rail, so no button. And one thing beside the update: on the empty start
screen a toast stack sits in the bottom-right corner, not lifted to the middle of the window by the
start page's composer standing there.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_desktop_update.py

With ``SHOTS=<directory>`` it also leaves pictures of the rail and of the dialog ready to install, in
English and in Russian.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import LAUNCHER_COMMAND, LauncherStub, expect_app  # noqa: E402
from screenshots import BASE, CHROMIUM, INBOX, UNHANDLED, notify_frames, stub  # noqa: E402

SHOTS = os.environ.get("SHOTS", "")
WORDS = {
    "en": {"rail": "Update to 0.15.2", "current": "Installed now: 0.15.1", "notes": "What's new", "download": "Download", "install": "Install and restart",
           "closing": "Closing…", "mb": "/ 222 MB", "check": "Check"},
    "ru": {"rail": "Обновить до 0.15.2", "current": "Сейчас установлена 0.15.1", "notes": "Что нового", "download": "Скачать", "install": "Установить и перезапустить",
           "closing": "Закрываемся…", "mb": "/ 222 МБ", "check": "Проверить"},
}


def serve(page: Page, launcher: LauncherStub, *, empty: bool = False) -> None:
    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = urlsplit(request.url).path
        rel = path[path.index("/api/"):] if "/api/" in path else ""
        answered = launcher.answer(request.method, rel)
        if answered is not None:
            status, body = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        if empty and rel == "/api/sessions":
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"sessions": [], "projects": []}))
        return stub(route)

    page.route("**/api/**", handle)


def go(page: Page, path: str, lang: str) -> None:
    page.goto(f"{BASE}{path}?token=t&lang={lang}")


def no_command(page: Page, where: str, problems: list[str]) -> None:
    if LAUNCHER_COMMAND in page.evaluate("document.documentElement.outerHTML"):
        problems.append(f"{where}: the launcher's command is on the page")


def shoot(page: Page, name: str, clip: dict | None = None) -> None:
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), clip=clip)  # type: ignore[arg-type]


def desktop(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    launcher = LauncherStub()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    serve(page, launcher)
    go(page, "/agents", lang)
    rail = page.locator("nav.rail")
    button = rail.locator("[data-rail='update']")
    expect(button).to_be_visible(timeout=20000)
    if button.get_attribute("aria-label") != words["rail"]:
        problems.append(f"{lang}: the rail's button is named {button.get_attribute('aria-label')!r}")
    # Directly above the menu and Settings, at the rail's foot.
    order = rail.locator(".rail-foot [data-rail]").evaluate_all("els => els.map(e => e.dataset.rail)")
    if order[:3] != ["update", "menu", "settings"]:
        problems.append(f"{lang}: the rail's foot is {order}, not the update above the menu and Settings")
    up, menu = button.bounding_box(), rail.locator("[data-rail='menu']").bounding_box()
    if not up or not menu or up["y"] + up["height"] > menu["y"]:
        problems.append(f"{lang}: the update button is not above the menu: {up} / {menu}")
    button.hover()
    expect(button.locator(".rail-tip")).to_have_text(words["rail"])
    if up:
        shoot(page, f"update-rail-{lang}", {"x": 0, "y": max(0, up["y"] - 120), "width": 360, "height": 260})
    no_command(page, f"{lang} rail", problems)

    # The dialog: the offer, then the download with its progress, then the install.
    button.click()
    sheet = page.locator(".update-sheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator(".sheet-head h3")).to_have_text("Daedalus 0.15.2")
    expect(sheet.locator(".update-current")).to_have_text(words["current"])
    notes = sheet.locator(".update-notes")
    expect(notes).to_have_text(words["notes"])
    if notes.get_attribute("href") != "https://example.invalid/releases/tag/desktop-v0.15.2" or notes.get_attribute("target") != "_blank":
        problems.append(f"{lang}: What's new does not open the release page outside the app")
    no_command(page, f"{lang} offer", problems)
    sheet.locator("[data-update-action='download']").click()
    expect(sheet.locator("[data-update-bytes]")).to_contain_text(words["mb"])
    shoot(page, f"update-downloading-{lang}")
    if launcher.posts[-1:] != ["download"]:
        problems.append(f"{lang}: Download posted {launcher.posts}")
    install = sheet.locator("[data-update-action='install']")
    expect(install).to_have_text(words["install"], timeout=10000)
    no_command(page, f"{lang} ready", problems)
    box = sheet.bounding_box()
    if box:
        shoot(page, f"update-ready-{lang}", {"x": max(0, box["x"] - 16), "y": max(0, box["y"] - 16), "width": box["width"] + 32, "height": box["height"] + 32})
    install.click()
    expect(sheet.locator(".update-closing")).to_have_text(words["closing"])
    if launcher.posts[-1:] != ["install"]:
        problems.append(f"{lang}: Install posted {launcher.posts}")
    page.keyboard.press("Escape")

    # Settings → About: the version, and a check on demand.
    go(page, "/settings/about", lang)
    card = page.locator("[data-desktop-app]")
    expect(card.locator("[data-app-version]")).to_have_text("0.15.1", timeout=20000)
    check = card.locator("[data-update-check]")
    expect(check).to_have_text(words["check"])
    before = len(launcher.posts)
    check.click()
    page.wait_for_timeout(300)
    if launcher.posts[before:] != ["check"]:
        problems.append(f"{lang}: Check posted {launcher.posts[before:]}")
    expect(card.locator("[data-update-open]")).to_be_visible()
    shoot(page, f"update-about-{lang}")
    no_command(page, f"{lang} settings", problems)
    context.close()


def phone(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, color_scheme="dark", is_mobile=True, has_touch=True)
    page = context.new_page()
    serve(page, LauncherStub())
    go(page, "/agents", "en")
    page.wait_for_selector(".ph-top .ph-menu", timeout=20000)
    page.wait_for_timeout(500)
    if page.locator(".rail-update").count():
        problems.append("the phone draws the rail's update button")
    context.close()


def toast_corner(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    """A toast on the empty start screen sits in the corner, not over the composer's middle of the page."""
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = context.new_page()
    serve(page, LauncherStub(), empty=True)
    finished = next(e for e in INBOX if e["id"] == 31)
    stub.events = notify_frames(finished)  # type: ignore[attr-defined]
    try:
        go(page, "/agents", "en")
        page.wait_for_selector(".notice-toasts .notice-toast", timeout=20000)
        page.wait_for_timeout(400)
        stack = page.locator(".notice-toasts").bounding_box()
        print("start screen, stack:", stack)
        if not stack or 900 - (stack["y"] + stack["height"]) > 40 or stack["x"] + stack["width"] < 1440 - 40:
            problems.append(f"the toast stack on the start screen is not in the bottom-right corner: {stack}")
    finally:
        stub.events = ""  # type: ignore[attr-defined]
        context.close()


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            desktop(browser, lang, problems)
        phone(browser, problems)
        toast_corner(browser, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
