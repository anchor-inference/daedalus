"""Settings → Translation is a section of its own, found by search.

At 1440 × 900 and on a 390 px phone, in English and Russian:

- the Settings search finds the Translation section and every one of its rows by "translate",
  "translation", "translator", "language", "перевод", "переводчик" and "язык" — the words an operator
  types, which once found nothing because the card sat at the foot of Chat & scheduler;
- the section is in the list under Agent, opens from it at ``/app/settings/translation`` under its own
  title, and holds the answer language beside the translation card; Working rules no longer does;
- a link to one of its rows opens the section on that row;
- the translator is a model chosen from a list that opens, and the list stays on the screen.

Pictures go to ``SHOTS`` when it is set.

    APP_URL=http://127.0.0.1:8471/app python3 tests/browser/check_settings_translation.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from screenshots import UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
LANG = os.environ.get("LANG_UI", "")
SHOTS = os.environ.get("SHOTS", "")
ROWS = ["answer_language", "translation.operator_language", "translation.agent_language", "translation.preset", "translation.timeout_seconds"]
WORDS = ["translate", "translation", "translator", "language", "перевод", "переводчик", "язык"]
TITLE = {"en": "Translation", "ru": "Перевод"}


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / name))


def run(browser, lang: str, viewport: dict, problems: list[str], **context) -> None:  # type: ignore[no-untyped-def]
    phone = viewport["width"] < 1024
    where = f"[{lang} {viewport['width']}]"
    say = lambda text: problems.append(f"{where} {text}")  # noqa: E731
    print(where)
    page = browser.new_context(viewport=viewport, color_scheme="dark", **context).new_page()
    page.route("**/api/**", stub)

    # The search, by every word for it in both languages: the section and each of its rows.
    page.goto(f"{BASE}/settings?token=t&lang={lang}")
    field = page.locator(".settings-search").first
    field.wait_for(timeout=15000)
    for word in WORDS:
        field.fill(word)
        # The list of what was found: the section, and under it each row as a way straight to it.
        try:
            page.locator('.settings-index .settings-link[data-section="translation"]').wait_for(timeout=5000)
        except Exception:  # noqa: BLE001
            say(f"the search for {word!r} did not offer the Translation section")
            continue
        missing = [row for row in ROWS if not page.locator(f'.settings-field-hit[data-field-hit="{row}"]').is_visible()]
        if missing:
            say(f"the search for {word!r} listed no way to {missing}")
        # Beside the list on a desktop, the found rows themselves, as the controls they are.
        if not phone:
            section = page.locator('.settings-hit-section[data-hit-section="translation"]')
            page.wait_for_timeout(700)  # the hit body settles which rows it keeps
            missing = [row for row in ROWS if not section.locator(f'[data-setting="{row}"]').first.is_visible()]
            if missing:
                say(f"the search for {word!r} left out {missing}")
        if word in ("translate", "перевод"):
            shot(page, f"search-{word}-{lang}{'-phone' if phone else ''}.png")
    field.fill("")

    # The section in the list, under Agent, opened from it.
    if phone:
        page.goto(f"{BASE}/settings/translation?token=t&lang={lang}")
    else:
        link = page.locator('.settings-link[data-section="translation"]')
        group = link.evaluate("(a) => { const ids = [...a.parentElement.querySelectorAll('.settings-link')].map((x) => x.dataset.section); return ids; }")
        if "rules" not in group or "memory" not in group:
            say(f"Translation is not listed under Agent: {group}")
        link.click()
    page.wait_for_selector('[data-setting="translation.preset"]', timeout=10000)
    path = page.evaluate("location.pathname")
    if not path.endswith("/settings/translation"):
        say(f"the section opened at {path}")
    if not phone:
        heading = page.locator(".settings-title").first.inner_text().strip()
        if heading != TITLE[lang]:
            say(f"the section is titled {heading!r}")
    for row in ROWS:
        if not page.locator(f'[data-setting="{row}"]').first.is_visible():
            say(f"the section does not show {row}")

    # The translator is a model chosen from a list, as the value at the end of its row.
    preset = page.locator('[data-setting="translation.preset"]')
    preset.scroll_into_view_if_needed()
    trigger = preset.locator(".dropdown-btn")
    if not trigger.count():
        say("the translator is not chosen from a list")
    else:
        trigger.click()
        page.wait_for_selector(".dropdown-list [role=option]", timeout=5000)
        if page.locator(".dropdown-list [role=option]").count() < 2:
            say("the translator's list offers no models")
        box = page.locator(".dropdown-list").bounding_box() or {"x": -1, "width": 0}
        if box["x"] < 0 or box["x"] + box["width"] > viewport["width"] + 0.5:
            say(f"the translator's list leaves the screen: {box}")
        page.keyboard.press("Escape")
        shot(page, f"section-{lang}{'-phone' if phone else ''}.png")

    # Working rules no longer holds the answer language.
    page.goto(f"{BASE}/settings/rules?token=t&lang={lang}")
    page.wait_for_selector('[data-setting="prompt.rules"]', timeout=10000)
    if page.locator('[data-setting="answer_language"]').count():
        say("Working rules still shows the answer language")

    # A link to a row of the section opens it on that row.
    page.goto(f"{BASE}/settings/translation?token=t&lang={lang}#answer_language")
    try:
        page.wait_for_selector('[data-setting="answer_language"].setting-flash', timeout=8000)
    except Exception:  # noqa: BLE001
        say("a link to the answer language did not land on its row")

    widths = page.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    if widths[0] > widths[1]:
        say(f"the page scrolls sideways: {widths}")
    page.context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ([LANG] if LANG else ["en", "ru"]):
            run(browser, lang, {"width": 1440, "height": 900}, problems)
            run(browser, lang, {"width": 390, "height": 844}, problems, is_mobile=True, has_touch=True)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
