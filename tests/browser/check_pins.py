"""Pinning a project and a chat to the top of the left column, on a desktop and on a phone.

A pinned item sits in the "Pinned" block above Projects and Chats, newest pin first, and nowhere
below it; the block is there only while something is pinned. A pinned project still folds and
unfolds. Pins are made and taken away from the row menu (the desktop's ⋯ and right click, the phone's
row sheet and the project's ⋮) and with P on a focused row, they survive a reload because the server
keeps them, and the status chips filter the block like the rest. On a phone the block heads the
drawer and the Chats page.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 9047 /tmp/app-root &
    APP_URL=http://127.0.0.1:9047/app python3 tests/browser/check_pins.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app, open_drawer  # noqa: E402

BASE = shots.BASE
WORDS = {
    "en": {"pin": "Pin", "unpin": "Unpin", "pinned": "Pinned"},
    "ru": {"pin": "Закрепить", "unpin": "Открепить", "pinned": "Закреплённые"},
}


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
            shots.PINS.clear()

            # -- the desktop ----------------------------------------------------------------
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            page = context.new_page()
            page.route("**/api/**", shots.stub)
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            side = page.locator("nav.sidebar")
            expect(side.locator(".sb-row").first).to_be_visible(timeout=15000)
            block = side.locator(".sb-pinned")
            check(block.count() == 0, f"{lang}: no block while nothing is pinned")

            def item(name: str, page=page):  # type: ignore[no-untyped-def]
                return page.get_by_role("menuitem", name=name, exact=True)

            def pinned_order(block=block) -> list[str]:  # type: ignore[no-untyped-def]
                return block.evaluate("""el => [...el.children].map((c) => c.dataset.session || c.dataset.project)""") if block.count() else []

            # A chat from its ⋯ menu.
            digest = side.locator(f"[data-session='{shots.S4}']")
            digest.hover()
            digest.locator(".sb-acts button[aria-haspopup='menu']").click()
            item(words["pin"]).click()
            expect(block.locator(f"[data-session='{shots.S4}']")).to_be_visible(timeout=5000)
            sections = side.locator(".sb-sec").evaluate_all("els => els.map((e) => e.textContent)")
            check(sections[0].startswith(words["pinned"]), f"{lang}: the block heads the column ({sections})")
            check(side.locator(f"[data-session='{shots.S4}']").count() == 1, f"{lang}: the pinned chat is not listed again under its day")

            # A project with P on its focused row.
            support = side.locator(f"[data-project='{shots.P3}'] .sb-prow")
            support.focus()
            page.keyboard.press("p")
            expect(block.locator(f"[data-project='{shots.P3}']")).to_be_visible(timeout=5000)
            check(pinned_order() == [shots.P3, shots.S4], f"{lang}: newest pin first ({pinned_order()})")
            check(side.locator(f"[data-project='{shots.P3}']").count() == 1, f"{lang}: the pinned project is not listed again under Projects")
            box = block.bounding_box()
            first_project = side.locator(".sb-sec").nth(1).bounding_box()
            check(bool(box and first_project and box["y"] < first_project["y"]), f"{lang}: the block stands above Projects")

            # It keeps its fold.
            head = block.locator(f"[data-project='{shots.P3}'] .sb-prow")
            check(head.get_attribute("aria-expanded") == "false", f"{lang}: the pinned project starts folded")
            head.click()
            check(head.get_attribute("aria-expanded") == "true", f"{lang}: and unfolds on a click")
            check(block.locator(f"[data-project='{shots.P3}'] [data-session='{shots.S3}']").count() == 1, f"{lang}: with its chats under it")
            head.click()

            # The server keeps them.
            page.reload()
            expect(block.locator(".sb-row").first).to_be_visible(timeout=15000)
            check(pinned_order() == [shots.P3, shots.S4], f"{lang}: the pins survive a reload ({pinned_order()})")

            # The chips filter the block: under Waiting only the chat that waits is left in it.
            side.locator(".sb-chips .sb-chip.warn").click()
            page.wait_for_timeout(400)
            check(pinned_order() == [shots.S4], f"{lang}: under Waiting the block keeps what waits ({pinned_order()})")
            side.locator(".sb-chips .sb-chip").first.click()
            page.wait_for_timeout(400)

            # Unpin: the chat from its right-click menu, the project with P.
            block.locator(f"[data-session='{shots.S4}']").click(button="right")
            item(words["unpin"]).click()
            expect(block.locator(f"[data-session='{shots.S4}']")).to_have_count(0, timeout=5000)
            check(side.locator(f".sb-day [data-session='{shots.S4}']").count() == 1, f"{lang}: an unpinned chat goes back under its day")
            block.locator(f"[data-project='{shots.P3}'] .sb-prow").focus()
            page.keyboard.press("p")
            expect(side.locator(".sb-pinned")).to_have_count(0, timeout=5000)
            check(side.locator(f"[data-project='{shots.P3}']").count() == 1, f"{lang}: an unpinned project goes back under Projects, and the block goes")
            context.close()

            # -- the phone ------------------------------------------------------------------
            shots.PINS.clear()
            context = browser.new_context(viewport=shots.PHONE, device_scale_factor=2, color_scheme="dark", is_mobile=True, has_touch=True)
            page = context.new_page()
            page.route("**/api/**", shots.stub)
            page.goto(f"{BASE}/agents?view=chats&token=t&scheme=dark&lang={lang}")
            expect(page.locator(".ph-row").first).to_be_visible(timeout=15000)
            pinned = page.locator("[data-pinned]")
            check(pinned.count() == 0, f"{lang} phone: no block while nothing is pinned")

            def sheet_pick(name: str, page=page) -> None:  # type: ignore[no-untyped-def]
                page.locator(".ph-actions .ph-mrow", has_text=name).first.click()

            page.locator(f"[data-session='{shots.S6}'] .ph-row-more").click()
            sheet_pick(words["pin"])
            expect(pinned.locator(f"[data-session='{shots.S6}']")).to_be_visible(timeout=5000)
            check(page.locator(f"[data-session='{shots.S6}']").count() == 1, f"{lang} phone: the pinned chat is not listed again under its day")
            page.locator(f"section[data-project='{shots.P1}'] .ph-sec-act button[aria-haspopup='menu']").click()
            sheet_pick(words["pin"])
            expect(pinned.locator(f"[data-project-head='{shots.P1}']")).to_be_visible(timeout=5000)
            check(page.locator(f"section[data-project='{shots.P1}']").count() == 0, f"{lang} phone: the pinned project leaves its own section")
            check(pinned.locator(".ph-sec").first.inner_text().startswith(words["pinned"]), f"{lang} phone: the block is headed Pinned")

            page.reload()
            expect(pinned.locator(".ph-row").first).to_be_visible(timeout=15000)
            order = pinned.evaluate("""el => [...el.querySelectorAll('[data-session], [data-project-head]')].filter((e) => !e.closest('.ph-pinkids')).map((e) => e.dataset.session || e.dataset.projectHead)""")
            check(order == [shots.P1, shots.S6], f"{lang} phone: the pins survive a reload, newest first ({order})")
            # Folded, the pinned project keeps its working chat in sight.
            pinned.locator(f"[data-project-head='{shots.P1}'] .ph-row").click()
            kids = pinned.locator(".ph-pinkids [data-session]").evaluate_all("els => els.map((e) => e.dataset.session)")
            check(kids == [shots.S1], f"{lang} phone: a folded pinned project keeps its working chat ({kids})")
            pinned.locator(f"[data-project-head='{shots.P1}'] .ph-row").click()

            open_drawer(page)
            drawer = page.locator(".ph-drawer-root.open")
            rows = drawer.locator(".ph-drow[data-pinned]").evaluate_all("els => els.map((e) => e.dataset.session || e.dataset.project)")
            check(rows == [shots.P1, shots.S6], f"{lang} phone: the drawer heads with the pins ({rows})")
            check(drawer.locator(f".ph-drow[data-project='{shots.P1}']").count() == 1, f"{lang} phone: and does not list the pinned project again")
            check(drawer.locator(f".ph-drow[data-session='{shots.S6}']").count() == 1, f"{lang} phone: nor the pinned chat among the recents")
            page.keyboard.press("Escape")
            page.goto(f"{BASE}/agents?view=chats&token=t&scheme=dark&lang={lang}")
            expect(pinned.locator(".ph-row").first).to_be_visible(timeout=15000)

            pinned.locator(f"[data-session='{shots.S6}'] .ph-row-more").click()
            sheet_pick(words["unpin"])
            expect(pinned.locator(f"[data-session='{shots.S6}']")).to_have_count(0, timeout=5000)
            pinned.locator(f"[data-project-head='{shots.P1}'] .ph-row-more").click()
            sheet_pick(words["unpin"])
            expect(page.locator("[data-pinned]")).to_have_count(0, timeout=5000)
            check(page.locator(f"section[data-project='{shots.P1}']").count() == 1, f"{lang} phone: unpinned, the project has its section again")
            context.close()
        shots.PINS.clear()
        browser.close()
    return failures + shots.UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
