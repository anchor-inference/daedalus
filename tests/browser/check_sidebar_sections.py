"""The desktop sidebar's list: the projects and the chats apart under their labels, a collapsed
project that says what its chats are doing, and the keyboard.

A chat's own scratch project (settings.ephemeral) is listed among the chats, by day; a project made by
hand, or kept, is listed under "Projects" whatever it holds, and so is Voice. Projects start collapsed,
the one holding the open chat opens itself, and a collapsed project says in a word that one of its
chats works or waits, and keeps a waiting one in sight. The "Projects" label leads to every project
and its + makes one; every row is one 32 px line whose text starts 12 px in, in English or in Russian.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 9047 /tmp/app-root &
    APP_URL=http://127.0.0.1:9047/app python3 tests/browser/check_sidebar_sections.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE


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
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            page = context.new_page()
            page.route("**/api/**", shots.stub)
            page.goto(f"{BASE}/agents?token=t&scheme=dark&lang={lang}")
            side = page.locator("nav.sidebar")
            expect(side.locator(".sb-row").first).to_be_visible(timeout=15000)

            width = side.evaluate("el => Math.round(el.getBoundingClientRect().width)")
            check(width == 300, f"{lang}: the column is 300 px wide ({width})")
            labels = side.locator(".sb-sec").evaluate_all("els => els.map((e) => e.textContent)")
            check(labels == (["Projects", "Chats"] if lang == "en" else ["Проекты", "Чаты"]), f"{lang}: the list's labels are {labels}")
            check(side.locator(".sb-sec-projects [data-action='all-projects']").count() == 1 and side.locator(".sb-sec-projects .sb-sec-add").count() == 1, f"{lang}: the Projects label leads to every project and carries the +")
            # Every row one 32 px line with its text 12 px in from the row's edge (36 inside a project).
            rows = side.evaluate("""el => [...el.querySelectorAll('.sb-list .sb-row')].map((r) => { const b = r.getBoundingClientRect(); const t = r.querySelector('.sb-t').getBoundingClientRect(); const icon = r.querySelector(':scope > .sb-tile'); return { h: Math.round(b.height), x: Math.round(b.left), pad: Math.round((icon ? icon.getBoundingClientRect().left : t.left) - b.left), nested: !!r.closest('.sb-kids') }; })""")
            bad = [r for r in rows if r["h"] != 32 or r["x"] != 8 or r["pad"] != (36 if r["nested"] else 12)]
            check(rows and not bad, f"{lang}: every row is 32 px with its text 12 px in ({bad or rows[:1]})")

            projects = side.locator("[data-project]").evaluate_all("els => els.map((e) => e.dataset.project)")
            check(sorted(projects) == sorted([shots.P1, shots.P3, "5a8d1c0b6e22", shots.PV]), f"{lang}: the projects section holds the projects and Voice ({projects})")
            days = side.locator(".sb-day").evaluate_all("els => els.map((d) => [d.dataset.day, [...d.querySelectorAll('[data-session]')].map((r) => r.dataset.session)])")
            check(days == [["today", [shots.S4]], ["yesterday", [shots.S6]]], f"{lang}: the chats are by day ({days})")

            bakery = side.locator(f"[data-project='{shots.P1}']")
            check(bakery.locator(".sb-prow").get_attribute("aria-expanded") == "false", f"{lang}: a project starts collapsed")
            live = bakery.locator("[data-session]").evaluate_all("els => els.map((e) => e.dataset.session)")
            check(live == [], f"{lang}: the collapsed project lists no working or idle chat ({live})")
            check(bakery.locator(".sb-prow .sb-word.running").count() == 1, f"{lang}: and says in a word that one of its chats works")

            # The keyboard: / to search, arrows down the rows, right to open a project, left to close it.
            page.locator(".start .composer-field").blur()
            page.mouse.click(900, 860)
            page.keyboard.press("/")
            check(page.evaluate("document.activeElement?.closest('.sb-search') !== null"), f"{lang}: / opens the search")
            page.keyboard.press("ArrowDown")
            first = side.locator(".sb-prow").first
            check(first.evaluate("el => el === document.activeElement"), f"{lang}: down from the search lands on the first row")
            page.keyboard.press("ArrowRight")
            check(first.get_attribute("aria-expanded") == "true", f"{lang}: right opens the project")
            page.keyboard.press("ArrowLeft")
            check(first.get_attribute("aria-expanded") == "false", f"{lang}: left closes it")
            digest = side.locator(f"[data-session='{shots.S4}']")
            digest.focus()
            page.keyboard.press("F2")
            dialog = page.get_by_role("dialog")
            check(dialog.count() == 1 and dialog.locator("input").input_value() == "Weekly digest", f"{lang}: F2 renames the focused chat")
            page.keyboard.press("Escape")
            digest.focus()
            page.keyboard.press("Delete")
            confirm = page.locator(".confirm, [role='alertdialog']")
            check(confirm.count() >= 1, f"{lang}: Delete asks before it deletes")
            page.keyboard.press("Escape")
            digest.focus()
            page.keyboard.press("Enter")
            page.wait_for_url(f"**/agents/{shots.S4}*")
            check(page.evaluate("location.pathname").endswith(f"/agents/{shots.S4}"), f"{lang}: Enter opens the chat")
            page.keyboard.press("Control+Shift+O")
            page.wait_for_function("() => location.pathname.endsWith('/agents')")
            check(page.evaluate("location.pathname").endswith("/agents"), f"{lang}: Ctrl+Shift+O starts a new chat")

            # The open chat's project opens itself.
            page.goto(f"{BASE}/agents/{shots.S2}?token=t&scheme=dark&lang={lang}")
            expect(side.locator(f"[data-session='{shots.S2}']")).to_be_visible(timeout=15000)
            check(bakery.locator(".sb-prow").get_attribute("aria-expanded") == "true", f"{lang}: the project of the open chat opens itself")
            check("current" in (side.locator(f"[data-session='{shots.S2}']").get_attribute("class") or ""), f"{lang}: and its row is the current one")
            context.close()
        browser.close()
    return failures + shots.UNHANDLED.report()


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
