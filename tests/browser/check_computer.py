"""The Computer control of a conversation in the native application, driven in a real browser.

The unit tests pin the host's routes and the component's states. What only a browser shows is the
whole flow on a drawn page: the header's button opens the control; on macOS it lists Screen
Recording and Accessibility with their state, Ask and the System Settings link, and says that the
grants are lost on every update; one window is chosen and control granted to this conversation; the
banner above the composer says the agent is controlling the computer, with the time left; the
operator's hand pausing the agent brings Resume; Stop ends it and the banner goes. On a phone the
control is a tile of the ⋮ sheet and the banner fits the screen. On a server (the shared stub's
default) nothing of it is drawn. In English and Russian.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -sfn "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_computer.py

``SHOTS=<dir>`` also writes screenshots of the control and the banner there. Exit status is the
number of failed checks.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, ComputerStub, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
CHAT = "chat-1"
WORDS = {
    "en": {"button": "Computer", "title": "Control of this computer", "grant": "Give control for 10 min", "preset": "10 min", "banner": "The agent is controlling your computer",
           "paused": "The agent is paused", "resume": "Resume", "stop": "Stop", "denied": "Not granted", "granted": "Granted", "open": "Open System Settings",
           "adhoc": "macOS forgets these permissions after every update", "left": "min left"},
    "ru": {"button": "Компьютер", "title": "Управление этим компьютером", "grant": "Дать управление на 10 мин", "preset": "10 мин", "banner": "Агент управляет вашим компьютером",
           "paused": "Агент на паузе", "resume": "Продолжить", "stop": "Остановить", "denied": "Не разрешено", "granted": "Разрешено", "open": "Открыть Системные настройки",
           "adhoc": "macOS забывает эти разрешения после каждого обновления", "left": "осталось"},
}
PRESETS = {"opus": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000}}


def message(seq: int, role: str, text: str) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "",
            "tool_calls": [], "tool_results": [], "created_at": f"2026-10-20T12:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None}


def detail(lang: str) -> dict:
    ru = lang == "ru"
    messages = [message(1, "user", "Заполни таблицу в редакторе" if ru else "Fill in the table in the editor"), message(2, "assistant", "Мне нужно управление окном редактора." if ru else "I need control of the editor's window.")]
    return {
        "id": CHAT, "title": "Quarterly report", "status": "idle", "error": "", "run_id": None,
        "workspace": "/workspace/report", "workspace_name": "report", "workspace_own": False, "workspace_sessions": [], "pending": None,
        "model": "Claude Opus 5", "provider": "claude", "project": {"id": "p", "name": "Report", "folders": folders("/workspace/report"), "settings": {"snapshots": True}},
        "folders": [], "configured_model": "claude-opus-5", "effective_model": "claude-opus-5", "fallback": None, "thinking": True, "reasoning_effort": "high",
        "mode": "", "yagni": False, "brief": "", "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {},
        "context": {"tokens": 4000, "window": 200000, "messages": len(messages), "summaries": 0, "operator_turns": 1},
        "prompt_coach_of": None, "btw_of": None, "messages": messages,
    }


class Host:
    def __init__(self, lang: str, *, native: bool = True) -> None:
        self.lang = lang
        self.native = native
        self.desk = ComputerStub()


HOST = Host("en")


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    path = req.url.split("?", 1)[0]
    rel = path[path.index("/api/"):]
    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if HOST.native and rel.startswith("/api/computer"):
        body = json.loads(req.post_data) if req.post_data else None
        answered = HOST.desk.answer(req.method, rel, body)
        if answered is not None:
            return route.fulfill(status=answered[0], content_type="application/json", body=json.dumps(answered[1]))
    reply: object
    if req.method == "GET" and rel == "/api/auth/me":
        reply = {"user_id": 1, "via": "token"}
    elif req.method == "GET" and rel == f"/api/sessions/{CHAT}":
        reply = detail(HOST.lang)
    elif req.method == "GET" and rel.startswith(f"/api/sessions/{CHAT}/"):
        reply = []
    elif req.method == "GET" and rel == "/api/settings":
        reply = {"model": {"preset": "opus"}, "presets": PRESETS}
    elif req.method == "GET" and rel.startswith("/api/usage/provider/"):
        reply = {"provider": "claude", "today": {"calls": 1}, "subscription": None, "balance": None}
    elif fulfil_shared(route):
        return
    elif req.method != "GET":
        reply = {}
    else:
        UNHANDLED.record(rel)
        reply = []
    route.fulfill(status=200, content_type="application/json", body=json.dumps(reply))


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
                if (r.right > width + 1 || r.left < -1) out.push(`${el.tagName.toLowerCase()}.${String(el.className).split(' ').slice(0, 2).join('.')} ${Math.round(r.left)}..${Math.round(r.right)}`);
            }
            return out.slice(0, 6);
        }""",
        root,
    )


def open_page(browser, lang: str, phone: bool, native: bool = True):  # type: ignore[no-untyped-def]
    global HOST
    HOST = Host(lang, native=native)
    viewport = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, color_scheme="dark", is_mobile=phone, has_touch=phone, device_scale_factor=2 if phone else 1)
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); } catch (e) {}")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{CHAT}?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".composer textarea", timeout=15000)
    return context, page, errors


def run_desktop(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} 1440px"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    shot = lambda name: page.screenshot(path=f"{SHOTS}/computer-wide-{lang}-{name}.png") if SHOTS else None  # noqa: E731
    context, page, errors = open_page(browser, lang, False)
    button = page.locator(".chat-head .computer-button")
    try:
        button.wait_for(timeout=8000)
    except Exception:  # noqa: BLE001 - reported
        say("the header has no Computer button in the native application")
        context.close()
        return
    if button.get_attribute("aria-label") != words["button"]:
        say(f"the button is labelled {button.get_attribute('aria-label')!r}")
    button.click()
    sheet = page.locator(".computer-sheet")
    try:
        sheet.wait_for(timeout=5000)
    except Exception:  # noqa: BLE001 - reported
        say("the button did not open the control")
        context.close()
        return
    if sheet.locator(".sheet-head h3").inner_text().strip() != words["title"]:
        say(f"the control's title is {sheet.locator('.sheet-head h3').inner_text()!r}")

    # macOS: both permissions with their state, Ask where it is missing, the link, and the warning.
    screen = sheet.locator("[data-permission=screen]")
    accessibility = sheet.locator("[data-permission=accessibility]")
    if screen.get_attribute("data-state") != "denied" or words["denied"] not in screen.inner_text():
        say(f"the Screen Recording row does not say it is not granted ({screen.inner_text()!r})")
    if words["granted"] not in accessibility.inner_text():
        say(f"the Accessibility row does not say it is granted ({accessibility.inner_text()!r})")
    if screen.locator("a").get_attribute("href") != ComputerStub.LINKS["screen"] or words["open"] not in screen.locator("a").inner_text():
        say("the Screen Recording row does not link to its System Settings pane")
    if words["adhoc"] not in sheet.inner_text():
        say("the control does not say macOS forgets the grants after an update")
    screen.locator("button").first.click()
    page.wait_for_timeout(300)
    if ("permissions", {"which": "screen"}) not in HOST.desk.posts:
        say(f"Ask did not ask for Screen Recording ({HOST.desk.posts})")

    # Grant one window for ten minutes.
    sheet.locator(".segmented button", has_text=words["preset"]).first.click()
    row = sheet.locator(".computer-row", has_text="notes.txt")
    if not row.count():
        say("the window list does not show notes.txt")
        context.close()
        return
    row.locator("input[type=radio]").check()
    off = overflow(page, ".computer-sheet")
    if off:
        say(f"the control runs off the side: {off}")
    shot("control")
    sheet.locator("button", has_text=words["grant"]).click()
    page.wait_for_timeout(400)
    grants = [body for action, body in HOST.desk.posts if action == "grant"]
    if grants != [{"session_id": CHAT, "scope": {"kind": "window", "window": "10"}, "minutes": 10}]:
        say(f"the grant sent was not one window for ten minutes ({grants})")
    if not sheet.locator(".computer-status[data-computer=mine]").count():
        say("the control does not say this conversation now holds the computer")
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    if page.locator(".computer-sheet").count():
        say("Escape does not close the control")

    banner = page.locator(".computer-banner")
    try:
        banner.wait_for(timeout=5000)
    except Exception:  # noqa: BLE001 - reported
        say("no banner while this conversation controls the computer")
        context.close()
        return
    text = banner.inner_text()
    if words["banner"] not in text or words["left"] not in text:
        say(f"the banner does not say what is happening and for how long ({text!r})")
    if "on" not in (button.get_attribute("class") or ""):
        say("the header's button is not lit while this conversation holds the computer")
    if banner.locator("button", has_text=words["resume"]).count():
        say("the banner offers Resume while nothing is paused")
    box = banner.bounding_box()
    composer = page.locator(".composer-box").first.bounding_box()
    if box and composer and (box["y"] + box["height"] > composer["y"] + 1):
        say(f"the banner is not above the composer ({box} / {composer})")
    shot("banner")

    # The operator's hand pauses the agent: Resume appears, and lifts it.
    HOST.desk.paused = True
    try:
        page.wait_for_selector(".computer-banner[data-computer=paused]", timeout=8000)
    except Exception:  # noqa: BLE001 - reported
        say("the banner did not show the pause within a poll")
    else:
        if words["paused"] not in banner.inner_text():
            say(f"the paused banner does not say so ({banner.inner_text()!r})")
        shot("paused")
        banner.locator("button", has_text=words["resume"]).click()
        page.wait_for_timeout(400)
        if ("resume", {}) not in HOST.desk.posts:
            say("Resume did not reach the host")

    # Stop ends it, and the banner goes.
    page.locator(".computer-banner button", has_text=words["stop"]).click()
    try:
        page.wait_for_selector(".computer-banner", state="detached", timeout=5000)
    except Exception:  # noqa: BLE001 - reported
        say("the banner is still there after Stop")
    if ("stop", {}) not in HOST.desk.posts:
        say("Stop did not reach the host")
    if errors:
        say(f"page errors: {errors[:3]}")
    context.close()


def run_phone(browser, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    where = f"{lang} 390px"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    context, page, errors = open_page(browser, lang, True)
    HOST.desk.answer("POST", "/api/computer/grant", {"session_id": CHAT, "scope": {"kind": "window", "window": "10"}, "minutes": 30})
    page.reload()
    try:
        page.wait_for_selector(".computer-banner", timeout=10000)
    except Exception:  # noqa: BLE001 - reported
        say("no banner on a phone while this conversation controls the computer")
    else:
        off = overflow(page, ".computer-banner")
        if off:
            say(f"the banner runs off the side: {off}")
        if SHOTS:
            page.screenshot(path=f"{SHOTS}/computer-phone-{lang}-banner.png")
    page.locator(".ph-ib[aria-haspopup=menu]").last.click()
    tile = page.locator(".ph-session-menu [data-tile=computer]")
    try:
        tile.wait_for(timeout=5000)
        if words["button"] not in tile.inner_text():
            say(f"the Computer tile reads {tile.inner_text()!r}")
        tile.click()
        page.wait_for_selector(".computer-sheet", timeout=5000)
    except Exception:  # noqa: BLE001 - reported
        say("the ⋮ sheet has no Computer tile that opens the control")
    if errors:
        say(f"page errors: {errors[:3]}")
    context.close()


def run_server(browser, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    context, page, errors = open_page(browser, "en", False, native=False)
    page.wait_for_timeout(800)
    if page.locator(".computer-button, .computer-banner").count():
        problems.append("server: the Computer control is drawn outside the native application")
    if errors:
        problems.append(f"server: page errors: {errors[:3]}")
    context.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM if Path(CHROMIUM).exists() else None)
        for lang in ("en", "ru"):
            run_desktop(browser, lang, problems)
            run_phone(browser, lang, problems)
        run_server(browser, problems)
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("computer control: all checks passed")
    return len(problems) + UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
