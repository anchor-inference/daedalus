"""The panel's Overview says what the chat's subscription has left.

The Overview is the panel's front page, and its Usage row said "$0 · today unmetered" for a model on a
subscription, which tells nothing. Now:

- a chat whose model runs on a subscription has a row named for the provider ("Claude") with each window
  ("5h", "weekly") as a thin meter and its share, when it starts again in the tooltip, and the attention
  colour on a window near its limit; the row opens the Usage tool;
- a metered model keeps its spend, without the "today unmetered" words;
- a subscription the host has no windows for draws no row at all rather than zeros.

    APP_URL=http://127.0.0.1:<port>/app python tests/browser/check_panel_overview.py

Exit 0 when every claim holds.
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = Path(os.environ["SHOTS"]) if os.environ.get("SHOTS") else None
WORDS = {"en": {"resets": "Resets", "unmetered": "unmetered", "usage": "Usage"}, "ru": {"resets": "Обновится", "unmetered": "без оплаты", "usage": "Расход"}}


def serve(page, usage: dict) -> None:  # type: ignore[no-untyped-def]
    def route(r) -> None:  # type: ignore[no-untyped-def]
        path = urlsplit(r.request.url).path
        path = path[path.index("/api/"):]
        if path.startswith("/api/usage/provider/"):
            return shots.respond(r, usage)
        return shots.stub(r)

    page.route("**/api/**", route)


def overview(browser, lang: str, usage: dict, name: str):  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": 1920, "height": 1080}, color_scheme="dark")
    page = context.new_page()
    serve(page, usage)
    page.goto(f"{BASE}/agents/{shots.S1}?token=t&scheme=dark&lang={lang}&panel=overview")
    page.wait_for_selector(".panel [data-tool-page='overview'] .ov-row", timeout=15000)
    page.wait_for_timeout(600)
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.locator(".panel").screenshot(path=str(SHOTS / f"overview-{name}-{lang}.png"))
    return context, page


def run() -> int:
    problems: list[str] = []
    subscription = copy.deepcopy(shots.PROVIDER_USAGE)
    near = copy.deepcopy(shots.PROVIDER_USAGE)
    near["subscription"]["windows"][0]["used_percent"] = 86
    metered = {**copy.deepcopy(shots.PROVIDER_USAGE), "subscription": None}
    empty = copy.deepcopy(shots.PROVIDER_USAGE)
    empty["subscription"]["windows"] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            words = WORDS[lang]
            where = f"{lang} subscription"
            context, page = overview(browser, lang, subscription, "subscription")
            row = page.locator(".panel .ov-row[data-row='usage']")
            if row.count() != 1 or row.get_attribute("data-kind") != "subscription":
                problems.append(f"{where}: no subscription row ({row.count()} usage rows, kind {row.first.get_attribute('data-kind') if row.count() else None})")
            else:
                text = row.inner_text()
                if "Claude" not in text or "23%" not in text or "41%" not in text or "5h" not in text or "weekly" not in text:
                    problems.append(f"{where}: the row reads {text!r}, not Claude with both windows")
                meters = row.locator(".ov-window .ov-meter i")
                widths = [m.evaluate("e => e.getBoundingClientRect().width / e.parentElement.getBoundingClientRect().width") for m in meters.all()]
                if [round(w, 2) for w in widths] != [0.23, 0.41]:
                    problems.append(f"{where}: the meters are filled {widths}, not 23% and 41%")
                titles = [w.get_attribute("title") or "" for w in row.locator(".ov-window").all()]
                if not all(words["resets"] in title for title in titles):
                    problems.append(f"{where}: a window's tooltip does not say when it starts again ({titles})")
                if words["unmetered"] in page.locator(".panel [data-tool-page='overview']").inner_text():
                    problems.append(f"{where}: the Overview still says {words['unmetered']!r}")
                row.click()
                if page.locator(".panel .panel-tab.on[data-tab='usage']").count() != 1:
                    problems.append(f"{where}: the row does not open the Usage tool")
            context.close()

            where = f"{lang} near the limit"
            context, page = overview(browser, lang, near, "near")
            window = page.locator(".panel .ov-row[data-row='usage'] .ov-window[data-quota='5h']")
            tone = window.get_attribute("data-tone") if window.count() else None
            colour = window.locator(".ov-window-pct").evaluate("e => getComputedStyle(e).color") if window.count() else None
            attention = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--attention').trim()")
            if tone != "attn" or not colour or colour == page.locator(".panel .ov-window[data-quota='weekly'] .ov-window-pct").evaluate("e => getComputedStyle(e).color"):
                problems.append(f"{where}: the 86% window is not in the attention colour (tone {tone}, colour {colour}, --attention {attention})")
            context.close()

            where = f"{lang} metered"
            context, page = overview(browser, lang, metered, "metered")
            row = page.locator(".panel .ov-row[data-row='usage']")
            if row.count() != 1 or row.get_attribute("data-kind") != "spend":
                problems.append(f"{where}: no spend row")
            elif "$" not in row.inner_text() or words["unmetered"] in row.inner_text() or words["usage"] not in row.inner_text():
                problems.append(f"{where}: the spend row reads {row.inner_text()!r}")
            context.close()

            where = f"{lang} no windows"
            context, page = overview(browser, lang, empty, "empty")
            # The stub's chat has spent $1.84, so a spend row is right; zeros for a subscription are not.
            row = page.locator(".panel .ov-row[data-row='usage']")
            if row.count() and (row.get_attribute("data-kind") == "subscription" or "0%" in row.inner_text()):
                problems.append(f"{where}: a subscription with no windows draws {row.inner_text()!r}")
            context.close()
        browser.close()
    print("problems:", "\n  ".join(problems) if problems else "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run() or shots.UNHANDLED.report())
