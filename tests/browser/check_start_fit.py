"""The start page's composer holds every control it carries, each with its whole word, at every desktop width.

Under the field the new chat's composer carries where and how the chat starts (the project, what it will
be, where it runs, its access) and the chat composer's own controls ("+", the model with its effort, the
microphone, Send). In one row with a long project name and a long model name they no longer fitted the
760 px card: every chip was cut to a letter or two ("No proj…", "C…", "With chec…") while the model's
name took the rest. They are two rows now, and the first wraps rather than cutting a word.

With a 40-character project picked and a 30-character model as the default, this walks the start page at
1280, 1366, 1920 and 2560 px, in English and in Russian, and refuses:

- two controls whose boxes overlap, or a control outside the card;
- a word of a chip cut, other than the project's name (the operator's own, any length), which may be
  cut only with its whole path in the tooltip;
- the model's name or its effort cut: the model has a row of its own and room for 30 characters;
- a row that does not end with Send, or a page that scrolls sideways.

    APP_URL=http://127.0.0.1:<port>/app python tests/browser/check_start_fit.py

Each width is photographed into ``SHOTS`` when it is set. Exit 0 when every claim holds.
"""

from __future__ import annotations

import copy
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

PROJECT = "Bakery site: the seasonal menu and prices"[:40]
MODEL = "deepseek-v4.1-flash-2025-09-30"
SIZES = ((1280, 800), (1366, 768), (1920, 1080), (2560, 1440))
assert len(PROJECT) == 40 and len(MODEL) == 30

FACTS = """() => {
  const card = document.querySelector('.start-composer .composer-box');
  const controls = [...card.querySelectorAll('.composer-where > *, .composer-row > *, .composer-tools > *')]
    .filter((e) => e.tagName !== 'INPUT' && !e.classList.contains('composer-tools') && e.getClientRects().length);
  const rect = (e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom }; };
  const name = (e) => (typeof e.className === 'string' && e.className.trim() ? e.className.trim().split(/\\s+/)[0] : e.tagName);
  // A word is cut when its own box is narrower than what it holds; a chip's text is in a span of its own.
  const cut = [];
  for (const c of controls) {
    for (const part of [c, ...c.querySelectorAll('span')]) {
      if (part.getClientRects().length && part.scrollWidth > part.clientWidth + 1) cut.push({ control: name(c), part: name(part), text: part.textContent, title: c.getAttribute('title') || '' });
    }
  }
  const last = controls[controls.length - 1];
  return {
    card: rect(card),
    controls: controls.map((e) => ({ name: name(e), classes: typeof e.className === 'string' ? e.className.split(/\\s+/) : [], ...rect(e) })),
    cut,
    model: card.querySelector('.model-select .model-label')?.textContent ?? null,
    effort: card.querySelector('.model-select .model-effort')?.textContent ?? null,
    project: card.querySelector('.composer-project')?.textContent ?? null,
    last: last ? last.getAttribute('data-action') : null,
    doc: document.documentElement.scrollWidth - window.innerWidth,
  };
}"""


def serve(page) -> None:  # type: ignore[no-untyped-def]
    projects = copy.deepcopy(shots.PROJECTS)
    for project in projects:
        if project["id"] == shots.P1:
            project["name"] = PROJECT
    settings = copy.deepcopy(shots.SETTINGS)
    settings["presets"]["deepseek-flash"]["label"] = MODEL

    def route(r) -> None:  # type: ignore[no-untyped-def]
        path = urlsplit(r.request.url).path
        path = path[path.index("/api/"):]
        if r.request.method == "GET" and path == "/api/projects":
            return shots.respond(r, projects)
        if r.request.method == "GET" and path == "/api/settings":
            return shots.respond(r, settings)
        if path == "/api/asr":
            return shots.respond(r, {"configured": True, "reason": "", "provider": "local", "model": "whisper", "max_seconds": 120, "autosend": False})
        return shots.stub(r)

    page.route("**/api/**", route)


def overlaps(controls: list[dict]) -> list[str]:
    found = []
    for i, a in enumerate(controls):
        for b in controls[i + 1:]:
            across = min(a["right"], b["right"]) - max(a["left"], b["left"])
            down = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
            if across > 0.5 and down > 0.5:
                found.append(f"{a['name']} and {b['name']} overlap by {across:.1f}x{down:.1f}px")
    return found


def walk(browser, lang: str, width: int, height: int, picked: bool) -> list[str]:  # type: ignore[no-untyped-def]
    where = f"{lang} {width} {'project' if picked else 'no project'}"
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark")
    page = context.new_page()
    serve(page)
    page.goto(f"{BASE}/agents?{f'in={shots.P1}&' if picked else ''}token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".start-composer .composer-project", timeout=15000)
    page.wait_for_function(f"() => (document.querySelector('.start-composer .model-select')?.textContent || '').includes({json.dumps(MODEL[:12])})", timeout=15000)
    page.locator(".start-composer .composer-field").fill("Bake the menu")
    page.wait_for_timeout(250)
    facts = page.evaluate(FACTS)
    # Everything the row carries: a chat of its own also says where it runs (Container or Host).
    wanted = ["composer-project", "composer-mode", "composer-access", "plus", "model-select", "mic", "roundbtn"] + ([] if picked else ["runon-select"])
    names = [c["name"] for c in facts["controls"]]
    missing = [w for w in wanted if not any(w in c["classes"] for c in facts["controls"])]
    print(where, json.dumps({k: facts[k] for k in ("project", "model", "effort", "last")}, ensure_ascii=False))
    print("   ", " ".join(f"{c['name']}[{c['left']:.0f}–{c['right']:.0f},{c['top']:.0f}]" for c in facts["controls"]))
    problems = [f"{where}: no {w} in the controls ({names})" for w in missing]
    problems += [f"{where}: {line}" for line in overlaps(facts["controls"])]
    card = facts["card"]
    for c in facts["controls"]:
        if c["left"] < card["left"] - 0.5 or c["right"] > card["right"] + 0.5 or c["top"] < card["top"] - 0.5 or c["bottom"] > card["bottom"] + 0.5:
            problems.append(f"{where}: {c['name']} at {c['left']:.0f}–{c['right']:.0f} leaves the card at {card['left']:.0f}–{card['right']:.0f}")
    for cut in facts["cut"]:
        if cut["control"] == "composer-project":
            if PROJECT not in cut["title"] and "/" not in cut["title"]:
                problems.append(f"{where}: the project's name is cut with no tooltip to read it whole ({cut})")
            continue
        problems.append(f"{where}: {cut['control']} is cut: {cut['text']!r}")
    if (facts["model"] or "").strip() != MODEL:
        problems.append(f"{where}: the model reads {facts['model']!r}, not its whole name")
    if not facts["effort"]:
        problems.append(f"{where}: no effort beside the model")
    if facts["last"] != "send":
        problems.append(f"{where}: the controls end with {facts['last']!r}, not Send")
    if facts["doc"] > 0:
        problems.append(f"{where}: the page scrolls sideways by {facts['doc']}px")
    if SHOTS is not None:
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.locator(".start-composer").screenshot(path=str(SHOTS / f"start-composer-{width}-{lang}{'-project' if picked else ''}.png"))
    context.close()
    return problems


def run() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height in SIZES:
                for picked in (True, False):
                    problems += walk(browser, lang, width, height, picked)
        browser.close()
    print("problems:", "\n  ".join(problems) if problems else "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    sys.exit(run() or shots.UNHANDLED.report())
