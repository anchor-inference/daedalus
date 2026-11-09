"""Replying to a passage of an agent's answer, in a real browser.

The unit tests pin the wire form (``miniapp/src/quotereply.test.ts``). What only a browser shows is the
way through the screen: that words selected in an agent's answer raise a pill with Reply and Ask on the
side (above the selection on a desktop, below it on a phone, where the system's copy menu sits above),
and that words selected in the operator's own message raise nothing; that Reply opens a popover (a
sheet on a phone) with the quote and a field, keeps the passage lit while the field has the focus, adds
the reply on Enter and gives it up on Escape; that the replies wait in the composer as cards which open
to a dialog that edits them and lose one to its ✕; that they survive a reload; that Send with an empty
field still sends, in the documented form; that the sent message is drawn as quotes and words with no
comment marker showing, and that a quote leads back to its passage; and that Ask on the side opens the
side question with the passage quoted in its field. In English and Russian, at 1440×900 and 390×844.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -sfn "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_reply_quote.py

``SHOTS=<dir>`` also writes screenshots of the pill, the popover, the cards and the sent message there, in
dark and light. Exit status is the number of failed checks.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, folders, fulfil_shared  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
CHAT = "chat-1"
WORDS = {
    "en": {
        "ask": "Make the menu show prices",
        "answer": "The prices now come from menu.json, which the page reads at build time.\n\nWhat changed:\n\n- each item has a price field\n- the page formats it in the visitor's currency\n\n```\nnpm run build\nnpm test\n```",
        "first": "which the page reads at build time",
        "list": ("each item has a price field", "the page formats it in the visitor's currency"),
        "one": "why at build time?", "two": "keep the currency fixed", "edited": "why at build time and not live?",
        "reply": "Reply", "aside": "Ask on the side", "placeholder": "Reply to the agent…",
    },
    "ru": {
        "ask": "Сделай меню с ценами",
        "answer": "Цены теперь берутся из menu.json, который страница читает при сборке.\n\nЧто изменилось:\n\n- у каждого блюда есть поле price\n- страница показывает его в валюте посетителя\n\n```\nnpm run build\nnpm test\n```",
        "first": "который страница читает при сборке",
        "list": ("у каждого блюда есть поле price", "страница показывает его в валюте посетителя"),
        "one": "почему при сборке?", "two": "валюту оставь одну", "edited": "почему при сборке, а не на лету?",
        "reply": "Ответить", "aside": "Спросить по ходу", "placeholder": "Ответ агенту…",
    },
}
PRESETS = {"opus": {"provider": "claude", "model": "claude-opus-5", "label": "Claude Opus 5", "thinking": True, "reasoning_effort": "high", "images": True, "context_window": 200000, "max_output_tokens": 32000}}


def message(seq: int, role: str, text: str, **over: object) -> dict:
    return {"role": role, "summary": False, "internal": False, "origin": "operator" if role == "user" else "", "seq": seq, "compaction": None, "headline": "", "text": text, "thinking": "", "tool_calls": [], "tool_results": [], "created_at": f"2026-11-04T12:00:{seq % 60:02d}+00:00", "model": "", "provider": "", "fallback": None, **over}


class Host:
    """What the invented host holds, and what the app sent it."""

    def __init__(self, lang: str) -> None:
        words = WORDS[lang]
        self.messages = [message(1, "user", words["ask"]), message(2, "assistant", words["answer"])]
        self.sent: list[dict] = []

    def detail(self) -> dict:
        return {
            "id": CHAT, "title": "Bakery site", "status": "idle", "error": "", "run_id": None,
            "workspace": "/workspace/bakery", "workspace_name": "bakery", "workspace_own": False, "workspace_sessions": [], "pending": None,
            "model": "Claude Opus 5", "provider": "claude", "project": {"id": "p", "name": "Bakery", "folders": folders("/workspace/bakery"), "settings": {"snapshots": True}},
            "folders": [], "configured_model": "claude-opus-5", "effective_model": "claude-opus-5", "fallback": None, "thinking": True, "reasoning_effort": "high",
            "yagni": False, "brief": "", "tools_off": [], "loop": None, "services": [], "subagents": [], "usage": {},
            "context": {"tokens": 4000, "window": 200000, "messages": len(self.messages), "summaries": 0, "operator_turns": 1},
            "prompt_coach_of": None, "btw_of": None, "messages": self.messages,
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
        if rel == f"/api/sessions/{CHAT}/messages" and req.method == "POST":
            HOST.sent.append(data or {})
            seq = max(m["seq"] for m in HOST.messages) + 1
            HOST.messages.append({**message(seq, "user", str((data or {}).get("text") or "")), "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "client_message_id": (data or {}).get("client_message_id")})
            body = {"run_id": f"run-{seq}", "receipt": {"status": "consumed", "run_id": f"run-{seq}", "client_message_id": (data or {}).get("client_message_id")}}
        else:
            body = {}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
    if rel == "/api/auth/me":
        body = {"user_id": 1, "via": "token"}
    elif rel == f"/api/sessions/{CHAT}":
        body = HOST.detail()
    elif rel.startswith(f"/api/sessions/{CHAT}/"):
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


SELECT = """([selector, start, end]) => {
    // Select from the text `start` to the end of the text `end` inside the element `selector`, as a
    // drag or a long press would: the browser's own Selection, which fires selectionchange.
    const root = document.querySelector(selector);
    if (!root) return null;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let from = null, to = null;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const i = node.data.indexOf(start);
        if (!from && i >= 0) from = [node, i];
        const j = node.data.indexOf(end);
        if (from && j >= 0) { to = [node, j + end.length]; break; }
    }
    if (!from || !to) return null;
    const range = document.createRange();
    range.setStart(from[0], from[1]);
    range.setEnd(to[0], to[1]);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    const rect = range.getBoundingClientRect();
    return { top: rect.top, bottom: rect.bottom, text: selection.toString() };
}"""


def select(page: Page, selector: str, start: str, end: str) -> dict | None:
    got = page.evaluate(SELECT, [selector, start, end])
    page.wait_for_timeout(450)
    return got


def overflow(page: Page) -> list[str]:
    return page.evaluate(
        """() => {
            const out = [];
            if (document.documentElement.scrollWidth > innerWidth + 1) out.push(`page scrolls sideways (${document.documentElement.scrollWidth})`);
            for (const el of document.querySelectorAll('.reply-pill, .reply-popover, .reply-sheet, .composer .attachments')) {
                const r = el.getBoundingClientRect();
                if (r.right > innerWidth + 1 || r.left < -1) out.push(`${el.className} ${Math.round(r.left)}..${Math.round(r.right)}`);
            }
            return out;
        }"""
    )


def add_reply(page: Page, phone: bool, text: str, say) -> bool:  # type: ignore[no-untyped-def]
    pill = page.locator(".reply-pill")
    if not pill.count():
        say("no pill over the selection")
        return False
    pill.locator("[data-action=reply]").click()
    root = ".reply-sheet" if phone else ".reply-popover"
    try:
        page.wait_for_selector(f"{root} .reply-input", timeout=4000)
    except Exception:  # noqa: BLE001 - reported
        say(f"Reply did not open the {'sheet' if phone else 'popover'}")
        return False
    page.locator(f"{root} .reply-input").fill(text)
    page.locator(f"{root} .reply-input").press("Enter")
    page.wait_for_timeout(250)
    if page.locator(root).count():
        say("Enter did not add the reply and close the popover")
        return False
    return True


def run(browser, lang: str, phone: bool, scheme: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    global HOST
    HOST = Host(lang)
    words = WORDS[lang]
    where = f"{lang} {'390' if phone else '1440'}px {scheme}"
    say = lambda text: problems.append(f"{where}: {text}")  # noqa: E731
    shot = lambda name: page.screenshot(path=f"{SHOTS}/reply-{'phone' if phone else 'wide'}-{lang}-{scheme}-{name}.png") if SHOTS else None  # noqa: E731
    viewport = {"width": 390, "height": 844} if phone else {"width": 1440, "height": 900}
    context = browser.new_context(viewport=viewport, color_scheme=scheme, is_mobile=phone, has_touch=phone, device_scale_factor=2 if phone else 1)
    context.add_init_script("try { localStorage.setItem('daedalus.session.panel', '0'); localStorage.removeItem('daedalus.btw.position'); } catch (e) {}")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{CHAT}?token=t&scheme={scheme}&lang={lang}")
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.wait_for_selector(".answer[data-quote='2']", timeout=8000)
    page.evaluate(f"() => {{ localStorage.removeItem('daedalus.replies.{CHAT}'); localStorage.removeItem('daedalus.draft.{CHAT}'); }}")

    # The operator's own words raise nothing.
    select(page, ".msg-wrap", words["ask"][:4], words["ask"][-4:])
    if page.locator(".reply-pill").count():
        say("selecting the operator's own message raised the pill")

    # The first reply: a part of a sentence.
    rect = select(page, ".answer[data-quote='2']", words["first"][:6], words["first"])
    if not rect:
        say("could not select the answer's words")
        context.close()
        return
    pill = page.locator(".reply-pill")
    if not pill.count():
        say("selecting words in the answer raised no pill")
        context.close()
        return
    labels = pill.inner_text()
    if words["reply"] not in labels or words["aside"] not in labels:
        say(f"the pill does not offer Reply and Ask on the side ({labels!r})")
    box = pill.bounding_box()
    if box and not phone and box["y"] + box["height"] > rect["top"] + 1:
        say(f"the desktop pill is not above the selection ({box['y'] + box['height']:.0f} > {rect['top']:.0f})")
    if box and phone and box["y"] < rect["bottom"] - 1:
        say(f"the phone's pill is not below the selection ({box['y']:.0f} < {rect['bottom']:.0f})")
    page.wait_for_timeout(200)
    shot("pill")
    pill.locator("[data-action=reply]").click()
    root = ".reply-sheet" if phone else ".reply-popover"
    try:
        page.wait_for_selector(f"{root} .reply-input", timeout=4000)
    except Exception:  # noqa: BLE001 - reported
        say("Reply did not open the popover")
        context.close()
        return
    page.wait_for_timeout(200)  # the field takes the focus a frame after the popover is placed
    if words["first"] not in page.locator(f"{root} .reply-quote").inner_text():
        say("the popover does not show the quote")
    if page.locator(f"{root} .reply-input").get_attribute("placeholder") != words["placeholder"]:
        say("the popover's field does not say what it is for")
    if not page.evaluate("() => document.activeElement?.classList.contains('reply-input')"):
        say("the popover's field does not have the focus")
    if not page.evaluate("() => typeof CSS !== 'undefined' && CSS.highlights && CSS.highlights.has('reply-pick')"):
        say("the passage is not kept lit while the popover is open")
    page.locator(f"{root} .reply-input").fill(words["one"])
    page.wait_for_timeout(200)
    shot("popover")
    page.locator(f"{root} .reply-input").press("Enter")
    page.wait_for_timeout(250)
    if page.locator(root).count():
        say("Enter did not close the popover")
    if page.locator(".composer .reply-card").count() != 1:
        say(f"the composer holds {page.locator('.composer .reply-card').count()} reply cards after one reply")

    # Escape gives a reply up.
    select(page, ".answer[data-quote='2']", words["list"][0][:5], words["list"][0])
    if page.locator(".reply-pill").count():
        page.locator(".reply-pill [data-action=reply]").click()
        page.wait_for_selector(f"{root} .reply-input", timeout=4000)
        page.locator(f"{root} .reply-input").fill("never sent")
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        if page.locator(root).count():
            say("Escape did not close the popover")
        if page.locator(".composer .reply-card").count() != 1:
            say("Escape added the reply")
    else:
        say("no pill over a list item")

    # The second reply spans two list items; the third two lines of a code block.
    select(page, ".answer[data-quote='2']", words["list"][0][:5], words["list"][1])
    add_reply(page, phone, words["two"], say)
    select(page, ".answer[data-quote='2']", "npm run build", "npm test")
    add_reply(page, phone, "drop", say)
    cards = page.locator(".composer .reply-card")
    if cards.count() != 3:
        say(f"the composer holds {cards.count()} reply cards after three replies")
        context.close()
        return
    if words["one"] not in cards.nth(0).inner_text() or words["reply"].lower()[:4] not in cards.nth(0).inner_text().lower():
        say(f"the first card does not show the reply and its chip ({cards.nth(0).inner_text()!r})")
    page.locator(".composer .composer-field").first.blur()
    page.wait_for_timeout(250)
    shot("cards")
    off = overflow(page)
    if off:
        say(f"something runs off the side: {off}")

    # The dialog: the whole quote, and the words, edited.
    cards.nth(0).locator(".reply-card-open").click()
    try:
        page.wait_for_selector(".reply-dialog textarea", timeout=4000)
    except Exception:  # noqa: BLE001 - reported
        say("a card does not open the reply dialog")
        context.close()
        return
    if words["first"] not in page.locator(".reply-dialog .reply-quote").inner_text():
        say("the dialog does not show the whole quote")
    page.locator(".reply-dialog textarea").fill(words["edited"])
    page.wait_for_timeout(150)
    shot("dialog")
    page.locator(".reply-dialog .btn.primary").click()
    page.wait_for_timeout(200)
    if words["edited"] not in cards.nth(0).inner_text():
        say("the edit in the dialog did not reach the card")

    # A card's ✕ removes it (shown on hover on a desktop).
    third = cards.nth(2)
    if not phone:
        third.hover()
        page.wait_for_timeout(200)
    third.locator(".reply-card-x").click()
    page.wait_for_timeout(200)
    if cards.count() != 2:
        say(f"the ✕ did not remove the card ({cards.count()} left)")

    # A reload finds the replies where they were.
    page.reload()
    page.wait_for_selector(".composer .roundbtn.primary", timeout=15000)
    page.wait_for_timeout(400)
    if page.locator(".composer .reply-card").count() != 2:
        say(f"after a reload the composer holds {page.locator('.composer .reply-card').count()} reply cards, not 2")

    # Send with nothing typed: the replies alone are a message, in the documented form.
    send = page.locator(".composer .roundbtn.primary")
    if send.is_disabled():
        say("Send is disabled with replies and an empty field")
    send.click()
    page.wait_for_timeout(900)
    expected = (
        f"<!-- reply 1 msg=2 -->\n> {words['first']}\n\n{words['edited']}\n\n"
        f"<!-- reply 2 msg=2 -->\n> {words['list'][0]}\n> {words['list'][1]}\n\n{words['two']}"
    )
    sent = HOST.sent[-1]["text"] if HOST.sent else ""
    if sent != expected:
        say(f"the message sent is not the documented form:\n{sent!r}\n!=\n{expected!r}")
    if page.locator(".composer .reply-card").count():
        say("the cards stayed in the composer after the send")
    if page.evaluate(f"() => localStorage.getItem('daedalus.replies.{CHAT}')"):
        say("the replies stayed in storage after the send")
    try:
        page.wait_for_selector(".msg-wrap .sent-reply", timeout=6000)
    except Exception:  # noqa: BLE001 - reported
        say("the sent message is not drawn with its replies")
        context.close()
        return
    page.wait_for_timeout(300)
    bubble = page.locator(".msg-wrap").last
    text = bubble.inner_text()
    if "<!--" in text or re.search(r"(^|\n)> ", text):
        say(f"the sent message shows the raw markers ({text[:120]!r})")
    if page.locator(".msg-wrap .sent-replies").count() != 1:
        say(f"the sent message is drawn {page.locator('.msg-wrap .sent-replies').count()} times")
    if bubble.locator(".sent-reply").count() != 2:
        say(f"the sent message draws {bubble.locator('.sent-reply').count()} replies, not 2")
    page.locator(".chat-scroll").evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
    page.wait_for_timeout(250)
    shot("sent")
    bubble.locator(".sent-reply-quote").first.click()
    page.wait_for_timeout(300)
    if not page.evaluate("() => typeof CSS !== 'undefined' && CSS.highlights && CSS.highlights.has('reply-reveal')"):
        say("a sent quote does not light its passage in the answer")

    # Ask on the side: the side question opens with the passage quoted in its field.
    select(page, ".answer[data-quote='2']", words["first"][:6], words["first"])
    if page.locator(".reply-pill [data-action=aside]").count():
        page.locator(".reply-pill [data-action=aside]").click()
        side = ".btw-sheet" if phone else ".btw-window"
        try:
            page.wait_for_selector(f"{side} .btw-ask textarea", timeout=5000)
            # The quote goes into the field in the effect after the window is drawn.
            page.wait_for_timeout(300)
            value = page.locator(f"{side} .btw-ask textarea").input_value()
            if not value.startswith(f"> {words['first']}"):
                say(f"the side question's field does not start with the quote ({value!r})")
        except Exception:  # noqa: BLE001 - reported
            say("Ask on the side did not open the side question")
    else:
        say("the pill offers no Ask on the side")
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
                run(browser, "en", phone, "light", problems)
        browser.close()
    for problem in problems:
        print("FAIL", problem)
    if not problems:
        print("reply to a passage: all checks passed")
    return len(problems) + UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
