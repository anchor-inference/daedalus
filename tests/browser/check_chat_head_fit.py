"""The chat header never lays one control over another, whatever it holds and however wide the window.

A chat with many helpers once showed its header as "Wo… › title", the Host and BYPASS chips, "waiting
for 1 subagent", and then the subagents button, a wide "Chat canvases (2)" button, the terminal, search,
the panel and the menu. The state's words ran on under the translucent buttons at the right and were
overprinted by them. Now the left group gives way in a fixed order (the state first, then the crumb, then
the title; the chips stay whole) and clips what is left, the right group stands on the header's own ground,
and the canvases are an icon button with a count like the subagents.

With a long title, a project crumb, both chips, a long state and every optional button (helpers,
canvases, the computer, a public share), this walks 1280, 1366 and 1920 px, with the panel open and closed,
in English and in Russian, and refuses:

- a part of the left group drawn past the start of the right group without the left group clipping it;
- a right group whose ground is not opaque, or whose buttons are missing;
- the crumb or the title cut while the state still shows, or a chip cut or pushed out of view;
- a canvases button wider than an icon button, or one with no count or no name in its tooltip.

    APP_URL=http://127.0.0.1:<port>/app python tests/browser/check_chat_head_fit.py

Each state is photographed into ``SHOTS`` when it is set. Exit 0 when every claim holds.
"""

from __future__ import annotations

import json
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

TITLE = "Move the whole order flow onto the new payment provider and keep the old receipts readable"
PROJECT = "Workshop and bakery operations"
SIZES = ((1280, 800), (1366, 768), (1920, 1080))
# The optional buttons, then the three every header has: search, the panel and the menu.
WANTED = ("chip", "subagents-trigger", "canvases-trigger", "computer-button", "term-button", "chat-search-btn")

FACTS = """() => {
  const head = document.querySelector('.chat-head');
  const left = head.querySelector('.chat-identity');
  const right = head.querySelector('.head-actions');
  const r = (e) => { const b = e.getBoundingClientRect(); return { left: b.left, right: b.right, width: b.width }; };
  const L = r(left), R = r(right);
  const style = getComputedStyle(left);
  const clips = style.overflowX !== 'visible';
  // Every visible piece of the left group that reaches into the right group's place: allowed only
  // when the left group clips it there.
  const reach = [...left.querySelectorAll('*')].filter((e) => e.getClientRects().length && e.getBoundingClientRect().right > R.left + 0.5)
    .map((e) => (typeof e.className === 'string' ? e.className : e.tagName) + ':' + (e.textContent || '').slice(0, 30));
  const cut = (e) => !!e && e.scrollWidth > e.clientWidth + 1;
  const state = left.querySelector('.chat-sub');
  const crumb = left.querySelector('.chat-crumb');
  const title = left.querySelector('.chat-title');
  const chips = [...left.querySelectorAll('.host-mark, .bypass-mark')].map((e) => ({ name: e.className, ...r(e), cut: cut(e) }));
  const canvases = right.querySelector('.canvases-trigger');
  const ground = getComputedStyle(right).backgroundColor;
  return {
    left: L, right: R, clips, reach,
    state: state ? { width: r(state).width, text: state.textContent } : null,
    crumb: crumb ? { width: r(crumb).width, cut: cut(crumb.querySelector('.truncate') || crumb) } : null,
    title: title ? { width: r(title).width, cut: cut(title) } : null,
    chips,
    buttons: [...right.children].map((e) => (typeof e.className === 'string' ? e.className : '') + ' ' + (e.getAttribute('aria-label') || '')),
    canvases: canvases ? { width: r(canvases).width, label: canvases.getAttribute('title') || '', count: canvases.querySelector('.canvases-count')?.textContent || '' } : null,
    ground,
    doc: document.documentElement.scrollWidth - window.innerWidth,
  };
}"""


def serve(page) -> None:  # type: ignore[no-untyped-def]
    def route(r) -> None:  # type: ignore[no-untyped-def]
        path = urlsplit(r.request.url).path
        path = path[path.index("/api/"):]
        if r.request.method == "GET" and path == f"/api/sessions/{shots.S1}":
            detail = shots.detail(shots.S1)
            detail.update(title=TITLE, env="host", bypass=True, subagents_running=1, background_count=0,
                          share={"mode": "public", "url": "http://127.0.0.1/s/x"},
                          project={**detail["project"], "name": PROJECT})
            return shots.respond(r, detail)
        if r.request.method == "GET" and path == "/api/canvases":
            items = [{"id": f"c{n}", "title": f"Receipts {n}", "version": 1, "created_at": shots.ago(hours=n), "updated_at": shots.ago(hours=n),
                      "updated_by": {"kind": "agent", "name": "Daedalus"}} for n in (1, 2)]
            return shots.respond(r, {"scopes": [], "items": items})
        if r.request.method == "GET" and path == "/api/computer":
            return shots.respond(r, {"native": True, "available": True, "lease": {"active": False}})
        return shots.stub(r)

    page.route("**/api/**", route)


def judge(facts: dict, where: str) -> list[str]:
    problems: list[str] = []
    if facts["reach"] and not facts["clips"]:
        problems.append(f"{where}: the left group runs under the buttons and nothing clips it: {facts['reach'][:3]}")
    if facts["left"]["right"] > facts["right"]["left"] + 0.5:
        problems.append(f"{where}: the left group ends at {facts['left']['right']:.0f}, past the buttons at {facts['right']['left']:.0f}")
    ground = facts["ground"]
    if ground in ("transparent", "rgba(0, 0, 0, 0)") or (ground.startswith("rgba(") and not ground.rstrip(")").endswith(", 1")):
        problems.append(f"{where}: the buttons stand on a ground that can be read through ({ground})")
    for wanted in WANTED:
        if not any(wanted in b.split() for b in facts["buttons"]):
            problems.append(f"{where}: no {wanted} among the header's buttons ({facts['buttons']})")
    if len(facts["buttons"]) < len(WANTED) + 2:
        problems.append(f"{where}: the panel's button or the menu is missing ({facts['buttons']})")
    state = facts["state"]
    showing = bool(state and state["width"] > 1.5)
    if facts["crumb"] and facts["crumb"]["cut"] and showing:
        problems.append(f"{where}: the crumb is cut while the state still shows {state['width']:.0f}px")
    if facts["title"] and facts["title"]["cut"] and showing:
        problems.append(f"{where}: the title is cut while the state still shows {state['width']:.0f}px")
    if facts["title"] and facts["title"]["cut"] and facts["crumb"] and facts["crumb"]["width"] > 40:
        problems.append(f"{where}: the title is cut while the crumb keeps {facts['crumb']['width']:.0f}px")
    names = " ".join(c["name"] for c in facts["chips"])
    if "host-mark" not in names or "bypass-mark" not in names:
        problems.append(f"{where}: a chip is missing ({names!r})")
    for chip in facts["chips"]:
        if chip["cut"] or chip["width"] < 8 or chip["right"] > facts["left"]["right"] + 0.5:
            problems.append(f"{where}: the chip {chip['name']} is cut or out of view ({chip})")
    canvases = facts["canvases"]
    if not canvases or canvases["width"] > 40 or canvases["count"] != "2" or "(2)" not in canvases["label"]:
        problems.append(f"{where}: the canvases button is not an icon button with its count and name ({canvases})")
    if facts["doc"] > 0:
        problems.append(f"{where}: the page scrolls sideways by {facts['doc']}px")
    return problems


def walk(browser, lang: str, width: int, height: int, panel: bool) -> list[str]:  # type: ignore[no-untyped-def]
    where = f"{lang} {width}{' panel' if panel else ''}"
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark")
    context.add_init_script(f"try {{ localStorage.setItem('daedalus.session.panel', '{1 if panel else 0}'); }} catch (e) {{}}")
    page = context.new_page()
    serve(page)
    page.goto(f"{BASE}/agents/{shots.S1}?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".chat-head .canvases-trigger", timeout=15000)
    page.wait_for_selector(".chat-head .jobs-pending", state="attached", timeout=15000)
    page.wait_for_selector(".chat-head .computer-button", timeout=15000)
    page.wait_for_timeout(300)
    facts = page.evaluate(FACTS)
    print(where, json.dumps({k: facts[k] for k in ("state", "crumb", "title", "canvases")}, ensure_ascii=False))
    problems = judge(facts, where)
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.locator(".chat-head").screenshot(path=str(SHOTS / f"chat-head-{width}{'-panel' if panel else ''}-{lang}.png"))
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height in SIZES:
                for panel in (False, True):
                    problems += walk(browser, lang, width, height, panel)
        browser.close()
    print("problems:", "\n  ".join(problems) if problems else "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run() or shots.UNHANDLED.report())
