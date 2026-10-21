"""The side question (/btw), driven from a chat's composer in a real browser.

The unit tests pin the server: the side question reads its chat's conversation, may only look, leaves the
chat's history alone and answers while the chat's run goes on. What only a browser shows is the way in and
the window: that the composer's helpers (the wand's menu on a desktop, a row of the "+" sheet on a phone)
offer it beside the prompt coach, that `/btw <question>` typed while the agent works opens the window and
is never sent to the chat as a steer, that the window embeds the side question's own conversation with
the answer rendered, that the button opens it on an empty field which asks on Ask, that the desktop window
floats inside the screen and a phone draws a tall sheet, and that Escape closes it (ending the side
question on the host) without doing anything else to the chat. In English and Russian, at 1440×900 and
390×844.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -sfn "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_btw.py

``SHOTS=<dir>`` also writes screenshots of the menu and the window there. Exit status is the number of
failed checks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
CHAT = "chat-1"
SIDE = "btw-1"
WORDS = {
    "en": {
        "helpers": "Improve the prompt or ask a quick question", "coach": "Improve prompt", "open": "Quick question", "title": "Quick question", "ask": "Ask",
        "question": "where did the prices go?", "typed": "why menu.json and not the page?",
        "answer": "They moved into **menu.json**:\n\n- each item has a `price` field\n- the page reads it at build time",
    },
    "ru": {
        "helpers": "Улучшить промпт или задать вопрос по ходу", "coach": "Улучшить промпт", "open": "Вопрос по ходу", "title": "Вопрос по ходу", "ask": "Спросить",
        "question": "куда делись цены?", "typed": "почему menu.json, а не страница?",
        "answer": "Они переехали в **menu.json**:\n\n- у каждого блюда есть поле `price`\n- страница читает его при сборке",
    },
}
PRESETS = {"opus": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000}}


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-10-14T12:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


class Host:
    """What the invented host holds, and what the app asked of it."""

    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.posted: list[tuple[str, str, dict | None]] = []
        self.asked = ""

    def detail(self, sid: str) -> dict:
        side = sid == SIDE
        ru = self.lang == "ru"
        if side:
            messages = [message(1, "user", self.asked), message(2, "assistant", WORDS[self.lang]["answer"])]
        else:
            messages = [message(1, "user", "Сделай меню с ценами" if ru else "Make the menu show prices"), message(2, "assistant", "Работаю над этим." if ru else "Working on it.")]
        return {
            "id": sid, "title": "Side question · Bakery" if side else "Bakery site", "status": "idle" if side else "running", "error": "", "run_id": None if side else "run-1",
            "workspace": "/workspace/bakery", "workspace_name": "bakery", "workspace_own": False, "workspace_sessions": [], "pending": None,
            "model": "Claude Opus 5", "provider": "claude", "project": {"id": "p", "name": "Bakery", "folders": folders("/workspace/bakery"), "settings": {"snapshots": True}},
            "folders": [], "configured_model": "claude-opus-5", "effective_model": "claude-opus-5", "fallback": None, "thinking": True, "reasoning_effort": "high",
            "yagni": False, "brief": "", "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {},
            "context": {"tokens": 4000, "window": 200000, "messages": len(messages), "summaries": 0, "operator_turns": 1},
            "prompt_coach_of": None, "btw_of": CHAT if side else None, "messages": messages,
        }


HOST = Host("en")


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    path = req.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    body: object
    if req.method != "GET":
        data = json.loads(req.post_data) if req.post_data else None
        HOST.posted.append((req.method, rel, data))
        if rel == f"/api/sessions/{CHAT}/btw" and req.method == "POST":
            HOST.asked = str((data or {}).get("question") or "")
            body = {"btw": {"session_id": SIDE, "parent_id": CHAT, "lent": 2}}
        elif rel == f"/api/sessions/{CHAT}/btw":
            body = {"deleted": 1}
        else:
            body = {}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    if rel == "/api/auth/me":
        body = {"user_id": 1, "via": "token"}
    elif rel in (f"/api/sessions/{CHAT}", f"/api/sessions/{SIDE}"):
        body = HOST.detail(rel.rsplit("/", 1)[1])
    elif rel.startswith(f"/api/sessions/{CHAT}/") or rel.startswith(f"/api/sessions/{SIDE}/"):
        body = []
    elif rel == "/api/settings":
        body = {"model": {"preset": "opus"}, "presets": PRESETS}
    elif rel == "/api/sessions":
        body = {"sessions": [], "projects": []}
    elif rel.startswith("/api/usage/provider/"):
        body = {"provider": "claude", "today": {"calls": 1}, "subscription": None, "balance": None}
    elif fulfil_shared(route):
        return
    else:
        UNHANDLED.record(rel)
        body = []
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def parent_field(page: Page):  # type: ignore[no-untyped-def]
    """The chat's own composer field, not the one inside the side question's window."""
    return page.locator(".composer textarea").filter(has_not=page.locator("xpath=ancestor::*[contains(@class,'coach-')]")).first


def overflow(page: Page, root: str) -> list[str]:
    """Everything inside ``root`` that runs off the side of the window, and the page's own sideways scroll."""
    return page.evaluate(
        """(root) => {
            const out = [];
            const width = window.innerWidth;
            if (document.documentElement.scrollWidth > width + 1) out.push(`page scrolls sideways (${document.documentElement.scrollWidth})`);
            for (const el of document.querySelectorAll(`${root}, ${root} *`)) {
                const r = el.getBoundingClientRect();
                if (!r.width || !r.height || getComputedStyle(el).visibility === 'hidden') continue;
                if (el.parentElement && el.parentElement.closest('pre')) continue;
                if (r.right > width + 1 || r.left < -1) out.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ').slice(0, 2).join('.')} ${Math.round(r.left)}..${Math.round(r.right)}`);
            }
            return out.slice(0, 6);
        }""",
        root,
    )


def leaked(question: str) -> list[str]:
    """Every write other than the side question's own that carried the question: a steer, a message, a queue."""
    return [f"{method} {rel}" for method, rel, data in HOST.posted if rel != f"/api/sessions/{CHAT}/btw" and question in json.dumps(data or {}, ensure_ascii=False)]


def run(browser, lang: str, phone: bool, scheme: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    global HOST
    HOST = Host(lang)
    words = WORDS[lang]
    where = f"{lang} {'390' if phone else '1440'}px {scheme}"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    shot = lambda name: page.screenshot(path=f"{SHOTS}/btw-{'phone' if phone else 'wide'}-{lang}-{scheme}-{name}.png") if SHOTS else None  # noqa: E731
    viewport = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, color_scheme=scheme, is_mobile=phone, has_touch=phone, device_scale_factor=2 if phone else 1)
    context.add_init_script("try { localStorage.removeItem('daedalus.btw.position'); localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{CHAT}?token=t&scheme={scheme}&lang={lang}")
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.evaluate(f"() => localStorage.removeItem('daedalus.draft.{CHAT}')")
    root = ".btw-sheet" if phone else ".btw-window"

    # The way in by the button: beside the coach, in the wand's menu or the + sheet.
    if phone:
        if page.locator(".composer .coach-open").count():
            say("a phone's composer row carries the helpers' button; they belong in the + sheet")
        page.locator(".composer .plus").first.click()
        row = page.locator(".ph-plus-sheet [data-action='btw']")
        try:
            row.wait_for(timeout=5000)
        except Exception:  # noqa: BLE001 - reported
            say("the + sheet has no side question row")
            context.close()
            return
        if words["open"] not in row.inner_text() or not page.locator(".ph-plus-sheet [data-action='coach']").count():
            say(f"the + sheet does not offer the side question beside the coach ({row.inner_text()!r})")
        page.wait_for_timeout(400)
        shot("menu")
        off = overflow(page, ".ph-plus-sheet")
        if off:
            say(f"the + sheet runs off the side: {off}")
        row.click()
    else:
        button = page.locator(".composer .coach-open")
        if button.count() != 1:
            say(f"the composer has {button.count()} helper buttons")
            context.close()
            return
        if button.get_attribute("aria-label") != words["helpers"]:
            say(f"the helpers' button is not labelled {words['helpers']!r}")
        button.click()
        item = page.locator(".helpers-menu [data-action='btw']")
        try:
            item.wait_for(timeout=5000)
        except Exception:  # noqa: BLE001 - reported
            say("the helpers' menu has no side question")
            context.close()
            return
        coach = page.locator(".helpers-menu [data-action='coach']")
        if words["open"] not in item.inner_text() or not coach.count() or words["coach"] not in coach.inner_text():
            say(f"the helpers' menu does not hold the coach and the side question ({page.locator('.helpers-menu').inner_text()!r})")
        page.wait_for_timeout(250)
        shot("menu")
        item.click()

    try:
        page.wait_for_selector(f"{root} .btw-ask textarea", timeout=8000)
    except Exception:  # noqa: BLE001 - reported
        say("the button did not open the window on its empty field")
        context.close()
        return
    title = page.locator(f"{root} .coach-title, {root} .sheet-head h3").first.inner_text().strip()
    if title != words["title"]:
        say(f"the window's title is {title!r}, not {words['title']!r}")
    page.wait_for_timeout(300)
    shot("empty")
    off = overflow(page, root)
    if off:
        say(f"the empty window runs off the side: {off}")
    page.locator(f"{root} .btw-ask textarea").fill(words["typed"])
    page.locator(f"{root} .btw-ask button[type=submit]").click()
    try:
        page.wait_for_selector(f"{root} .coach-chat .chat", timeout=8000)
    except Exception:  # noqa: BLE001 - reported
        say("Ask did not show the side question's conversation")
    asked = [data for method, rel, data in HOST.posted if rel == f"/api/sessions/{CHAT}/btw" and method == "POST"]
    if not asked or asked[-1] != {"question": words["typed"]}:
        say(f"Ask did not send the typed question ({asked})")
    page.locator(f"{root} .sheet-head button[aria-label], {root} .coach-head button[aria-label]").last.click()
    page.wait_for_timeout(300)
    if page.locator(root).count():
        say("Close does not close the window")
    if not any(method == "DELETE" and rel == f"/api/sessions/{CHAT}/btw" for method, rel, _ in HOST.posted):
        say("closing the window did not end the side question on the host")

    # The way in by the command, while the agent works: the line opens the window and never steers.
    field = parent_field(page)
    field.click()
    field.fill(f"/btw {words['question']}")
    page.wait_for_timeout(150)
    if phone:
        page.locator(".composer .roundbtn.primary").first.click()
    else:
        field.press("Enter")
    try:
        page.wait_for_selector(f"{root} .coach-chat .chat .msg, {root} .coach-chat .chat [class*='turn']", timeout=10000)
    except Exception:  # noqa: BLE001 - reported
        say("/btw did not open the window on the side question's conversation")
        context.close()
        return
    asked = [data for method, rel, data in HOST.posted if rel == f"/api/sessions/{CHAT}/btw" and method == "POST"]
    if not asked or asked[-1] != {"question": words["question"]}:
        say(f"/btw did not ask the question it carried ({asked[-1:]})")
    if leaked(words["question"]):
        say(f"the /btw line also went to the chat: {leaked(words['question'])}")
    if parent_field(page).input_value():
        say(f"the chat's field still holds the /btw line ({parent_field(page).input_value()!r})")
    page.wait_for_timeout(600)
    text = page.locator(root).inner_text()
    if words["question"] not in text:
        say("the window does not show the question")
    if not page.locator(f"{root} .coach-chat strong, {root} .coach-chat b").count() or not page.locator(f"{root} .coach-chat li").count():
        say("the answer is not rendered as markdown (no bold, no list)")
    if page.locator(f"{root} .chat-head, {root} .ph-topbar, {root} .terminal-dock").count():
        say("the embedded conversation draws a header or a terminal dock of its own")
    if page.locator(f"{root} .composer .coach-open").count() or page.locator(f"{root} .ph-plus-sheet").count():
        say("the side question's own composer offers the helpers")
    if not page.locator(f"{root} .coach-chat .composer textarea").count():
        say("the window has no field for a follow-up")
    box = page.locator(root).bounding_box()
    if phone:
        if not box or box["height"] < viewport["height"] * 0.8:
            say(f"the phone's sheet is not a tall one ({box})")
    elif not box or box["x"] < 0 or box["y"] < 0 or box["x"] + box["width"] > viewport["width"] or box["y"] + box["height"] > viewport["height"]:
        say(f"the window is not inside the screen ({box})")
    off = overflow(page, root)
    if off:
        say(f"the window runs off the side with the answer showing: {off}")
    shot("answer")

    # Escape closes the window and does nothing else: the chat's field keeps what is typed in it.
    field = parent_field(page)
    if not phone:
        field.click()
        field.fill("/")  # the palette is open: an Escape the composer heard would clear the field
        page.wait_for_timeout(150)
    before = len(HOST.posted)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if page.locator(root).count():
        say("Escape does not close the window")
    if not any(method == "DELETE" and rel == f"/api/sessions/{CHAT}/btw" for method, rel, _ in HOST.posted[before:]):
        say("Escape did not end the side question on the host")
    if not phone and parent_field(page).input_value() != "/":
        say(f"the same Escape also cleared the chat's field ({parent_field(page).input_value()!r})")
    if f"/agents/{CHAT}" not in page.url:
        say(f"the same Escape also left the chat ({page.url})")
    if errors:
        say(f"page errors: {errors[:3]}")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM if Path(CHROMIUM).exists() else None)
        for lang in ("en", "ru"):
            for phone in (False, True):
                run(browser, lang, phone, "dark", problems)
        if SHOTS:
            for phone in (False, True):
                run(browser, "ru", phone, "light", problems)
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("side question: all checks passed")
    return len(problems) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
