"""A chat started on the host from a Docker installation, and the mark a host session carries.
English and Russian, 1440 px desktop and 390 px phone.

What is checked:

- in Docker with the host terminal daemon answering, the start composer offers where the chat runs
  (the pill beside the model on a desktop, a chip under the heading beside the project and the mode
  on a phone), and a chat started on the host is created with ``env: "host"``;
- with the daemon down the host is offered greyed out, with the reason;
- on a phone with a project picked, the chip shows the side of the project's folder, its sheet says
  the folder decides and opens the project's settings, where the folder is moved; the chat is created
  in the project and sends no ``env`` of its own;
- natively there is no choice at all, since everything already runs on the host;
- a session that runs on the host carries the host pill on its sidebar row and in its Details panel.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_host_chat.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import P1, PROJECTS, S1, S4, UNHANDLED, detail, listing, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
DOCKER = {"local": "container", "available": ["container", "host"], "host_bridge": True, "docker": True}
DOWN = {"local": "container", "available": ["container"], "host_bridge": False, "docker": True}
NATIVE = {"local": "host", "available": ["host"], "host_bridge": False, "docker": False}
WORDS = {
    "en": {"host": "On the host", "down": "not answering", "decides": "run where its folder is", "settings": "Project settings"},
    "ru": {"host": "На хосте", "down": "не отвечает", "decides": "работают там же, где его папка", "settings": "Настройки проекта"},
}


class Host:
    def __init__(self, environments: dict) -> None:
        self.environments = environments
        self.created: list[dict] = []
        self.host_folder = False


def serve(page: Page, host: Host) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        request = r.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):]
        if path == "/api/project-environments":
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(host.environments))
        if path == "/api/projects" and request.method == "GET" and host.host_folder:
            # The first project's folder moved to the host: its chats run there.
            answer = [{**p, "folders": [{**f, "env": "host"} for f in p["folders"]]} if p["id"] == P1 else p for p in PROJECTS]
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
        if path == "/api/sessions" and request.method == "POST":
            host.created.append(request.post_data_json or {})
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"id": S1}))
        if request.method == "POST" and path.startswith(f"/api/sessions/{S1}/"):
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "run_id": "r1"}))
        if path == "/api/sessions" and request.method == "GET":
            # One session runs on the host; the rest are the container's.
            answer = listing()
            answer["sessions"] = [{**s, "env": "host" if s["id"] == S4 else "container"} for s in answer["sessions"]]
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
        if path == f"/api/sessions/{S4}" and request.method == "GET":
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({**detail(S4), "env": "host"}))
        return stub(r)

    page.route("**/api/**", route)


def start(browser, lang: str, phone: bool, environments: dict, init: str = ""):  # type: ignore[no-untyped-def]
    size = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=size, is_mobile=phone, has_touch=phone, color_scheme="dark")
    if init:
        context.add_init_script(init)
    page = context.new_page()
    host = Host(environments)
    serve(page, host)
    return context, page, host


def run(browser, lang: str, phone: bool, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} {'390' if phone else '1440'}px"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731

    # Docker with the daemon answering: the choice is there, and the host is sent.
    context, page, host = start(browser, lang, phone, DOCKER)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    composer = page.locator(".start-composer")
    expect(composer).to_be_visible(timeout=15000)
    if phone:
        # Beside the project and the mode under the heading, where it is seen before typing.
        chip = page.locator(".ph-hero-picks .runon-chip")
        expect(chip).to_have_attribute("data-runon-chip", "container")
        box = chip.bounding_box()
        if not box or box["height"] < 32:
            say(f"the chip is not touch-sized: {box}")
        chip.click()
        row = page.locator(".runon-sheet [data-runon='host']")
        expect(row).to_be_visible()
        expect(row).to_contain_text(words["host"])
        if row.is_disabled():
            say("the host is greyed out while the daemon answers")
        if SHOTS:
            page.wait_for_timeout(400)  # the sheet's rise, so the picture is not of it half-way
            page.screenshot(path=f"{SHOTS}/phone-runon-sheet-{lang}.png")
        row.click()
        expect(page.locator(".runon-sheet")).to_have_count(0)
        expect(chip).to_have_attribute("data-runon-chip", "host")
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/phone-runon-host-{lang}.png")
    else:
        select = composer.locator(".runon-select")
        expect(select).to_be_visible()
        expect(select.locator(".term-env.container")).to_be_visible()
        select.click()
        option = page.locator(".runon-menu .runon-option").nth(1)
        expect(option).to_contain_text(words["host"])
        if option.is_disabled():
            say("the host is greyed out while the daemon answers")
        option.click()
        expect(select.locator(".term-env.host")).to_be_visible()
    composer.locator("textarea").fill("Check the disk")
    composer.locator(".roundbtn.primary").click()
    page.wait_for_timeout(600)
    if not host.created or host.created[-1].get("env") != "host":
        say(f"the chat was not created on the host: {host.created}")
    if page.evaluate("document.documentElement.scrollWidth - innerWidth") > 0:
        say("the page scrolls sideways")
    context.close()

    # The daemon down: the host is there, greyed out, with the reason.
    context, page, host = start(browser, lang, phone, DOWN)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    composer = page.locator(".start-composer")
    expect(composer).to_be_visible(timeout=15000)
    if phone:
        page.locator(".ph-hero-picks .runon-chip").click()
        row = page.locator(".runon-sheet [data-runon='host']")
        expect(row).to_be_disabled()
        expect(row).to_contain_text(words["down"])
    else:
        composer.locator(".runon-select").click()
        option = page.locator(".runon-menu .runon-option").nth(1)
        expect(option).to_be_disabled()
        expect(option).to_contain_text(words["down"])
    context.close()

    # Natively there is nothing to choose.
    context, page, host = start(browser, lang, phone, NATIVE)
    page.goto(f"{BASE}/agents?token=t&lang={lang}")
    composer = page.locator(".start-composer")
    expect(composer).to_be_visible(timeout=15000)
    page.wait_for_timeout(500)
    if composer.locator(".runon-select").count():
        say("the choice is offered natively")
    if phone and page.locator(".runon-chip").count():
        say("the phone offers a side natively")
    context.close()

    # A phone with a project picked: the folder decides, the chip says which side, and the sheet
    # leads to the project's settings, where the folder is moved.
    if phone:
        for on_host in (False, True):
            context, page, host = start(browser, lang, phone, DOCKER)
            host.host_folder = on_host
            side = "host" if on_host else "container"
            page.goto(f"{BASE}/agents?token=t&lang={lang}&in={P1}")
            chip = page.locator(".ph-hero-picks .runon-chip")
            expect(chip).to_have_attribute("data-runon-chip", side, timeout=15000)
            chip.click()
            panel = page.locator(f".runon-sheet [data-runon-project='{P1}']")
            expect(panel).to_contain_text(words["decides"])
            expect(panel.locator(f".term-env.{side}")).to_be_visible()
            if page.locator(".runon-sheet [data-runon]").count():
                say("a project's chat offers a side the server would refuse")
            if SHOTS:
                page.wait_for_timeout(400)
                page.screenshot(path=f"{SHOTS}/phone-runon-project-{side}-{lang}.png")
            panel.get_by_role("button", name=words["settings"]).click()
            expect(page.locator(".sheet").filter(has=page.locator("[data-folder]"))).to_be_visible()
            page.keyboard.press("Escape")
            composer = page.locator(".start-composer")
            composer.locator("textarea").fill("Check the oven timer")
            composer.locator(".roundbtn.primary").click()
            page.wait_for_timeout(600)
            made = host.created[-1] if host.created else {}
            if made.get("project_id") != P1 or "env" in made:
                say(f"the project's chat was not created in the project alone: {host.created}")
            context.close()

    # The mark: the host session's row, and its Details on a desktop.
    if not phone:
        context, page, host = start(browser, lang, phone, DOCKER, "try { localStorage.setItem('daedalus.session.panel', 'details'); } catch (e) {}")
        page.goto(f"{BASE}/agents/{S4}?token=t&lang={lang}")
        row = page.locator(f".sidebar [data-session='{S4}']").first
        expect(row).to_be_visible(timeout=15000)
        expect(row.locator(".host-mark .term-env.host")).to_be_visible()
        if page.locator(f".sidebar [data-session='{S1}'] .host-mark").count():
            say("a container session is marked as on the host")
        expect(page.locator(".panel .host-mark .term-env.host")).to_be_visible(timeout=10000)
        context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for phone in (False, True):
                try:
                    run(browser, lang, phone, problems)
                except Exception as exc:  # noqa: BLE001 — one failure must not hide the rest
                    problems.append(f"{lang} {'390' if phone else '1440'}px: {exc}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("ok: the host chat choice and its mark")
    return (1 if problems else 0) + UNHANDLED.report()

if __name__ == "__main__":
    sys.exit(main())
