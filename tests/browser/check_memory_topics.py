"""The Memory screen by topic and by category, its merge, the tidying's suggestions, undo, and
Settings → Memory's sensitive topics.

What is checked, on a desktop window and on a phone, in both languages:

- By topic: one group per topic, the biggest first, a record with two topics under both, the
  records without a topic last; a topic tag on a card filters the screen to that topic;
- By category: one group per category that has records, in the categories' order;
- the suggestion the tidying was unsure of is offered, and Merge applies it and the banner goes;
- History lists the change and Undo asks the host to put it back;
- selecting two records and merging them sends both ids;
- on a phone nothing leaves the screen sideways;
- Settings → Memory: switching a sensitive topic on saves the list with it.

    APP_URL=http://127.0.0.1:8236/app python3 tests/browser/check_memory_topics.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from memory_stub import MemoryStub  # noqa: E402
from screenshots import MEMORY_RECORDS, MEMORY_SESSIONS, MEMORY_SUGGESTIONS, SETTINGS, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

OVERFLOW = """() => {
  const vw = document.documentElement.clientWidth;
  const out = [];
  for (const el of document.querySelectorAll('.memory *, .pagehead *')) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    if (el.closest('.chiprow, .chips, [class*="chip-row"]')) continue;
    if (r.left < -0.5 || r.right > vw + 0.5) out.push((el.className || el.tagName) + ' ' + Math.round(r.left) + '..' + Math.round(r.right));
  }
  return out;
}"""


def open_page(browser, width: int, lang: str, memory: MemoryStub, path: str = "memory", saved: list[dict] | None = None) -> Page:  # type: ignore[no-untyped-def]
    phone = width < 600
    context = browser.new_context(viewport={"width": width, "height": 900}, is_mobile=phone, has_touch=phone, color_scheme="dark")
    context.add_init_script("try { localStorage.removeItem('daedalus.memory.view'); } catch (e) {}")
    page = context.new_page()
    page.route("**/api/**", stub)

    def memory_route(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        rel = request.url.split("?", 1)[0]
        rel = rel[rel.index("/api/"):]
        body = json.loads(request.post_data or "{}") if request.post_data else None
        answered = memory.answer(request.method, rel, body)
        if answered is None:
            return stub(route)
        return route.fulfill(status=answered[0], content_type="application/json", body=json.dumps(answered[1]))

    page.route("**/api/memory**", memory_route)
    if saved is not None:
        state = {"settings": copy.deepcopy(SETTINGS) | {"revision": "r1"}}

        def settings(route) -> None:  # type: ignore[no-untyped-def]
            if route.request.method == "PUT":
                body = json.loads(route.request.post_data or "{}")
                saved.append(body)
                state["settings"] = {**state["settings"], **{k: v for k, v in body.items() if k != "base_revision"}}
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(state["settings"]))

        page.route("**/api/settings", settings)
        page.route("**/api/settings/validate", lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps({"valid": True, "stale": False, "problems": []})))
    page.goto(f"{BASE}/{path}?token=t&lang={lang}")
    return page


def groups(page: Page) -> list[tuple[str, list[str]]]:
    return page.evaluate("""() => [...document.querySelectorAll('.memory-group[data-group]')].map((g) => [g.dataset.group, [...g.querySelectorAll('.memory-card')].map((c) => c.dataset.memory)])""")


def check(browser, width: int, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} {width}px"
    memory = MemoryStub(MEMORY_RECORDS, MEMORY_SESSIONS, MEMORY_SUGGESTIONS)
    page = open_page(browser, width, lang, memory)
    page.wait_for_selector(".memory-card", timeout=20000)
    views = page.locator(".memory-view [role=radio]")
    views.nth(1).click()
    page.wait_for_selector(".memory-group[data-group]", timeout=5000)
    found = groups(page)
    names = [name for name, _ in found]
    if names[:2] != ["bakery", "images"] or names[-1] != "":
        problems.append(f"{where}: the topic groups are {names}, not the biggest first and the untagged last")
    by_name = dict(found)
    if sorted(by_name.get("bakery", [])) != ["m3", "m4", "m7"] or sorted(by_name.get("images", [])) != ["m4", "m7"] or by_name.get("") != ["m8"]:
        problems.append(f"{where}: the records are not under each of their topics: {found}")
    if width < 600:
        for item in page.evaluate(OVERFLOW)[:5]:
            problems.append(f"{where}: past the screen's edge: {item}")
    page.locator('.memory-card[data-memory="m5"] .topic-tag', has_text="#refunds").first.click()
    page.wait_for_selector(".memory-filter", timeout=5000)
    shown = set(page.evaluate("() => [...document.querySelectorAll('.memory-card')].map((c) => c.dataset.memory)"))
    if shown != {"m5"}:
        problems.append(f"{where}: filtering by #refunds shows {sorted(shown)}")
    page.locator(".memory-filter .chip").click()
    views.nth(2).click()
    page.wait_for_timeout(200)
    categories = [name for name, _ in groups(page)]
    if categories != ["person", "project", "preference", "fact", "procedure"]:
        problems.append(f"{where}: the category groups are {categories}")
    # The suggestion: offered, applied, gone.
    banner = page.locator(".memory-suggest-banner")
    if not banner.is_visible():
        problems.append(f"{where}: the waiting suggestion is not offered")
    else:
        banner.click()
        page.wait_for_selector(".memory-suggestion", timeout=5000)
        page.locator('.memory-suggestion [data-action="apply"]').click()
        page.wait_for_timeout(400)
        if ("POST", "/api/memory/suggestions/1/apply") not in [(m, p) for m, p, _ in memory.writes]:
            problems.append(f"{where}: Merge on the suggestion did not apply it")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
        if page.locator(".memory-suggest-banner").count():
            problems.append(f"{where}: the banner still offers the applied suggestion")
    # History and undo.
    page.locator('[data-action="history"]').click()
    page.wait_for_selector(".memory-history-entry", timeout=5000)
    page.locator('.memory-history-entry [data-action="undo"]').first.click()
    page.wait_for_timeout(400)
    if not any(p.endswith("/undo") for _, p, _ in memory.writes):
        problems.append(f"{where}: Undo in the history did not reach the host")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    # Merging two selected records.
    views.nth(0).click()
    page.locator('[data-action="select"]').click()
    page.locator('.memory-card[data-memory="m1"]').click()
    page.locator('.memory-card[data-memory="m8"]').click()
    page.locator('[data-action="merge"]').click()
    page.locator('[data-action="merge-confirm"]').click()
    page.wait_for_timeout(400)
    merges = [body for m, p, body in memory.writes if p == "/api/memory/merge"]
    if not merges or sorted(merges[-1].get("ids", [])) != ["m1", "m8"]:
        problems.append(f"{where}: merging the two selected records sent {merges}")
    page.context.close()


def check_settings(browser, width: int, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} {width}px settings"
    saved: list[dict] = []
    page = open_page(browser, width, lang, MemoryStub(), "settings/memory", saved)
    page.wait_for_selector('.memory-settings [data-subject="health"] [role=switch]', timeout=20000)
    if page.locator(".memory-settings [data-subject]").count() != 5:
        problems.append(f"{where}: not five sensitive topics")
    page.locator('.memory-settings [data-subject="health"] [role=switch]').click()
    page.wait_for_timeout(500)
    sent = (saved[-1].get("memory") or {}) if saved else {}
    if sent.get("sensitive_allowed") != ["health"] or sent.get("consolidate") is not True:
        problems.append(f"{where}: switching health on saved {sent}")
    page.context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width in (1280, 390):
                check(browser, width, lang, problems)
                check_settings(browser, width, lang, problems)
            print(f"memory topics {lang}: {'ok' if not any(x.startswith(lang) for x in problems) else 'FAILED'}")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    return (1 if problems else 0) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
