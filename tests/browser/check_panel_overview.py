"""The panel's Overview says what the chat's subscription has left, and Details is a tab beside it.

The Overview is the panel's front page. Its Usage row once said "$0 · today unmetered" for a model on a
subscription, then a row of chips that a provider with three long window names cut to letters
("Google (Anti…  Gemi… ▬ 6%"). Now:

- a chat whose model runs on a subscription has a block under the rows: the provider by its whole name
  with its plan, a link to the Usage tool, and one line a window with the window's whole name ("5 hours",
  "Weekly", "Gemini 4 Argon (high)"), a meter filled to its share, the share, and when it resets in words;
  a window near its limit is in the attention colour; nothing in it is cut at 520 px, 410 px or on a phone;
- a metered model keeps its spend as a row, without the "today unmetered" words;
- a subscription the host has no windows for draws nothing rather than zeros.

Details was reached only through "+". It is now the panel's second tab in every ordinary chat, until the
reader closes it: the closing is remembered across chats and reloads, and the Overview's head then offers
a quiet "Details" link that brings the tab back in its place. The strip still fits at 1366.

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
WORDS = {"en": {"resets": "Resets", "in": "in ", "unmetered": "unmetered", "usage": "Usage", "hours": "5 hours", "weekly": "Weekly"},
         "ru": {"resets": "Обновится", "in": "через ", "unmetered": "без оплаты", "usage": "Расход", "hours": "5 часов", "weekly": "Неделя"}}
# Three windows with long names, the way Google's subscription reports them through the key proxy.
GOOGLE_WINDOWS = [("Gemini 3.5 Pro (high and low)", 6), ("Gemini 4 Argon (preview, shared)", 6), ("Claude / GPT-OSS on Antigravity", 0)]
GOOGLE = {"provider": "antigravity", "today": {"calls": 4, "input_tokens": 1000, "output_tokens": 200, "cost_usd": 0},
          "subscription": {"logged_in": True, "plan": "pro", "windows": [{"name": name, "used_percent": used, "resets_at": shots.ahead(hours=2, minutes=14 + i)} for i, (name, used) in enumerate(GOOGLE_WINDOWS)]},
          "balance": None}
# The browser's own measure of a cut: text wider than its box, or a box that clips its first line.
CUT = "e => e.scrollWidth > e.clientWidth + 1"
# The panel beside the chat, or the sheet it is on a phone.
P = ":is(.panel, .panel-sheet)"


def serve(page, usage: dict, detail=None) -> None:  # type: ignore[no-untyped-def]
    def route(r) -> None:  # type: ignore[no-untyped-def]
        path = urlsplit(r.request.url).path
        path = path[path.index("/api/"):]
        if path.startswith("/api/usage/provider/"):
            return shots.respond(r, usage)
        if detail is not None and path == f"/api/sessions/{shots.S1}" and r.request.method == "GET":
            return shots.respond(r, detail(shots.detail(shots.S1)))
        return shots.stub(r)

    page.route("**/api/**", route)


def overview(browser, lang: str, usage: dict, name: str, *, size=(1920, 1080), detail=None):  # type: ignore[no-untyped-def]
    touch = size[0] < 600
    context = browser.new_context(viewport={"width": size[0], "height": size[1]}, color_scheme="dark", is_mobile=touch, has_touch=touch)
    page = context.new_page()
    serve(page, usage, detail)
    page.goto(f"{BASE}/agents/{shots.S1}?token=t&scheme=dark&lang={lang}&panel=overview")
    page.wait_for_selector(f"{P} [data-tool-page='overview'] .ov-row", timeout=15000)
    page.wait_for_timeout(600)
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.locator(P).screenshot(path=str(SHOTS / f"overview-{name}-{lang}.png"))
    return context, page


def tabs(page) -> list[str]:  # type: ignore[no-untyped-def]
    return page.locator(".panel .panel-tablist .panel-tab").evaluate_all("els => els.map((e) => e.dataset.tab)")


def details_tab(browser, lang: str) -> list[str]:  # type: ignore[no-untyped-def]
    where = f"{lang} 1366 details"
    words = {"en": "Details", "ru": "Подробнее"}[lang]
    problems: list[str] = []
    context = browser.new_context(viewport={"width": 1366, "height": 768}, color_scheme="dark")
    page = context.new_page()
    serve(page, shots.PROVIDER_USAGE)
    page.goto(f"{BASE}/agents/{shots.S1}?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".panel [data-tool-page='overview']", timeout=15000)
    page.wait_for_timeout(400)
    order = tabs(page)
    if order[:2] != ["overview", "details"]:
        problems.append(f"{where}: a fresh panel's tabs are {order}, not the Overview then Details")
    strip = page.locator(".panel .panel-tablist").evaluate("e => ({ over: e.scrollWidth - e.clientWidth, right: e.getBoundingClientRect().right, tabs: [...e.querySelectorAll('.panel-tab')].map((t) => t.getBoundingClientRect().right) })")
    if strip["over"] > 0 or any(right > strip["right"] + 0.5 for right in strip["tabs"]):
        problems.append(f"{where}: the strip does not fit ({strip})")
    if page.locator(".panel .ov-details-link").count():
        problems.append(f"{where}: the Overview offers Details while its tab is there")
    # Closed by the reader: gone in this chat and in the next, with the Overview's link in its place.
    page.locator(".panel .panel-tab[data-tab='details']").click()
    page.locator(".panel .panel-tab-close").click()
    page.wait_for_timeout(200)
    page.goto(f"{BASE}/agents/{shots.S2}?token=t&scheme=dark&lang={lang}&panel=overview")
    page.wait_for_selector(".panel [data-tool-page='overview']", timeout=15000)
    page.wait_for_timeout(400)
    if "details" in tabs(page):
        problems.append(f"{where}: Details came back in the next chat after it was closed ({tabs(page)})")
    link = page.locator(".panel .ov-head .ov-details-link")
    if link.count() != 1 or link.inner_text().strip() != words:
        problems.append(f"{where}: the Overview's head has no {words!r} link once Details is closed")
    else:
        if SHOTS is not None:
            page.locator(".panel").screenshot(path=str(SHOTS / f"overview-details-closed-{lang}.png"))
        link.click()
        page.wait_for_selector(".panel .panel-tab.on[data-tab='details']", timeout=5000)
        if tabs(page)[:2] != ["overview", "details"]:
            problems.append(f"{where}: the link put Details at {tabs(page)}, not second")
    context.close()
    return problems


def usage_block(page, where: str, words: dict, provider: str, windows: list, plan: str = "Max") -> list[str]:  # type: ignore[no-untyped-def]
    """The subscription's block: the provider whole, its plan, one line a window with the window's whole
    name, a meter filled to its share, the share, and the reset in words, nothing cut or past the panel."""
    problems: list[str] = []
    block = page.locator(f"{P} .ov-usage[data-usage='subscription']")
    if block.count() != 1:
        return [f"{where}: no subscription block ({block.count()})"]
    name = block.locator(".ov-usage-name")
    if name.inner_text().strip() != provider or name.evaluate(CUT):
        problems.append(f"{where}: the provider reads {name.inner_text()!r}, not {provider!r} whole")
    if plan not in block.locator(".ov-usage-head").inner_text():
        problems.append(f"{where}: the plan {plan!r} is not in the heading ({block.locator('.ov-usage-head').inner_text()!r})")
    rows = block.locator(".qrow")
    if rows.count() != len(windows):
        return problems + [f"{where}: {rows.count()} window lines, not {len(windows)}"]
    panel = page.locator(P).bounding_box()
    for row, (label, share) in zip(rows.all(), windows, strict=True):
        shown = row.locator(".qrow-name")
        if shown.inner_text().strip() != label or shown.evaluate(CUT):
            problems.append(f"{where}: a window reads {shown.inner_text()!r}, not {label!r} whole")
        fill = row.locator(".qrow-meter i").evaluate("e => e.getBoundingClientRect().width / e.parentElement.getBoundingClientRect().width")
        if abs(fill - share) > 0.01:
            problems.append(f"{where}: {label!r} is filled {fill:.2f}, not {share}")
        if f"{round(share * 100)}%" not in row.locator(".qrow-pct").inner_text():
            problems.append(f"{where}: {label!r} shows {row.locator('.qrow-pct').inner_text()!r}")
        reset = row.locator(".qrow-reset").inner_text().strip()
        if not reset or words["resets"] not in (row.get_attribute("title") or ""):
            problems.append(f"{where}: {label!r} does not say when it resets ({reset!r}, title {row.get_attribute('title')!r})")
        for part in (".qrow-name", ".qrow-meter", ".qrow-pct", ".qrow-reset"):
            box = row.locator(part).bounding_box()
            if not box or box["x"] < panel["x"] or box["x"] + box["width"] > panel["x"] + panel["width"] + 0.5:
                problems.append(f"{where}: {label!r}'s {part} stands outside the panel ({box})")
        if row.locator(".qrow-reset").evaluate(CUT):
            problems.append(f"{where}: {label!r}'s reset is cut")
    if SHOTS is not None:
        block.screenshot(path=str(SHOTS / f"usage-{where.replace(' ', '-')}.png"))
    return problems


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
            problems += details_tab(browser, lang)
            words = WORDS[lang]
            where = f"{lang} subscription"
            context, page = overview(browser, lang, subscription, "subscription")
            problems += usage_block(page, where, words, "Claude", [(words["hours"], 0.23), (words["weekly"], 0.41)])
            if page.locator(".panel .ov-row[data-row='usage']").count():
                problems.append(f"{where}: a usage row is drawn beside the subscription's block")
            if words["unmetered"] in page.locator(".panel [data-tool-page='overview']").inner_text():
                problems.append(f"{where}: the Overview still says {words['unmetered']!r}")
            link = page.locator(".panel .ov-usage .ov-usage-open")
            if link.count() != 1:
                problems.append(f"{where}: the block has no way to the Usage tool")
            else:
                link.click()
                if page.locator(".panel .panel-tab.on[data-tab='usage']").count() != 1:
                    problems.append(f"{where}: the block's link does not open the Usage tool")
                elif page.locator(".panel [data-page='provider'] .qrow").count() != 2:
                    problems.append(f"{where}: the Usage tool does not draw the windows with the same lines")
            context.close()

            # Google's subscription: a long provider name and three long window names, none of them cut
            # in the 520 px panel, the 410 px one or the phone's sheet.
            for size, name in (((1920, 1080), "google"), ((1366, 768), "google-1366"), ((390, 844), "google-phone")):
                where = f"{lang} {name}"
                context, page = overview(browser, lang, GOOGLE, name, size=size, detail=lambda d: {**d, "provider": "antigravity", "model": "gemini-3.5-pro"})
                problems += usage_block(page, where, words, "Google (Antigravity)", [(label, used / 100) for label, used in GOOGLE_WINDOWS], plan="Pro")
                context.close()

            where = f"{lang} near the limit"
            context, page = overview(browser, lang, near, "near")
            window = page.locator(".panel .ov-usage .qrow[data-quota='5h']")
            tone = window.get_attribute("data-tone") if window.count() else None
            colour = window.locator(".qrow-pct").evaluate("e => getComputedStyle(e).color") if window.count() else None
            attention = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--attention').trim()")
            if tone != "attn" or not colour or colour == page.locator(".panel .ov-usage .qrow[data-quota='weekly'] .qrow-pct").evaluate("e => getComputedStyle(e).color"):
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
            if page.locator(".panel .ov-usage").count() or (row.count() and "0%" in row.inner_text()):
                problems.append(f"{where}: a subscription with no windows draws {page.locator('.panel [data-tool-page=overview]').inner_text()!r}")
            context.close()
        browser.close()
    print("problems:", "\n  ".join(problems) if problems else "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run() or shots.UNHANDLED.report())
