"""The prompt coach's window, driven from a chat's composer in a real browser.

The unit tests pin the server: what the coach may call, where its prompt lands, that no list shows it.
What only a browser shows is the window: that the composer's button (the wand beside "+" on a desktop,
whose menu holds the coach beside the side question; a row of the "+" sheet on a phone) opens it with the draft framed in the reader's language, that it
embeds the coach's own conversation with its question card, that a coach which has saved a prompt
offers Insert and Copy and the saved path, that Insert puts the prompt into the chat's own message,
that the desktop window floats inside the screen, can be dragged (and remembers where), minimised and
closed, and that a phone draws a tall sheet instead. In English and Russian, at 1440×900 and 390×844.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -sfn "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_prompt_coach.py

``SHOTS=<dir>`` also writes screenshots of the window there. Exit status is the number of failed checks.
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
COACH = "coach-1"
DRAFT = "fix the menu page so prices show"
PROMPT = (
    "Rebuild the menu page of the bakery site so every item shows its price.\n\n"
    "Context: the menu is rendered from menu.json by menu.html; prices are in the \"price\" field, in roubles.\n"
    "Constraints: keep the current layout and fonts; do not touch the order form.\n"
    "Output: the changed files, and a one-line summary of what changed.\n"
    "Done when: every item on /menu shows a price formatted like \"350 ₽\", and the page still passes its tests."
)
WORDS = {
    "en": {"open": "Improve prompt", "helpers": "Improve the prompt or ask a quick question", "title": "Prompt coach", "seed": f"Help me improve this prompt:\n\n{DRAFT}", "insert": "Insert into message", "copy": "Copy", "restart": "Start over"},
    "ru": {"open": "Улучшить промпт", "helpers": "Улучшить промпт или задать вопрос по ходу", "title": "Улучшение промпта", "seed": f"Помоги улучшить этот промпт:\n\n{DRAFT}", "insert": "Вставить в сообщение", "copy": "Копировать", "restart": "Начать заново"},
}
PRESETS = {"opus": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000}}


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-10-14T12:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


class Host:
    """What the invented host holds, and what the app asked of it."""

    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.posted: list[tuple[str, str, dict | None]] = []
        self.finished = False
        self.coach_open = False

    def detail(self, sid: str) -> dict:
        coach = sid == COACH
        ru = self.lang == "ru"
        messages = [message(1, "user", "Сделай меню" if ru else "Make the menu"), message(2, "assistant", "Готово." if ru else "Done.")]
        pending = None
        if coach:
            messages = [message(1, "user", WORDS[self.lang]["seed"]), message(2, "assistant", "Два вопроса, чтобы промпт подошёл:" if ru else "Two questions so the prompt fits:")]
            if self.finished:
                messages.append(message(3, "assistant", f"```\n{PROMPT}\n```", tool_calls=[{"id": "c1", "name": "SavePrompt", "arguments": {"title": "Menu prices", "prompt": PROMPT}}]))
            else:
                pending = {"questions": [{"question": "Кто будет выполнять промпт?" if ru else "Who runs the prompt?", "header": "Agent", "options": [
                    {"label": "Агент с кодом" if ru else "A coding agent", "description": "в репозитории сайта" if ru else "in the site's repository"},
                    {"label": "Человек" if ru else "A person"}], "allow_custom": True}]}
        return {
            "id": sid, "title": "Prompt coach · Bakery" if coach else "Bakery site", "status": "waiting" if pending else "idle", "error": "", "run_id": None,
            "workspace": "/workspace/bakery", "workspace_name": "bakery", "workspace_own": False, "workspace_sessions": [], "pending": pending,
            "model": "Claude Opus 5", "provider": "claude", "project": {"id": "p", "name": "Bakery", "folders": folders("/workspace/bakery"), "settings": {"snapshots": True}},
            "folders": [], "configured_model": "claude-opus-5", "effective_model": "claude-opus-5", "fallback": None, "thinking": True, "reasoning_effort": "high",
            "mode": "", "yagni": False, "brief": "", "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {},
            "context": {"tokens": 4000, "window": 200000, "messages": len(messages), "summaries": 0, "operator_turns": 1},
            "prompt_coach_of": CHAT if coach else None, "messages": messages,
        }

    def coach(self) -> dict | None:
        if not self.coach_open:
            return None
        result = {"title": "Menu prices", "prompt": PROMPT, "path": "prompts/2026-10-14-menu-prices.md", "at": "2026-10-14T12:01:00+00:00"} if self.finished else None
        return {"session_id": COACH, "parent_id": CHAT, "finished": self.finished, "result": result}


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
        if rel == f"/api/sessions/{CHAT}/coach":
            HOST.coach_open = True
            if (data or {}).get("fresh"):
                HOST.finished = False
            body = {"coach": {**(HOST.coach() or {}), "created": True}}
        else:
            body = {}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    if rel == "/api/auth/me":
        body = {"user_id": 1, "via": "token"}
    elif rel == f"/api/sessions/{CHAT}/coach":
        body = {"coach": HOST.coach()}
    elif rel in (f"/api/sessions/{CHAT}", f"/api/sessions/{COACH}"):
        body = HOST.detail(rel.rsplit("/", 1)[1])
    elif rel.startswith(f"/api/sessions/{CHAT}/") or rel.startswith(f"/api/sessions/{COACH}/"):
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
    """The chat's own composer field, not the one inside the coach's window."""
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
                // A code block scrolls sideways inside itself; what is clipped there is not off the screen.
                if (el.parentElement && el.parentElement.closest('pre')) continue;
                if (r.right > width + 1 || r.left < -1) out.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ').slice(0, 2).join('.')} ${Math.round(r.left)}..${Math.round(r.right)}`);
            }
            return out.slice(0, 6);
        }""",
        root,
    )


def run(browser, lang: str, phone: bool, scheme: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    global HOST
    HOST = Host(lang)
    words = WORDS[lang]
    where = f"{lang} {'390' if phone else '1440'}px {scheme}"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    viewport = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, color_scheme=scheme, is_mobile=phone, has_touch=phone, device_scale_factor=2 if phone else 1)
    context.add_init_script("try { localStorage.removeItem('daedalus.coach.position'); localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{CHAT}?token=t&scheme={scheme}&lang={lang}")
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.evaluate(f"() => localStorage.removeItem('daedalus.draft.{CHAT}')")
    field = parent_field(page)
    field.click()
    field.fill(DRAFT)
    page.wait_for_timeout(150)

    # The button: beside "+" on a desktop, a row of the "+" sheet on a phone (the phone's row has no room).
    if phone:
        if page.locator(".composer .coach-open").count():
            say("a phone's composer row carries the coach button; it belongs in the + sheet")
        page.locator(".composer .plus").first.click()
        row = page.locator(".ph-plus-sheet [data-action='coach']")
        row.wait_for(timeout=5000)
        if words["open"] not in row.inner_text():
            say(f"the + sheet's row does not say {words['open']!r} ({row.inner_text()!r})")
        row.click()
    else:
        button = page.locator(".composer .coach-open")
        if button.count() != 1:
            say(f"the composer has {button.count()} coach buttons")
            return
        if button.get_attribute("aria-label") != words["helpers"]:
            say(f"the button is not labelled {words['helpers']!r}")
        button.click()
        item = page.locator(".helpers-menu [data-action='coach']")
        item.wait_for(timeout=5000)
        if words["open"] not in item.inner_text():
            say(f"the helpers' menu does not offer {words['open']!r} ({item.inner_text()!r})")
        item.click()

    root = ".coach-sheet" if phone else ".coach-window"
    try:
        page.wait_for_selector(f"{root} .coach-chat .chat .composer textarea", timeout=10000)
    except Exception:  # noqa: BLE001 - reported
        say("the window did not open on the coach's conversation")
        context.close()
        return
    sent = [data for method, rel, data in HOST.posted if rel == f"/api/sessions/{CHAT}/coach"]
    if not sent or sent[0].get("message") != words["seed"] or sent[0].get("fresh"):
        say(f"the coach was not opened with the framed draft ({sent})")
    title = page.locator(f"{root} .coach-title, {root} .sheet-head h3").first.inner_text().strip()
    if title != words["title"]:
        say(f"the window's title is {title!r}, not {words['title']!r}")
    page.wait_for_selector(f"{root} .ask-card, {root} .ph-ask, {root} [class*='ask']", timeout=8000)
    if page.locator(f"{root} .chat-head, {root} .ph-topbar, {root} .terminal-dock").count():
        say("the embedded conversation draws a header or a terminal dock of its own")
    if page.locator(f"{root} .composer .coach-open").count() or page.locator(f"{root} .composer .composer-mode, {root} .composer .mode-select").count() > 1:
        say("the coach's own composer offers a coach or a mode")
    box = page.locator(root).bounding_box()
    if phone:
        if not box or box["height"] < viewport["height"] * 0.8:
            say(f"the phone's sheet is not a tall one ({box})")
    elif not box or box["x"] < 0 or box["y"] < 0 or box["x"] + box["width"] > viewport["width"] or box["y"] + box["height"] > viewport["height"]:
        say(f"the window is not inside the screen ({box})")
    elif box["x"] < viewport["width"] / 2 or box["y"] + box["height"] > viewport["height"] - 120:
        say(f"the window does not start on the right, above the composer ({box})")
    else:
        send = page.locator(".composer .roundbtn.primary").first.bounding_box()
        if send and box["y"] + box["height"] > send["y"] and box["x"] < send["x"] + send["width"]:
            say("the window covers the chat's Send button")
    off = overflow(page, root)
    if off:
        say(f"runs off the side with the question showing: {off}")
    if SHOTS:
        page.wait_for_timeout(500)  # the sheet's rise has finished
        page.screenshot(path=f"{SHOTS}/coach-{'phone' if phone else 'wide'}-{lang}-{scheme}-asking.png")

    # The coach saves its prompt: the window offers it.
    HOST.finished = True
    try:
        page.wait_for_selector(f"{root} .coach-result .coach-insert", timeout=10000)
    except Exception:  # noqa: BLE001 - reported
        say("a finished coach does not offer its prompt")
        context.close()
        return
    insert = page.locator(f"{root} .coach-insert")
    copy = page.locator(f"{root} .coach-copy")
    if words["insert"] not in insert.inner_text() or words["copy"] not in copy.inner_text():
        say(f"the result's buttons read {insert.inner_text()!r} and {copy.inner_text()!r}")
    if "prompts/2026-10-14-menu-prices.md" not in page.locator(f"{root} .coach-path").inner_text():
        say("the saved path is not shown")
    off = overflow(page, root)
    if off:
        say(f"runs off the side with the result showing: {off}")
    for button in (insert, copy):
        b = button.bounding_box()
        if b and b["height"] < (40 if phone else 26):
            say(f"a result button is too small to press ({b})")
    if SHOTS:
        page.wait_for_timeout(300)
        page.screenshot(path=f"{SHOTS}/coach-{'phone' if phone else 'wide'}-{lang}-{scheme}-ready.png")

    if not phone:
        # Dragged by its title bar, kept on the screen, remembered.
        head = page.locator(".coach-head .coach-title").bounding_box()
        page.mouse.move(head["x"] + 10, head["y"] + 10)
        page.mouse.down()
        page.mouse.move(head["x"] - 600, head["y"] - 900, steps=6)
        page.mouse.up()
        moved = page.locator(".coach-window").bounding_box()
        if not moved or moved["y"] < 0 or moved["x"] < 0 or moved["x"] > box["x"] - 300:
            say(f"dragging the title bar did not move the window, or moved it off the screen ({moved})")
        stored = page.evaluate("() => localStorage.getItem('daedalus.coach.position')")
        if not stored or json.loads(stored)["y"] < 0:
            say(f"the window's place is not remembered ({stored})")
        # Minimised: a bar in the corner, the conversation still mounted.
        page.locator(".coach-head button[aria-label]").nth(-2).click()
        page.wait_for_timeout(200)
        bar = page.locator(".coach-window.min").bounding_box()
        if not bar or bar["height"] > 60 or bar["x"] + bar["width"] > viewport["width"]:
            say(f"minimised, the window is not a bar in the corner ({bar})")
        page.locator(".coach-head button[aria-label]").nth(-2).click()
        page.wait_for_selector(".coach-window:not(.min) .coach-insert", timeout=5000)

    # Insert puts the prompt into the chat's own message.
    insert.click()
    page.wait_for_timeout(300)
    value = parent_field(page).input_value()
    if value != PROMPT:
        say(f"Insert did not put the prompt into the chat's message ({value[:60]!r})")
    if phone and page.locator(".coach-sheet").count():
        say("the phone's sheet stays over the message after Insert")
    if not phone:
        page.locator(".coach-head button[aria-label]").last.click()
        page.wait_for_timeout(200)
        if page.locator(".coach-window").count():
            say("Close does not close the window")
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
        print("prompt coach: all checks passed")
    return len(problems) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
