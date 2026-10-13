"""A widget an agent put into its answer (the Widget tool), in the conversation, in a real browser.

The turn drew one widget: a counter kept in the widget's storage, a button that grows a list, a
button that asks a follow-up, a fetch to another site, and a line that reads the theme. The checks:

- it renders in a sandboxed frame without same-origin, under its title, after the step that drew it;
- the frame's height follows its content, and grows when the list does;
- what it stores survives a reload of the app;
- its ask puts the text into the composer and sends nothing;
- its fetch is refused by the frame's policy, and no request leaves the browser;
- it follows the app from dark to light without being reloaded;
- "Open as page" hands the widget to the host and opens the page it answers with.

Desktop and phone, English and Russian; nothing scrolls sideways.

    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_widgets.py

SHOTS=<directory> also saves the pictures this looks at. Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, WIDGET_PAGES, WIDGET_VALUES, expect_app  # noqa: E402
from pages_stub import PagesStub  # noqa: E402
from screenshots import S1, UNHANDLED, ago, detail, respond, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")

WIDGET = """<style>
.row { display:flex; gap:8px; align-items:center; margin-bottom:8px; }
button { padding:6px 12px; border:0; border-radius:var(--radius-md); background:var(--accent); color:var(--accent-fg); }
#list div { height:30px; }
</style>
<div class="row"><b id="label"></b><button id="inc">+1</button><span id="count">…</span></div>
<div class="row"><button id="grow">Grow</button><button id="ask">Ask</button></div>
<div id="list"></div>
<p id="net">pending</p><p id="look"></p>
<script>
var n = 0;
document.getElementById("label").textContent = widget.data.label;
widget.storage.get("count").then(function (v) { n = v || 0; document.getElementById("count").textContent = String(n); });
document.getElementById("inc").onclick = function () {
  n++;
  widget.storage.set("count", n).then(function () { document.getElementById("count").textContent = String(n); });
};
document.getElementById("grow").onclick = function () {
  for (var i = 0; i < 10; i++) { var d = document.createElement("div"); d.textContent = "row " + i; document.getElementById("list").appendChild(d); }
};
document.getElementById("ask").onclick = function () {
  widget.ask("Why is the count " + n + "?").then(function () { document.getElementById("ask").dataset.done = "1"; });
};
fetch("https://example.com/collect?x=1").then(function () { document.getElementById("net").textContent = "reached"; },
                                               function () { document.getElementById("net").textContent = "blocked"; });
function paint() {
  document.getElementById("look").textContent = widget.theme.scheme + " " + getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
}
paint();
widget.onTheme(paint);
</script>"""

WORDS = {
    "en": {"title": "Counter", "page": "Open as page"},
    "ru": {"title": "Counter", "page": "Открыть как страницу"},
}


def turn_messages() -> list[dict]:
    calls = [{"id": "w1", "name": "Widget", "arguments": {"title": "Counter", "html": WIDGET, "data": {"label": "Cups"}}}]
    results = [{"id": "w1", "content": "The widget 'Counter' (2 KB) is in the conversation now.", "is_error": False}]
    return [
        {"role": "user", "seq": 801, "text": "Give me a counter for the cups.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=6)},
        {"role": "assistant", "seq": 802, "text": "", "thinking": "", "tool_calls": calls, "tool_results": [], "created_at": ago(minutes=5)},
        {"role": "tool", "seq": 803, "text": "", "thinking": "", "tool_calls": [], "tool_results": results, "created_at": ago(minutes=4)},
        {"role": "assistant", "seq": 804, "text": "Tap +1 for every cup.", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": ago(minutes=3)},
    ]


def routes(page: Page, pages: PagesStub, app_root: str, outside: list[str]) -> None:
    def api(r) -> None:  # type: ignore[no-untyped-def]
        url = r.request.url.split("?", 1)[0]
        rel = url[url.index("/api/"):]
        if rel == f"/api/sessions/{S1}" and r.request.method == "GET":
            return respond(r, {**detail(S1), "messages": turn_messages()})
        if pages.fulfil(r):
            return None
        return stub(r)

    def own(r) -> None:  # type: ignore[no-untyped-def]
        if "/api/" in r.request.url:
            r.fallback()
        else:
            pages.serve(r, app_root)

    page.route("**/api/**", api)
    page.route("**/pages/**", own)
    page.on("request", lambda request: outside.append(request.url) if "example.com" in request.url else None)


def frame_height(page: Page) -> float:
    box = page.locator(".widget-frame").bounding_box()
    return box["height"] if box else 0.0


def run() -> int:
    expect_app(BASE)
    app_root = BASE.rstrip("/")
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        for language in ("en", "ru"):
            words = WORDS[language]
            for width, height, mobile in ((1440, 900, False), (390, 844, True)):
                where = f"{language} {width}px"
                WIDGET_VALUES.clear()
                WIDGET_PAGES.clear()
                outside: list[str] = []
                context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile, color_scheme="dark")
                page = context.new_page()
                page.set_default_timeout(10000)
                routes(page, PagesStub(), app_root, outside)
                page.goto(f"{BASE}/agents/{S1}?token=t&scheme=auto&lang={language}")
                page.wait_for_selector(".chat-scroll .timeline .answer", timeout=15000)
                card = page.locator(".widget-card[data-widget='w1']")
                expect(card).to_have_count(1)
                expect(card.locator(".widget-title")).to_have_text(words["title"])
                expect(card.locator(".widget-page")).to_have_attribute("title", words["page"])
                frame_el = card.locator("iframe.widget-frame")
                if frame_el.get_attribute("sandbox") != "allow-scripts":
                    problems.append(f"{where}: the frame's sandbox is {frame_el.get_attribute('sandbox')!r}")
                widget = page.frame_locator(".widget-card[data-widget='w1'] iframe")
                expect(widget.locator("#label")).to_have_text("Cups")
                expect(widget.locator("#count")).to_have_text("0")
                # The widget comes before the answer that follows it.
                order = page.evaluate("""() => {
                    const card = document.querySelector('.widget-card');
                    const answers = [...document.querySelectorAll('.answer')];
                    return answers.some((a) => card.compareDocumentPosition(a) & Node.DOCUMENT_POSITION_FOLLOWING);
                }""")
                if not order:
                    problems.append(f"{where}: the answer after the widget is drawn above it")

                # No network: the fetch is refused inside the frame and never leaves the browser.
                expect(widget.locator("#net")).to_have_text("blocked")
                if outside:
                    problems.append(f"{where}: the widget reached the network: {outside}")

                # The height follows the content.
                page.wait_for_function("() => { const f = document.querySelector('.widget-frame'); return f && f.getBoundingClientRect().height > 100; }")
                before = frame_height(page)
                inner = widget.locator("body").evaluate("b => Math.ceil(document.documentElement.getBoundingClientRect().height)")
                if abs(before - inner) > 2:
                    problems.append(f"{where}: the frame is {before}px for {inner}px of content")
                widget.locator("#grow").click()
                page.wait_for_function(f"() => document.querySelector('.widget-frame').getBoundingClientRect().height > {before + 250}")
                grown = frame_height(page)
                scrolls = widget.locator("body").evaluate("b => document.documentElement.scrollHeight - innerHeight")
                if scrolls > 2:
                    problems.append(f"{where}: the frame grew to {grown}px but its content still scrolls by {scrolls}px")

                # Storage: two taps, kept by the host, still there after a reload.
                widget.locator("#inc").click()
                expect(widget.locator("#count")).to_have_text("1")
                widget.locator("#inc").click()
                expect(widget.locator("#count")).to_have_text("2")
                if WIDGET_VALUES.get((S1, "w1")) != {"count": 2}:
                    problems.append(f"{where}: the host keeps {WIDGET_VALUES.get((S1, 'w1'))}")

                # Ask fills the composer and sends nothing.
                widget.locator("#ask").click()
                expect(widget.locator("#ask")).to_have_attribute("data-done", "1")
                field = page.locator(".composer textarea").first
                expect(field).to_have_value("Why is the count 2?")

                # The theme: dark now, light when the system turns light, without a reload.
                expect(widget.locator("#look")).to_contain_text("dark")
                dark_bg = widget.locator("#look").inner_text().split(" ", 1)[1]
                widget.locator("#label").evaluate("el => { el.dataset.marker = 'kept'; }")
                page.emulate_media(color_scheme="light")
                expect(widget.locator("#look")).to_contain_text("light")
                light_bg = widget.locator("#look").inner_text().split(" ", 1)[1]
                if light_bg == dark_bg:
                    problems.append(f"{where}: the widget's --bg stayed {dark_bg} in the light scheme")
                expect(widget.locator("#label")).to_have_attribute("data-marker", "kept")

                overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
                if overflow > 0:
                    problems.append(f"{where}: the chat scrolls sideways by {overflow}px with the widget")
                box = card.bounding_box()
                if box is None or box["x"] < 0 or box["x"] + box["width"] > width:
                    problems.append(f"{where}: the widget is outside the window: {box}")
                if SHOTS:
                    Path(SHOTS).mkdir(parents=True, exist_ok=True)
                    card.scroll_into_view_if_needed()
                    page.screenshot(path=str(Path(SHOTS) / f"widget-{language}-{width}.png"))

                page.reload()
                page.wait_for_selector(".chat-scroll .timeline .answer", timeout=15000)
                widget = page.frame_locator(".widget-card[data-widget='w1'] iframe")
                expect(widget.locator("#count")).to_have_text("2")

                # Open as page: the host is handed the widget, and the app goes to the page it made.
                page.locator(".widget-card[data-widget='w1'] .widget-page").click()
                page.wait_for_url("**/app/p/orders**")
                if not WIDGET_PAGES or WIDGET_PAGES[-1].get("title") != "Counter" or "widget.storage" not in str(WIDGET_PAGES[-1].get("html")):
                    problems.append(f"{where}: the page was asked for with {WIDGET_PAGES[-1:] or 'nothing'}")
                print(f"{where}: drawn, sized, kept, asked, offline, themed, made a page")
                context.close()
        browser.close()
    for problem in problems:
        print("PROBLEM:", problem)
    unhandled = UNHANDLED.report()
    return 1 if problems else unhandled


if __name__ == "__main__":
    sys.exit(run())
