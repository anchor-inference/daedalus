"""Opening a step of an earlier turn leaves the reader where they were.

What the operator saw: in a conversation long enough to scroll, opening the "Worked for …" line of a
turn that is not the last one threw the chat down to its end. The pin to the end was still held when
the reader had got there by any way but a wheel or a finger, and the list growing under the opened
line was read as new output to follow.

In a session and in a project orchestrator's chat, at 1440 px and on a 390 px phone, the reader goes
up to an earlier turn by each of the ways a reader does — a wheel, the scrollbar or the keyboard on a
desktop, a jump that sets the position (a link, find in page) on either — and opens the work of a turn
there. The scroll position must not move by more than a few pixels, and the line that was clicked must
stay where it was on the screen. Opening the last turn's work while at the end must not move the line
either. Each chat must still open at its end.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_expand_scroll.py

Exit 0 when no opened step moves the reader.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from check_orchestration_mode import PID, go, serve, stubs  # noqa: E402
from screenshots import S1, UNHANDLED, detail, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

MEASURE = "(() => { const el = document.querySelector('.chat-scroll'); return { top: el.scrollTop, gap: el.scrollHeight - el.scrollTop - el.clientHeight, height: el.clientHeight }; })()"

# Marks the first folded "Worked for …" line wholly inside the upper part of the screen, and says where
# it is; the last one in the list when `last` is set.
PICK = """(last) => {
  const el = document.querySelector('.chat-scroll');
  const box = el.getBoundingClientRect();
  document.querySelectorAll('[data-picked]').forEach((n) => n.removeAttribute('data-picked'));
  const heads = [...el.querySelectorAll('.thinking-head[aria-expanded="false"]')];
  const seen = heads.filter((h) => { const r = h.getBoundingClientRect(); return r.top > box.top + 8 && r.bottom < box.top + box.height * (last ? 0.95 : 0.85); });
  const head = last ? seen[seen.length - 1] : seen[0];
  if (!head) return null;
  head.setAttribute('data-picked', '1');
  return { y: head.getBoundingClientRect().top - box.top, isLast: head === heads[heads.length - 1] };
}"""
WHERE = "(() => { const el = document.querySelector('.chat-scroll'); const h = el.querySelector('[data-picked]'); return h ? h.getBoundingClientRect().top - el.getBoundingClientRect().top : null; })()"


def worked(first: int, pairs: int) -> list[dict]:
    """A history whose every answer came after a few steps, so each turn has a folded line of work."""
    out: list[dict] = []
    for i in range(pairs):
        seq = first + 5 * i
        at = "2026-09-25T08:00:00Z"
        out.append({"role": "user", "seq": seq, "origin": "operator", "text": f"Question {i}: " + "what about this part " * 6, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": at})
        calls = [{"id": f"c{seq}-{k}", "name": "Exec", "arguments": {"command": f"grep -rn part{k} src/ | head -40"}} for k in range(3)]
        out.append({"role": "assistant", "seq": seq + 1, "text": "", "thinking": "Reading the sources is enough to tell.", "tool_calls": calls, "tool_results": [], "created_at": at})
        result = "".join(f"src/part{i}.py:{n}: match\n" for n in range(30))
        out.append({"role": "tool", "seq": seq + 2, "text": "", "thinking": "", "tool_calls": [], "tool_results": [{"id": c["id"], "content": result, "is_error": False, "length": len(result)} for c in calls], "created_at": at})
        out.append({"role": "assistant", "seq": seq + 3, "text": f"Answer {i}.\n\n" + "A paragraph of the answer that takes a few lines on any screen. " * 5, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2026-09-25T08:00:09Z"})
    return out


def session_route(route) -> None:  # type: ignore[no-untyped-def]
    """The stubbed installation, with the first session's history made long and full of work."""
    path = route.request.url.split("?", 1)[0]
    if route.request.method == "GET" and path.endswith(f"/api/sessions/{S1}"):
        body = detail(S1)
        body["messages"] = worked(10, 80)
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    return stub(route)


def open_session(context) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.route("**/api/**", session_route)
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=en")
    page.wait_for_selector(".chat-scroll .timeline .thinking-head", timeout=15000)
    return page


def open_orchestrator(context) -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    focus, main = stubs("en")
    held = focus.details["orch-bakery"]
    # The worked turns come last, where the reader starts: the stub's own newest messages (a card of a
    # member's steps, receipts under the operator's words) fill a phone's screen with no folded line.
    held["messages"] = [{**m, "seq": 1 + n} for n, m in enumerate(held["messages"])] + worked(1000, 80)
    serve(page, focus, main, "en")
    go(page, f"/orchestration/project/{PID}", "en")
    page.wait_for_selector(".chat-scroll .timeline .thinking-head", timeout=15000)
    return page


def up(page: Page, how: str) -> None:
    """Take the reader some screens up, the way `how` names."""
    box = page.locator(".chat-scroll").bounding_box()
    assert box
    if how == "wheel":
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, -2500)
    elif how == "scrollbar":
        # A headless browser draws no scrollbar to take hold of. What the page hears of a drag is a
        # press on the list, a run of scroll events and a release, with no wheel among them.
        page.evaluate("""async () => {
          const el = document.querySelector('.chat-scroll');
          el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
          for (let i = 0; i < 10; i++) { el.scrollTop -= 250; await new Promise((r) => requestAnimationFrame(() => r())); }
          el.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
          el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
        }""")
    elif how == "keyboard":
        # A click on the list's empty margin gives it the keyboard without opening anything.
        page.mouse.click(box["x"] + 6, box["y"] + box["height"] / 2)
        for _ in range(4):
            page.keyboard.press("PageUp")
            page.wait_for_timeout(150)
    elif how == "jump":
        page.evaluate("(() => { const el = document.querySelector('.chat-scroll'); el.scrollTop = el.scrollTop - 2500; })()")
    page.wait_for_timeout(1200)


def expand(page: Page, where: str, *, last: bool = False) -> list[str]:
    before = page.evaluate(MEASURE)
    picked = page.evaluate(PICK, last)
    if not picked:
        return [f"{where}: no folded line of work on the screen to open"]
    page.locator("[data-picked]").click()
    page.wait_for_timeout(1200)
    after = page.evaluate(MEASURE)
    y = page.evaluate(WHERE)
    print(where, "before", before, "line at", round(picked["y"]), "after", after, "line at", y if y is None else round(y))
    problems: list[str] = []
    if not last and abs(after["top"] - before["top"]) > 4:
        problems.append(f"{where}: opening the step moved the chat from {before['top']:.0f} to {after['top']:.0f}")
    if y is None or abs(y - picked["y"]) > 4:
        problems.append(f"{where}: the opened line went from {picked['y']:.0f}px to {y}px on the screen")
    if not last and before["gap"] < 48:
        problems.append(f"{where}: the reader was never taken up (gap {before['gap']:.0f}px)")
    return problems


def check(context, where: str, opener, ways: tuple[str, ...]) -> list[str]:  # type: ignore[no-untyped-def]
    problems: list[str] = []
    for how in ways:
        page = opener(context)
        page.wait_for_timeout(1800)
        opened = page.evaluate(MEASURE)
        if opened["gap"] > 48:
            problems.append(f"{where}: the chat opened {opened['gap']:.0f}px above its end")
        up(page, how)
        problems += expand(page, f"{where} after {how}")
        page.close()
    # At the end: the last turn's work opens under the reader's eye and the line stays put.
    page = opener(context)
    page.wait_for_timeout(1800)
    problems += expand(page, f"{where} at the end", last=True)
    page.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for label, viewport, mobile, ways in (
            ("desktop", {"width": 1440, "height": 900}, False, ("wheel", "scrollbar", "keyboard", "jump")),
            ("phone", {"width": 390, "height": 844}, True, ("wheel", "jump")),
        ):
            for chat, opener in (("session", open_session), ("orchestrator", open_orchestrator)):
                context = browser.new_context(viewport=viewport, is_mobile=mobile, has_touch=mobile, color_scheme="dark")
                context.add_init_script("try { localStorage.setItem('daedalus.mode', 'orchestration'); } catch (e) {}" if chat == "orchestrator" else "")
                problems += check(context, f"{label} {chat}", opener, ways)
                context.close()
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = run()
    sys.exit(failed or UNHANDLED.report())
