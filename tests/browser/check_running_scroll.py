"""While a run is on, what the operator scrolled or selected stays where they put it.

What the operator saw: with an agent running, the expanded card of the running command jumped back
to its start every second or two after being scrolled sideways, and a file open in the right-hand
panel lost its scroll position and, more often, the text selected in it. The live turn re-renders
every second for its elapsed time and the session screen on every event and poll, and each render
handed React 19 a new ``{ __html }`` object, which it writes into the page whatever the string in
it: the card's and the file's nodes were thrown away and built again from the same text.

A running session, its stream held open and fed a thinking delta and a change of state every
second, the second making the screen read the session again:

- the running command's card, scrolled sideways, keeps its ``<pre>`` node and its offset
  over several ticks;
- a source file open in the panel, scrolled and with a selection in it, keeps its nodes, its scroll
  position and the selected text over several ticks;
- the same for a markdown file, the other kind drawn from a string.

    cd miniapp && npm run build
    APP_URL=http://127.0.0.1:<port>/app CHROMIUM=... python3 tests/browser/check_running_scroll.py

Exit 0 when nothing the operator set is reset.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import FILE_TEXT, expect_app  # noqa: E402
from screenshots import BASE, CHROMIUM, S1, UNHANDLED, stub  # noqa: E402

TICKS = 4
"""Seconds watched: the clock that redrew the card beats once a second."""

STREAM = """const originalFetch = window.fetch;
  window.fetch = (input, init) => String(input).includes('/stream')
    ? Promise.resolve(new Response(new ReadableStream({start(controller) {
        window.sendEvent = (name, payload) => controller.enqueue(new TextEncoder().encode(
          'event: ' + name + '\\ndata: ' + JSON.stringify(payload) + '\\n\\n'));
      }}), {headers: {'Content-Type': 'text/event-stream'}}))
    : originalFetch(input, init);"""

# A command long both ways, as the agent writes a build script inline: one line wider than any card
# and more lines than the card shows.
COMMAND = "set -e; for page in site/*.html; do " + " ".join(f"check-step-{n} \"$page\"" for n in range(40)) + "; done\n" + "\n".join(f"echo 'stage {n}: ' && make stage-{n} " + "--flag " * 30 for n in range(40))

SOURCE = "src/build_report.py"
FILE_TEXT[SOURCE] = "".join(f"result_{n:04d} = compute_stage({n}, label='stage {n}', notes='" + "a note that runs past the panel edge " * 4 + "')\n" for n in range(300))

NOTES = "docs/stage_notes.md"
FILE_TEXT[NOTES] = "# Stage notes\n\n" + "".join(f"## Stage {n}\n\nWhat stage {n} checks, and the note the agent left about it for the next run to read.\n\n" for n in range(60))

# Marks every node under `root` and finds the nearest scrolling box, so a later look can tell
# whether the nodes are the same ones and whether the scroll is where it was put.
MARK = """([sel, left, top]) => {
  const root = document.querySelector(sel);
  if (!root) return null;
  const nodes = [root, ...root.querySelectorAll('*')];
  window.__marked = nodes;
  // The box that scrolls the way asked: a code card scrolls sideways itself, a file in the panel
  // scrolls up and down in a box around it.
  const scrolls = (el) => { const style = getComputedStyle(el); return left
    ? el.scrollWidth > el.clientWidth + 4 && /auto|scroll/.test(style.overflowX)
    : el.scrollHeight > el.clientHeight + 4 && /auto|scroll/.test(style.overflowY); };
  let box = root;
  while (box && !scrolls(box)) box = box.parentElement;
  if (!box) return null;
  box.setAttribute('data-watched', '1');
  if (left) box.scrollLeft = left;
  if (top) box.scrollTop = top;
  return { nodes: nodes.length, left: box.scrollLeft, top: box.scrollTop };
}"""

LOOK = """() => {
  const box = document.querySelector('[data-watched]');
  return {
    replaced: (window.__marked || []).filter((n) => !n.isConnected).length,
    left: box ? box.scrollLeft : -1, top: box ? box.scrollTop : -1,
    selected: String(document.getSelection() || ''),
  };
}"""

# Selects a run of text inside the second text node under `sel` that holds at least 40 characters.
SELECT = """(sel) => {
  const root = document.querySelector(sel);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) if (n.textContent.length >= 40) texts.push(n);
  const text = texts[1] || texts[0];
  if (!text) return '';
  const range = document.createRange();
  range.setStart(text, 4);
  range.setEnd(text, 36);
  const sel_ = document.getSelection();
  sel_.removeAllRanges();
  sel_.addRange(range);
  return String(sel_);
}"""


def open_running(context, query: str = "") -> Page:  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.add_init_script(STREAM)
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang=en{query}")
    page.wait_for_function("typeof window.sendEvent === 'function'", timeout=15000)
    page.wait_for_selector(".chat-scroll .timeline", timeout=15000)
    return page


def event(page: Page, name: str, payload: dict) -> None:
    page.evaluate(f"window.sendEvent({json.dumps(name)}, {json.dumps({'run_id': 'r-modes', **payload})})")


def watch(page: Page, where: str, *, left: int, top: int, selected: str = "") -> list[str]:
    """Let the run tick with events arriving, then say what of the marked state did not survive."""
    problems: list[str] = []
    before = page.evaluate(LOOK)
    for k in range(TICKS):
        # What a run sends between its steps: words of its thinking, which repaint the live turn, and a
        # change of state, after which the screen reads the session again and repaints around it.
        event(page, "content_block_delta", {"delta": {"type": "thinking_delta", "text": f"Still checking stage {k}. "}})
        event(page, "state_changed", {})
        page.wait_for_timeout(1000)
    after = page.evaluate(LOOK)
    print(f"{where}: before {before}, after {after}")
    if after["replaced"]:
        problems.append(f"{where}: {after['replaced']} nodes were replaced while the run ticked")
    if left and after["left"] != before["left"]:
        problems.append(f"{where}: the sideways scroll went from {before['left']} to {after['left']}")
    if top and after["top"] != before["top"]:
        problems.append(f"{where}: the scroll went from {before['top']} to {after['top']}")
    if selected and after["selected"] != selected:
        problems.append(f"{where}: the selection {selected!r} became {after['selected']!r}")
    return problems


def running_command(browser) -> list[str]:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_running(context)
    event(page, "message_start", {})
    event(page, "tool_use_start", {"tool_call_id": "run-long", "tool_name": "Exec"})
    event(page, "tool_use_stop", {"tool_call_id": "run-long", "final_input": {"command": COMMAND}})
    page.locator(".turn:last-of-type .thinking-head").click()
    page.wait_for_selector(".turn:last-of-type .act.running + .toolcard pre", timeout=5000)
    marked = page.evaluate(MARK, [".turn:last-of-type .act.running + .toolcard .codecard pre", 240, 60])
    problems: list[str] = []
    if not marked or marked["left"] < 200:
        problems.append(f"the running command's card does not scroll sideways: {marked}")
    else:
        problems += watch(page, "running command", left=marked["left"], top=marked["top"])
    context.close()
    return problems


def panel_file(browser, path: str, body: str) -> list[str]:  # type: ignore[no-untyped-def]
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_running(context, f"&panel=preview&path={path}")
    page.wait_for_selector(f".panel-body.tab-preview {body}", timeout=8000)
    page.wait_for_timeout(500)
    marked = page.evaluate(MARK, [f".panel-body.tab-preview {body}", 0, 400])
    selected = page.evaluate(SELECT, f".panel-body.tab-preview {body}")
    problems: list[str] = []
    if not marked or marked["top"] < 300:
        problems.append(f"{path}: the panel does not scroll: {marked}")
    elif not selected:
        problems.append(f"{path}: nothing to select in the panel")
    else:
        problems += watch(page, f"panel {path}", left=0, top=marked["top"], selected=selected)
    context.close()
    return problems


def main() -> int:
    expect_app(BASE)
    stub.running = True  # type: ignore[attr-defined]
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        problems += running_command(browser)
        problems += panel_file(browser, SOURCE, ".source-view")
        problems += panel_file(browser, NOTES, ".preview-doc")
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if UNHANDLED.report():
        return 1
    print("ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
