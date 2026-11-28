"""A live link's limits in a real browser: the operator's window and the visitor's side of it.

The operator opens the share dialog, then the live link's window: wide on a desktop (1100 px at 1920 and
at 1366), a full-screen sheet on a phone at 390 with 16 px gutters and nothing scrolling sideways. A link
is made with a password, every role locked but the conversation's, which is opened to all of one provider
with one effort narrowed, a money and a weekly limit and no terminals, and the POST body must equal what
the host's routes take. The same link is then edited: a short password is refused by the host and the
refusal shows at the password, and a new label and limit go out as a PATCH. The visitor's page meets a
password screen (a wrong password, too many tries with the wait counted down, the right one), then a
locked model that opens no menu, what is left of the budget, and a send refused with 402.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 48231 /tmp/app-root &
    APP_URL=http://127.0.0.1:48231/app python3 tests/browser/check_live_link_limits.py

``SHOTS=dir`` keeps pictures of the window and the password screen. Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_stub  # noqa: E402
from api_stub import DEFAULT_APP, VisitDoor, expect_app, folders, live_options  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402  the record the installation below writes its misses to
from screenshots import stub as installation  # noqa: E402  the same invented installation the pictures are taken of

BASE = os.environ.get("APP_URL", DEFAULT_APP)
ORIGIN = BASE.rsplit("/app", 1)[0]
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
SESSION = "sess-live"
TOKEN = "k" * 43
PASSWORD = "plum-river-42"

WORDS = {
    "en": {"actions": "Session actions", "share": "Share…", "create": "Create live link", "set": "From a set", "high": "High", "save": "Save", "change": "Change",
           "wrong": "That password is not right.", "spent": "The limit the owner set for this link is reached."},
    "ru": {"actions": "Действия с сессией", "share": "Поделиться…", "create": "Создать живую ссылку", "set": "Из набора", "high": "Высокое", "save": "Сохранить", "change": "Сменить",
           "wrong": "Пароль неверный.", "spent": "Лимит, который владелец задал для этой ссылки, исчерпан."},
}


def message(seq: int, role: str, text: str) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "",
            "tool_calls": [], "tool_results": [], "created_at": f"2026-11-28T12:00:{seq:02d}+00:00", "model": "", "provider": "", "fallback": None}


def detail() -> dict:
    return {
        "id": SESSION, "title": "Меню пекарни на зиму", "status": "idle", "error": "", "run_id": None, "workspace": "/workspace", "workspace_name": "ws", "workspace_own": True,
        "workspace_sessions": [], "pending": None, "model": "Claude Opus 5", "provider": "claude",
        "project": {"id": "p", "name": "Bakery", "folders": folders("/workspace"), "settings": {}},
        "configured_model": "claude-opus-5", "effective_model": "claude-opus-5", "fallback": None, "thinking": True, "reasoning_effort": "high", "yagni": False, "brief": "",
        "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {}, "context": None,
        "messages": [message(1, "user", "Составь меню на зиму."), message(2, "assistant", "Вот черновик: хлеб, кофе, штрудель.")],
    }


DOOR: VisitDoor | None = None
SENDS: list[int] = []


def stub(route) -> None:  # type: ignore[no-untyped-def]
    req = route.request
    rel = req.url.split("?", 1)[0]
    rel = rel[rel.index("/api/"):]

    def answer(body: object, status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    if DOOR is not None and f"/v/{TOKEN}/" in req.url:
        sent = json.loads(req.post_data) if req.post_data else None
        opened = DOOR.answer(req.method, rel, sent)
        if opened is not None:
            if opened[0] == 402:
                SENDS.append(402)
            return answer(opened[1], opened[0])
    if rel.endswith("/stream"):
        return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
    if rel == f"/api/sessions/{SESSION}/messages" and req.method == "POST":
        SENDS.append(200)
        return answer({"run_id": "r2", "receipt": {"status": "queued"}})
    if req.method == "GET" and rel == f"/api/sessions/{SESSION}":
        return answer(detail())
    if rel.startswith(f"/api/sessions/{SESSION}/live-shares"):
        parts = rel.split("/")
        status, _kind, body = api_stub.LIVE.answer(req.method, SESSION, parts[5] if len(parts) > 5 else "", json.loads(req.post_data) if req.post_data else None)
        return route.fulfill(status=status, content_type="application/json", body=body)
    if req.method == "GET" and rel.startswith(f"/api/sessions/{SESSION}/") and rel.split("/")[-1] in ("tasks", "terminals", "attachments", "canvases", "notes"):
        return answer([] if rel.endswith(("canvases", "notes")) else {"attachments": []} if rel.endswith("attachments") else [])
    return installation(route)


def shot(page: Page, name: str, at: str = "") -> None:
    """A picture; ``at`` names a section of the window to scroll to first (its top otherwise stays where the steps left it)."""
    if not SHOTS:
        return
    if at:
        page.evaluate("(s) => { const el = document.querySelector(`[data-live-window] [data-section='${s}']`); const box = el && (el.closest('.live-win-body') && getComputedStyle(el.closest('.live-win-body')).overflowY === 'auto' ? el.closest('.live-win-body') : el.closest('.sheet-body')); if (el && box) box.scrollTop = el.offsetTop - (box === el.closest('.live-win-body') ? 0 : 0) - 8; }", at)
    page.wait_for_timeout(450)
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    page.screenshot(path=f"{SHOTS}/{name}.png")


def press(locator) -> None:  # type: ignore[no-untyped-def]
    """A click on a control of the window brought to the middle first: on a phone the decision's foot is
    pinned over the bottom of the screen, and a control scrolled just into view sits under it."""
    locator.evaluate("el => el.scrollIntoView({block: 'center'})")
    try:
        locator.click(timeout=3000)
    except Exception:
        if SHOTS:
            locator.page.screenshot(path=f"{SHOTS}/debug-press.png")
        raise


def open_window(page: Page, words: dict[str, str]) -> None:
    page.get_by_role("button", name=words["actions"], exact=True).click()
    page.wait_for_selector(".ph-session-menu, [role='menu']", timeout=5000)
    sheet = page.locator(".ph-session-menu")
    if sheet.count():
        sheet.get_by_role("button", name=words["share"], exact=True).click()
    else:
        page.get_by_role("menuitem", name=words["share"], exact=True).click()
    page.locator(".live-links").get_by_role("button", name=words["create"]).click()
    page.wait_for_selector("[data-live-window] .live-role", timeout=10000)


def check_layout(page: Page, width: int, problems: list[str]) -> None:
    box = page.locator(".sheet.live-sheet").bounding_box()
    assert box, "the window is not drawn"
    want = 390 if width < 1024 else 1100
    if abs(box["width"] - want) > 1:
        problems.append(f"{width}: the window is {box['width']:.0f} px wide, not {want}")
    scroll = page.evaluate("() => Math.max(document.documentElement.scrollWidth - innerWidth, ...[...document.querySelectorAll('.live-sheet .sheet-body, .live-win-body, .live-win')].map(e => e.scrollWidth - e.clientWidth))")
    if scroll > 0:
        problems.append(f"{width}: something scrolls sideways by {scroll} px")
    if width < 1024:
        for selector in (".live-sec-title", ".live-role", ".live-pw", ".live-win-listhead"):
            edge = page.locator(selector).first.bounding_box()
            if edge and (edge["x"] < 16 - 0.5 or edge["x"] + edge["width"] > width - 16 + 0.5):
                problems.append(f"{width}: {selector} leaves the 16 px gutters ({edge['x']:.0f}..{edge['x'] + edge['width']:.0f})")


def expected_create(words: dict[str, str]) -> dict:
    """The POST the window must build for the link the check fills in, in the shape the host's routes take."""
    options = live_options()
    roles = {role["id"]: {**role["current"], "allowed": []} for role in options["roles"]}
    codex = [p["id"] for p in options["roles"][0]["presets"] if p["provider"] == "codex"]
    roles["main"]["allowed"] = [{"preset": pid, "accounts": [], "efforts": ["high"] if pid == "gpt-6-codex" else []} for pid in codex]
    return {"expires_hours": 24, "label": "Анна — меню", "password": PASSWORD, "policy": {"roles": roles, "budget_usd": 5, "budget_weekly_pct": 3, "terminals": "none"}}


def operator(browser, lang: str, width: int, height: int, scheme: str, problems: list[str], full: bool) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    api_stub.LIVE = api_stub.LiveLinks()
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{SESSION}?token=t&scheme={scheme}&lang={lang}")
    page.wait_for_selector(".composer", timeout=15000)
    open_window(page, words)
    check_layout(page, width, problems)
    window = page.locator("[data-live-window]")
    if not full:
        shot(page, f"window-new-{width}-{scheme}-{lang}")
        context.close()
        return
    window.locator("#live-label").fill("Анна — меню")
    window.locator("#live-password").fill(PASSWORD)
    main = window.locator(".live-role[data-role='main']")
    press(main.get_by_role("radio", name=words["set"]))
    press(main.locator(".live-allowed-prov[data-prov='codex']"))
    main.locator(".live-allowed-provall input").evaluate("el => el.scrollIntoView({block: 'center'})")
    main.locator(".live-allowed-provall input").check()
    press(main.locator(".live-allowed-row[data-preset='gpt-6-codex'] .live-chipbtn", has_text=words["high"]))
    window.locator("#live-limit-usd").fill("5")
    window.locator("#live-limit-pct").fill("3")
    window.locator("[data-terminals='none'] input").evaluate("el => el.scrollIntoView({block: 'center'})")
    window.locator("[data-terminals='none'] input").check()
    summary = window.locator("[data-live-summary]").inner_text()
    if not summary.strip():
        problems.append("the summary strip is empty")
    page.locator(".live-win-body, .live-sheet .sheet-body").evaluate_all("els => els.forEach(e => { e.scrollTop = 0; })")
    shot(page, f"window-filled-{width}-{scheme}-{lang}")
    shot(page, f"window-filled-models-{width}-{scheme}-{lang}", at="models")
    shot(page, f"window-filled-limits-{width}-{scheme}-{lang}", at="limits")
    window.locator(".live-save").click()
    deadline = time.monotonic() + 5
    while not api_stub.LIVE.writes and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    if not api_stub.LIVE.writes:
        problems.append("no POST was sent")
        context.close()
        return
    method, _link, body = api_stub.LIVE.writes[0]
    want = expected_create(words)
    if method != "POST" or body != want:
        problems.append(f"the POST body differs from the shape the host takes:\n  sent {json.dumps(body, ensure_ascii=False)}\n  want {json.dumps(want, ensure_ascii=False)}")
    # The new link is chosen in the list, and its settings open for editing.
    expect(window.locator(".live-win-row.on")).to_have_count(1)
    expect(window.locator(".live-pw[data-password='set']")).to_be_visible()
    press(window.locator(".live-pw-change"))
    window.locator("#live-password").fill("abc")
    window.locator(".live-save").click()
    expect(window.locator(".live-pw .field-error")).to_be_visible()
    window.locator("#live-password").fill("")
    press(window.get_by_role("button", name="Keep the current one" if lang == "en" else "Оставить текущий"))
    window.locator("#live-label").fill("Анна — зимнее меню")
    window.locator("#live-limit-usd").fill("7,5")
    window.locator(".live-save").click()
    deadline = time.monotonic() + 5
    while len([w for w in api_stub.LIVE.writes if w[0] == "PATCH"]) < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    patches = [w for w in api_stub.LIVE.writes if w[0] == "PATCH"]
    if len(patches) < 2:
        problems.append(f"the edit sent {len(patches)} PATCH(es), wanted the refused one and the saved one")
    else:
        refused, saved = patches[0][2], patches[1][2]
        if refused.get("password") != "abc":
            problems.append(f"the refused PATCH did not carry the short password: {refused}")
        if saved.get("label") != "Анна — зимнее меню" or saved.get("policy", {}).get("budget_usd") != 7.5 or "password" in saved or "clear_password" in saved:
            problems.append(f"the saved PATCH is not the edit: {saved}")
    page.locator(".live-win-body, .live-sheet .sheet-body").evaluate_all("els => els.forEach(e => { e.scrollTop = 0; })")
    shot(page, f"window-edit-{width}-{scheme}-{lang}")
    shot(page, f"window-edit-limits-{width}-{scheme}-{lang}", at="limits")
    context.close()


def visitor(browser, lang: str, width: int, height: int, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    global DOOR
    words = WORDS[lang]
    limits = {"roles": {"main": {"preset": "claude-opus-5", "effort": "high", "account": "", "allowed": []}},
              "budget": {"usd": {"limit": 5, "spent": 1.2}, "weekly_pct": None}, "terminals": "none"}
    DOOR = VisitDoor(SESSION, "Меню пекарни на зиму", password=PASSWORD, limits=limits, tries=3, retry_after=3)
    SENDS.clear()
    context = browser.new_context(viewport={"width": width, "height": height})
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{ORIGIN}/v/{TOKEN}/?lang={lang}&scheme=dark")
    form = page.locator("[data-unlock]")
    expect(form).to_be_visible(timeout=15000)
    if page.evaluate("document.documentElement.scrollWidth - innerWidth") > 0:
        problems.append(f"{width}: the password screen scrolls sideways")
    if "Меню" in page.content():
        problems.append("the password screen shows the conversation's title before unlocking")
    shot(page, f"visitor-lock-{width}-{lang}")
    field = page.locator("#visit-password")
    field.fill("nope")
    page.locator(".visit-lock-open").click()
    expect(page.locator("#visit-lock-problem")).to_have_text(words["wrong"])
    field.fill("still-wrong")
    page.locator(".visit-lock-open").click()
    page.wait_for_timeout(200)
    field.fill("third-wrong")
    page.locator(".visit-lock-open").click()
    expect(page.locator(".visit-lock-open")).to_be_disabled()
    expect(page.locator(".visit-lock-open")).to_contain_text("3" if lang == "en" else "3")
    shot(page, f"visitor-lock-slow-{width}-{lang}")
    expect(page.locator(".visit-lock-open")).to_be_enabled(timeout=6000)
    field.fill(PASSWORD)
    page.locator(".visit-lock-open").click()
    expect(page.locator(".visit-bar")).to_be_visible(timeout=10000)
    # The model is locked: shown, with no menu behind it.
    expect(page.locator(".composer .model-select.locked")).to_be_visible(timeout=10000)
    expect(page.locator(".composer button.model-select")).to_have_count(0)
    expect(page.locator("[data-budget-left]")).to_contain_text("$3.80")
    if page.locator(".visit.no-terminals").count() != 1:
        problems.append("the visitor's page does not know terminals are off")
    DOOR.spent = True
    page.locator(".composer .composer-field").fill("Ещё один вопрос")
    page.locator(".composer .roundbtn.primary").click()
    expect(page.locator(".visit-spent")).to_contain_text(words["spent"], timeout=5000)
    if 402 not in SENDS:
        problems.append("the send was never refused with 402")
    shot(page, f"visitor-spent-{width}-{lang}")
    context.close()
    DOOR = None


def run() -> int:
    expect_app(BASE)
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        operator(browser, "ru", 1920, 1080, "dark", problems, full=True)
        operator(browser, "en", 1920, 1080, "dark", problems, full=True)
        operator(browser, "ru", 1366, 768, "dark", problems, full=True)
        operator(browser, "ru", 390, 844, "dark", problems, full=True)
        operator(browser, "ru", 1920, 1080, "light", problems, full=True)
        visitor(browser, "ru", 1920, 1080, problems)
        visitor(browser, "ru", 390, 844, problems)
        browser.close()
    for problem in problems:
        print(f"FAIL {problem}")
    missed = UNHANDLED.report()
    if problems or missed:
        return 1
    print("live link limits: ok")
    return 0


if __name__ == "__main__":
    sys.exit(run())
