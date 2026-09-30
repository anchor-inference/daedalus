"""A link to one message (`#m<seq>`) opens the chat at that message, however far back it is.

The orchestrator's chat is the long one: its history here is several of the host's pages, and the
message a link points at is only reached by reading four older pages. At 1280 px and on a 390 px
phone, in both languages:

- the project's address with `#m<seq>` opens the chat at that turn, on the screen and lit;
- the plain session address with the same hash moves to the project and keeps the hash on the way;
- a message the history does not hold says so instead of opening anywhere;
- a turn's "copy link" there writes the project's address, not the plain one (desktop);
- nothing scrolls sideways.

    cd miniapp && npx vite build --outDir /tmp/app-dist
    mkdir -p /tmp/app-root && ln -s /tmp/app-dist /tmp/app-root/app
    python3 tests/browser/serve_app.py 8193 /tmp/app-root &
    APP_URL=http://127.0.0.1:8193/app python3 tests/browser/check_message_anchor.py

Exit 0 when every link lands where it points.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, MainStub, expect_app, long_history  # noqa: E402
from check_orchestration_mode import PID, fits, serve, stubs  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ORCHESTRATOR = "orch-bakery"

WORDS = {
    "en": {"gone": "That message is no longer in this conversation.", "link": "Copy a link to this turn"},
    "ru": {"gone": "Этого сообщения больше нет в разговоре.", "link": "Скопировать ссылку на этот ход"},
}


def long_chat(lang: str, planted: dict[int, str] | None = None) -> tuple[FocusStub, MainStub, dict[int, int]]:
    """The Bakery project with an orchestrator's chat of fourteen hundred messages in front of its own."""
    focus, main = stubs(lang)
    questions = long_history(focus.details[ORCHESTRATOR], planted=planted)
    return focus, main, questions


def go(page: Page, path: str, lang: str) -> None:
    """The app's address with the sign-in and the language in its query, and the hash after them."""
    path, _, hash_ = path.partition("#")
    joiner = "&" if "?" in path else "?"
    page.goto(f"{BASE}{path}{joiner}token=t&lang={lang}" + (f"#{hash_}" if hash_ else ""))


def landed(page: Page, question: int, where: str) -> list[str]:
    """The lit turn is the one asked for, and it is on the screen, not under an edge of it."""
    problems: list[str] = []
    lit = page.locator(".chat-scroll .anchored")
    expect(lit).to_have_count(1, timeout=15000)
    expect(lit).to_contain_text(re.compile(rf"Question {question}:"))
    # The turns above it take their heights over the first frames and the chat puts it back each
    # time; what counts is where it rests, read while it is still lit.
    box = {"top": -1.0, "bottom": 0.0}
    for _ in range(8):
        box = page.evaluate(
            "() => { const lit = document.querySelector('.chat-scroll .anchored'); if (!lit) return { top: -1, bottom: 0 };"
            " const el = lit.getBoundingClientRect(); const s = document.querySelector('.chat-scroll').getBoundingClientRect();"
            " return { top: el.top - s.top, bottom: s.bottom - el.top }; }"
        )
        if box["top"] >= -2 and box["bottom"] >= 40:
            break
        page.wait_for_timeout(150)
    print(where, "lit turn at", box)
    if box["top"] < -2 or box["bottom"] < 40:
        problems.append(f"{where}: the message is not on the screen ({box})")
    fits(page, where)
    return problems


def check(page: Page, lang: str, name: str) -> list[str]:
    problems: list[str] = []
    words = WORDS[lang]
    focus, main, questions = long_chat(lang)
    serve(page, focus, main, lang)

    # The project's own address, four pages back.
    go(page, f"/orchestration/project/{PID}#m{questions[20]}", lang)
    problems += landed(page, 20, f"{lang} {name} project link")

    # The plain address of the orchestrator's session: moved to the project, the hash kept.
    go(page, f"/agents/{ORCHESTRATOR}#m{questions[30]}", lang)
    page.wait_for_url(re.compile(rf"/app/orchestration/project/{PID}\?.*#m{questions[30]}$|/app/orchestration/project/{PID}#m{questions[30]}$"))
    problems += landed(page, 30, f"{lang} {name} plain link")

    # A turn's link, copied where the turn is, is the project's address.
    if name == "desktop":
        page.wait_for_timeout(2800)
        turn = page.locator(".chat-scroll .turn", has_text=re.compile(r"Question 30:")).first
        turn.hover()
        turn.locator(f"button[aria-label='{words['link']}']").click()
        page.wait_for_timeout(300)
        clip = page.evaluate("() => navigator.clipboard.readText()")
        print(lang, name, "copied", clip)
        if not clip.endswith(f"/app/orchestration/project/{PID}#m{questions[30]}"):
            problems.append(f"{lang} {name}: the copied link is not the project's address ({clip!r})")

    # A message before the start of the history: said, not guessed.
    go(page, f"/orchestration/project/{PID}#m0", lang)
    expect(page.locator(".anchor-note")).to_have_text(words["gone"], timeout=15000)
    expect(page.locator(".chat-scroll .anchored")).to_have_count(0)
    fits(page, f"{lang} {name} missing message")
    return problems


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for name, width, height in (("desktop", 1280, 900), ("phone", 390, 844)):
                phone = name == "phone"
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=phone, has_touch=phone, permissions=["clipboard-read", "clipboard-write"])
                problems += check(context.new_page(), lang, name)
                context.close()
                print(f"message anchor {lang} {name}: done")
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
