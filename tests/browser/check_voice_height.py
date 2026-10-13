"""The transcript on the voice page fills its box and scrolls inside it.

Opened over a long conversation the chat block used to take its natural height, so the page grew to
whatever six hundred turns came to: the window scrolled, and the floating microphone and the agents
panel — both anchored to the bottom of an area that was now taller than the screen — went off the
bottom of it. A transcript is read by scrolling the transcript, not the page.

So the conversation here is deliberately long, and what is asserted is the shape of the result: the
document does not scroll at all, the transcript's own scroller does, and both controls are inside
the viewport. On a wide window and on a phone, because the two are laid out by different rules.

    cd miniapp && npm run build
    python3 tests/browser/serve_app.py 8203 /tmp/app-root &
    APP_URL=http://127.0.0.1:8203/app python3 tests/browser/check_voice_height.py

Exit status is the number of checks that failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = shots.BASE

LINE = (
    "The seasonal section now reads from data/menu.json, so the owner edits one file and the three "
    "pages follow. I have left the old markup in place for one release in case the shop wants it back."
)


def long_conversation(session_id: str, turns: int = 160) -> dict:
    """A session detail whose transcript is far taller than any window it can be drawn in."""
    base = shots.detail(session_id)
    messages = []
    for i in range(turns):
        messages.append({"role": "user", "seq": i * 2 + 1, "text": f"Turn {i + 1}: what happened to the seasonal section?", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": shots.ago(hours=2)})
        messages.append({"role": "assistant", "seq": i * 2 + 2, "text": f"{LINE} ({i + 1})", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": shots.ago(hours=2)})
    return {**base, "messages": messages, "context": {**base["context"], "messages": len(messages)}}


class Wire:
    """The stub table, with one long transcript in it: the concierge's own session."""

    def __init__(self) -> None:
        self.session = shots.VOICE["session_id"]

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        rel = request.url.split("?", 1)[0]
        rel = rel[rel.index("/api/") :]
        if request.method == "GET" and rel == f"/api/sessions/{self.session}":
            return shots.respond(route, long_conversation(self.session))
        return shots.stub(route)


def measure(page) -> dict:  # type: ignore[no-untyped-def]
    return page.evaluate("""() => {
      const doc = document.scrollingElement;
      const main = document.querySelector('.main');
      const scroll = document.querySelector('.voice-read .chat-scroll');
      const box = s => { const el = document.querySelector(s); if (!el) return null; const r = el.getBoundingClientRect(); return {top: r.top, bottom: r.bottom, left: r.left, right: r.right}; };
      return {
        view: {w: window.innerWidth, h: window.innerHeight},
        doc: {scroll: doc.scrollHeight, client: doc.clientHeight},
        main: {scroll: main.scrollHeight, client: main.clientHeight},
        read: scroll ? {scroll: scroll.scrollHeight, client: scroll.clientHeight, top: scroll.scrollTop} : null,
        mic: box('.voice-mic-float'),
        agents: box('.voice-agents'),
      };
    }""")


def open_transcript(browser, viewport: dict):  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport=viewport, color_scheme="dark", permissions=["microphone"])
    page = context.new_page()
    page.route("**/api/**", Wire().route)
    page.goto(f"{BASE}/companion?token=t&scheme=dark&lang=en")
    shots.stage(page)
    page.wait_for_selector(".voice-stage", timeout=15000)
    page.get_by_role("button", name="Transcript").click()
    page.wait_for_selector(".voice-read .chat-scroll", timeout=15000)
    page.wait_for_timeout(900)
    return context, page


def check_width(browser, check, label: str, viewport: dict) -> None:  # type: ignore[no-untyped-def]
    context, page = open_transcript(browser, viewport)
    m = measure(page)
    h = m["view"]["h"]
    # A couple of pixels of slack: a sub-pixel border on a scaled layout is not a scrolling page.
    check(m["doc"]["scroll"] <= m["doc"]["client"] + 2, f"{label}: the document does not scroll ({m['doc']['scroll']} tall in {m['doc']['client']})")
    check(m["main"]["scroll"] <= m["main"]["client"] + 2, f"{label}: nor does the screen behind the page ({m['main']['scroll']} in {m['main']['client']})")
    assert m["read"]
    check(m["read"]["scroll"] > m["read"]["client"] + 50, f"{label}: the transcript's own box is the thing that scrolls ({m['read']['scroll']} in {m['read']['client']})")
    check(m["read"]["client"] > 200, f"{label}: and it is given real room to be read in ({m['read']['client']}px)")
    # A box that scrolls is only half of it: it has to actually scroll when asked. Upwards, because
    # a transcript opens at the newest turn and is already as far down as it goes.
    over = page.locator(".voice-read .chat-scroll").bounding_box()
    assert over
    page.mouse.move(over["x"] + over["width"] / 2, over["y"] + over["height"] / 2)
    page.mouse.wheel(0, -600)
    page.wait_for_timeout(300)
    after = measure(page)
    check(after["read"]["top"] != m["read"]["top"], f"{label}: the wheel moves the transcript ({m['read']['top']} → {after['read']['top']})")
    check(after["doc"]["scroll"] <= after["doc"]["client"] + 2, f"{label}: and the page stays put while it does")
    mic = after["mic"]
    check(bool(mic) and mic["bottom"] <= h + 1 and mic["top"] >= 0, f"{label}: the microphone is on the screen ({mic})")
    agents = after["agents"]
    if viewport["width"] > 900:
        check(bool(agents) and agents["top"] >= 0 and agents["top"] < h, f"{label}: the agents panel is on the screen ({agents})")
    else:
        # On a phone the panel is a sheet that rises from a key in the breadcrumb; closed, it is
        # meant to be off the bottom of the screen, and what matters is that the key is reachable.
        key = page.locator(".voice-panel-key").bounding_box()
        check(bool(key) and key["y"] >= 0 and key["y"] + key["height"] <= h + 1, f"{label}: the key that raises the agents panel is on the screen ({key})")
    context.close()


def main() -> int:
    failures = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failures
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures += 1

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=shots.CHROMIUM, args=shots.FAKE_MEDIA)
        check_width(browser, check, "desktop", {"width": 1440, "height": 900})
        check_width(browser, check, "wide", {"width": 2560, "height": 1400})
        check_width(browser, check, "phone", {"width": 390, "height": 844})
        browser.close()
    return failures


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(main())
