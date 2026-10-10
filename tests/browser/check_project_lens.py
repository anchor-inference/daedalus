"""The project lens: seen when it is on, lifted in one move, and never set behind the operator's back.

A lens narrows every list of Agents mode to one project. It was announced by a grey chip naming the
project, and set by creating a project, so an operator read a column of two chats as every other
dialog gone. On a desktop the column now says "N chats in {name}" with a "Show all" beside it, and
Escape lifts it too; creating a project opens its folder without narrowing anything, and lifts a
lens on another project. On a phone a project tapped in the drawer narrows Chats for that visit
only, through the address, with the chip that clears it.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 9047 /tmp/app-root &
    APP_URL=http://127.0.0.1:9047/app python3 tests/browser/check_project_lens.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app, folders, open_drawer  # noqa: E402
from folder_stub import WORKSPACES, FolderStub  # noqa: E402

BASE = shots.BASE
LENS = "daedalus.project"
WORDS = {
    "en": {"showall": "Show all", "count": "chats in Bakery site", "palette": "Show all projects", "create": "Create project"},
    "ru": {"showall": "Показать все", "count": "в «Bakery site»", "palette": "Показать все проекты", "create": "Создать проект"},
}


def creating_route(folder_stub: FolderStub, posted: list[dict]):  # type: ignore[no-untyped-def]
    """The shared stub, the folder stub, and a POST /api/projects that makes the project asked for."""

    def route(r) -> None:  # type: ignore[no-untyped-def]
        request = r.request
        path = urlsplit(request.url).path
        if folder_stub.fulfil(r):
            return
        if path == "/api/projects" and request.method == "POST":
            body = request.post_data_json
            posted.append(body)
            made = {"id": "1f2e3d4c5b6a", "name": body["name"], "entity_revision": 1, "created_at": "2026-10-09T00:00:00Z",
                    "folders": folders(f"{WORKSPACES}/{body.get('folder_name', 'x')}"), "settings": {"snapshots": True, "ephemeral": False}, "system": "", "sessions": []}
            r.fulfill(status=200, content_type="application/json", body=json.dumps(made))
            return
        shots.stub(r)

    return route


def main() -> int:
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM)
        for lang in ("en", "ru"):
            words = WORDS[lang]
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            page = context.new_page()
            folder_stub = FolderStub(host="up")
            posted: list[dict] = []

            page.route("**/api/**", creating_route(folder_stub, posted))
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            side = page.locator("nav.sidebar")
            expect(side.locator(".sb-row").first).to_be_visible(timeout=15000)
            everything = side.locator("[data-project]").count()
            check(side.locator(".sb-lens").count() == 0, f"{lang}: no lens strip while every project is listed")

            def lens_on(page=page, side=side) -> None:  # type: ignore[no-untyped-def]
                page.evaluate(f"localStorage.setItem('{LENS}', '{shots.P1}')")
                page.reload()
                expect(side.locator(".sb-lens")).to_be_visible(timeout=15000)

            lens_on()
            strip = side.locator(".sb-lens")
            check(words["count"] in strip.inner_text(), f"{lang}: the strip counts the chats in the project ({strip.inner_text()!r})")
            check(words["showall"] in strip.inner_text(), f"{lang}: and offers Show all")
            shown = side.locator("[data-project]").evaluate_all("els => els.map((e) => e.dataset.project)")
            check(set(shown) <= {shots.P1}, f"{lang}: only the lens's project is listed ({shown})")
            box = strip.bounding_box()
            search = side.locator(".sb-search").bounding_box()
            check(bool(box and search and box["y"] < search["y"]), f"{lang}: the strip stands above the search and the lists")
            clipped = strip.evaluate("el => { const b = el.querySelector('.sb-lens-clear'); return b.scrollWidth > b.clientWidth + 1 }")
            check(not clipped, f"{lang}: Show all is not cut")

            strip.locator(".sb-lens-clear").click()
            expect(side.locator(".sb-lens")).to_have_count(0)
            check(side.locator("[data-project]").count() == everything, f"{lang}: one click brings every project back")
            check(page.evaluate(f"localStorage.getItem('{LENS}')") is None, f"{lang}: and forgets the lens")

            # Escape from a row of the column.
            lens_on()
            side.locator(".sb-row").first.focus()
            page.keyboard.press("Escape")
            expect(side.locator(".sb-lens")).to_have_count(0)
            check(page.evaluate(f"localStorage.getItem('{LENS}')") is None, f"{lang}: Escape in the column lifts the lens")

            # The palette offers lifting it while one is on.
            lens_on()
            page.keyboard.press("Control+k")
            item = page.get_by_text(words["palette"], exact=True)
            expect(item).to_be_visible(timeout=5000)
            item.click()
            expect(side.locator(".sb-lens")).to_have_count(0)
            check(page.evaluate(f"localStorage.getItem('{LENS}')") is None, f"{lang}: the palette's Show all projects lifts the lens")

            # Creating a project does not narrow the column; with a lens on another project it lifts it.
            for start in ("", shots.P1):
                if start:
                    lens_on()
                page.evaluate("window.dispatchEvent(new CustomEvent('daedalus:new-project', { detail: 'agents' }))")
                sheet = page.locator(".sheet.np-sheet")
                expect(sheet).to_be_visible()
                page.locator("#project-name").fill("Greenhouse" if not start else "Greenhouse two")
                sheet.get_by_role("button", name=words["create"]).click()
                expect(sheet).to_have_count(0)
                page.wait_for_timeout(300)
                stored = page.evaluate(f"localStorage.getItem('{LENS}')")
                check(stored is None, f"{lang}: creating a project {'with a lens on another' if start else 'with none'} leaves no lens ({stored})")
                check(side.locator(".sb-lens").count() == 0, f"{lang}: and no strip")
            check(len(posted) == 2, f"{lang}: both projects were posted ({len(posted)})")
            context.close()

            # The phone: a project from the drawer narrows Chats for the visit, not the device.
            context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", is_mobile=True, has_touch=True)
            page = context.new_page()
            page.route("**/api/**", shots.stub)
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            page.wait_for_selector(".app")
            open_drawer(page)
            drawer = page.locator(".ph-drawer-root.open")
            recents = drawer.locator(".ph-drow[data-session]").count()
            check(recents > 8, f"{lang} phone: the drawer lists every recent chat, not eight ({recents})")
            check(drawer.locator(".ph-dsec [data-nav='chats']").count() == 1, f"{lang} phone: Recents has See all")
            drawer.locator(f".ph-drow[data-project='{shots.P1}']").first.click()
            chip = page.locator(".ph-lens")
            expect(chip).to_be_visible(timeout=10000)
            check("project=" in page.url, f"{lang} phone: the drawer's project is in the address ({page.url})")
            check(page.evaluate(f"localStorage.getItem('{LENS}')") is None, f"{lang} phone: and not remembered as the lens")
            chip.click()
            expect(page.locator(".ph-lens")).to_have_count(0)
            check("project=" not in page.url, f"{lang} phone: the chip clears it")
            context.close()
        browser.close()
    return failures + shots.UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
