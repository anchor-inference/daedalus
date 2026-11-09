"""A fenced block in the composer is the code card an answer draws, as it is typed. English and
Russian, dark and light, 1440 px and a 360 px phone, the start page's composer and a chat's.

What is checked:

- typing three backticks and a label, then Enter, opens a card (the fence closes itself, the caret
  inside): monospaced, coloured by the label, with the label in its head and a copy button that copies
  the code alone; Enter inside the code is the next line of code, never Send;
- a line longer than the card scrolls inside the card, and on a 360 px phone the page never scrolls
  sideways;
- Russian through an input method lands whole, Undo and Redo take it back and bring it again, and a
  paste of rich text arrives as plain text (a pasted fence draws its card);
- the message sent carries the text exactly as typed, fences included, and the chat draws it, and
  an answer's block, as the same card, coloured, scrolling sideways;
- a chat's own composer draws the same card.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_composer_code.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import DEFAULT_APP, expect_app  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS_DIR", "")
LONG = "return a + b  # a comment long enough to run past the right edge of the card on any screen"
FIELD = ".start-composer .composer-field"


def serve(page: Page, posted: list[dict]) -> None:
    def route(r) -> None:  # type: ignore[no-untyped-def]
        request = r.request
        path = urlsplit(request.url).path
        path = path[path.index("/api/"):]
        if request.method == "POST" and path == "/api/sessions":
            posted.append({"path": path, "body": request.post_data_json})
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"id": shots.S1}))
        if request.method == "POST" and path.startswith(f"/api/sessions/{shots.S1}/"):
            posted.append({"path": path, "body": request.post_data_json})
            return r.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "run_id": "r1"}))
        return shots.stub(r)

    page.route("**/api/**", route)


def text_of(page: Page, selector: str = FIELD) -> str:
    """The field's text as the composer holds it: lines joined by newlines, the copy button left out."""
    return page.evaluate("""s => [...document.querySelector(s).querySelectorAll('[data-line]')].map(l => l.textContent).join('\\n')""", selector)


def type_block(page: Page) -> None:
    page.keyboard.type("Look at this:")
    page.keyboard.press("Shift+Enter")
    page.keyboard.type("```python")
    page.keyboard.press("Enter")
    page.keyboard.type("def add(a, b):")
    page.keyboard.press("Enter")
    page.keyboard.type(LONG)


def desktop(browser, lang: str, scheme: str, check) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} {scheme} 1440"
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=scheme)
    context.grant_permissions(["clipboard-read", "clipboard-write"])
    page = context.new_page()
    posted: list[dict] = []
    serve(page, posted)
    page.goto(f"{BASE}/agents?token=t&lang={lang}&scheme={scheme}")
    page.wait_for_selector(FIELD, timeout=15000)
    page.locator(FIELD).click()
    type_block(page)
    card = page.locator(f"{FIELD} .cf-card")
    check(card.count() == 1, f"{where}: three backticks and a label open a card")
    check(text_of(page) == f"Look at this:\n```python\ndef add(a, b):\n{LONG}\n```", f"{where}: the fence closed itself, Enter stayed in the code ({text_of(page)!r})")
    check(not posted, f"{where}: Enter inside the code sent nothing")
    check(card.get_attribute("data-lang") == "python" and card.locator(".cf-lang").inner_text() == "python", f"{where}: the label heads the card")
    check(card.locator(".tk-k").count() >= 2 and card.locator(".tk-f").count() >= 1, f"{where}: the code is coloured by its label")
    font = page.evaluate(f"() => getComputedStyle(document.querySelector('{FIELD} .cf-code')).fontFamily")
    prose = page.evaluate(f"() => getComputedStyle(document.querySelector('{FIELD}')).fontFamily")
    check(font != prose and "mono" in font.lower(), f"{where}: the code is monospaced ({font!r})")
    widths = page.evaluate(f"() => {{ const c = document.querySelector('{FIELD} .cf-code'); return [c.scrollWidth, c.clientWidth]; }}")
    check(widths[0] > widths[1], f"{where}: the long line scrolls inside the card {widths}")
    lines = page.evaluate(f"() => document.querySelectorAll('{FIELD} .cf-code .cf-line').length")
    check(lines == 2, f"{where}: the long line does not wrap ({lines} lines)")
    if SHOTS:
        page.locator(".start-composer").screenshot(path=f"{SHOTS}/composer-code-{lang}-{scheme}.png")
    card.locator("button[data-copy]").click()
    copied = page.evaluate("() => navigator.clipboard.readText()")
    check(copied == f"def add(a, b):\n{LONG}", f"{where}: copy takes the code alone ({copied!r})")
    # Out of the card and send: the message is the text as typed.
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("ArrowDown")
    page.keyboard.type("Thanks")
    page.keyboard.press("Enter")
    page.wait_for_function("() => location.pathname.includes('/agents/')", timeout=10000)
    sent = [p["body"] for p in posted if p["path"].endswith("/messages")]
    check(bool(sent) and sent[0].get("text") == f"Look at this:\n```python\ndef add(a, b):\n{LONG}\n```\nThanks", f"{where}: the message is the text as typed ({sent})")
    context.close()


def editing(browser, check) -> None:  # type: ignore[no-untyped-def]
    where = "editing"
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    serve(page, [])
    page.goto(f"{BASE}/agents?token=t&lang=ru")
    page.wait_for_selector(FIELD, timeout=15000)
    page.locator(FIELD).click()
    cdp = page.context.new_cdp_session(page)
    # A Russian word through an input method: nothing is redrawn under the composition, and it lands whole.
    cdp.send("Input.imeSetComposition", {"text": "прив", "selectionStart": 4, "selectionEnd": 4})
    cdp.send("Input.imeSetComposition", {"text": "привет", "selectionStart": 6, "selectionEnd": 6})
    cdp.send("Input.insertText", {"text": "привет"})
    page.keyboard.type(" мир")
    check(text_of(page) == "привет мир", f"{where}: the composed word and the typed ones land ({text_of(page)!r})")
    page.keyboard.press("Control+z")
    check(text_of(page) == "привет", f"{where}: Undo takes back the last word ({text_of(page)!r})")
    page.keyboard.press("Control+Shift+z")
    check(text_of(page) == "привет мир", f"{where}: Redo brings it again ({text_of(page)!r})")
    page.evaluate("""s => { const dt = new DataTransfer(); dt.setData('text/plain', '\\n```sh\\nls -la\\n```'); dt.setData('text/html', '<b>bold</b>');
      document.querySelector(s).dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""", FIELD)
    check(page.locator(f"{FIELD} .cf-card[data-lang='sh']").count() == 1, f"{where}: a pasted fence draws its card")
    check(page.locator(f"{FIELD} b").count() == 0, f"{where}: rich text arrives plain")
    check(text_of(page) == "привет мир\n```sh\nls -la\n```", f"{where}: the paste is the text pasted ({text_of(page)!r})")
    page.close()


def phone(browser, lang: str, check) -> None:  # type: ignore[no-untyped-def]
    where = f"{lang} 360"
    page = browser.new_page(viewport={"width": 360, "height": 780}, is_mobile=True, has_touch=True)
    serve(page, [])
    page.goto(f"{BASE}/agents?token=t&lang={lang}&scheme=dark")
    page.wait_for_selector(FIELD, timeout=15000)
    page.locator(FIELD).click()
    page.keyboard.type("```js")
    page.keyboard.press("Enter")
    page.keyboard.type("const veryLongName = someFunction(argumentNumberOne, argumentNumberTwo, three);")
    wide = page.evaluate("() => [document.documentElement.scrollWidth, innerWidth]")
    check(wide[0] <= wide[1], f"{where}: the page does not scroll sideways {wide}")
    box = page.locator(f"{FIELD} .cf-card").bounding_box()
    check(bool(box) and box["x"] >= 0 and box["x"] + box["width"] <= 360, f"{where}: the card stays inside the screen ({box})")
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/composer-code-phone-{lang}.png")
    page.close()


def chat(browser, lang: str, check) -> None:  # type: ignore[no-untyped-def]
    """A chat's own composer draws the same card."""
    where = f"{lang} chat"
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/api/**", shots.stub)
    page.goto(f"{BASE}/agents/{shots.S1}?token=t&lang={lang}")
    field = ".chat .composer .composer-field"
    page.wait_for_selector(field, timeout=15000)
    page.locator(field).click()
    page.keyboard.type("```ts")
    page.keyboard.press("Enter")
    page.keyboard.type("const x: number = 1;")
    check(page.locator(f"{field} .cf-card[data-lang='ts'] .tk-k").count() >= 1, f"{where}: the chat's composer draws a coloured card")
    page.close()


def sent_message(browser, lang: str, scheme: str, check) -> None:  # type: ignore[no-untyped-def]
    """The operator's message and the agent's answer draw a fenced block as one card, coloured alike."""
    where = f"{lang} {scheme} sent"
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    code = "```python\ndef add(a, b):\n    return a + b  # " + "long " * 30 + "\n```"

    def route(r) -> None:  # type: ignore[no-untyped-def]
        path = urlsplit(r.request.url).path
        if r.request.method == "GET" and path.endswith(f"/api/sessions/{shots.S5}"):
            body = shots.detail(shots.S5)
            body["messages"] = [
                {"role": "user", "seq": 1, "text": f"Why does this fail?\n{code}", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": shots.ago(minutes=3)},
                {"role": "assistant", "seq": 2, "text": f"It does not; here it is again:\n{code}", "thinking": "", "tool_calls": [], "tool_results": [], "created_at": shots.ago(minutes=2)},
            ]
            return shots.respond(r, body)
        return shots.stub(r)

    page.route("**/api/**", route)
    page.goto(f"{BASE}/agents/{shots.S5}?token=t&lang={lang}&scheme={scheme}")
    page.wait_for_selector(".msg.user .codecard", timeout=15000)
    mine = page.locator(".msg.user .codecard").first
    theirs = page.locator(".answer .codecard").first
    check(mine.locator(".tk-k").count() >= 2 and theirs.locator(".tk-k").count() >= 2, f"{where}: both cards are coloured")
    check(mine.locator(".codehead").inner_text().strip() == "python", f"{where}: the label heads the operator's card")
    widths = mine.evaluate("el => { const p = el.querySelector('pre'); return [p.scrollWidth, p.clientWidth]; }")
    check(widths[0] > widths[1], f"{where}: a long line scrolls inside the card {widths}")
    if SHOTS:
        page.locator(".timeline").screenshot(path=f"{SHOTS}/sent-code-{lang}-{scheme}.png")
    page.close()


def main() -> int:
    expect_app(BASE)
    problems: list[str] = []

    def check(ok: bool, what: str) -> None:
        if not ok:
            problems.append(what)
            print("FAIL", what)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for scheme in ("dark", "light"):
                desktop(browser, lang, scheme, check)
                sent_message(browser, lang, scheme, check)
            phone(browser, lang, check)
            chat(browser, lang, check)
        editing(browser, check)
        browser.close()
    print("composer code:", "ok" if not problems else f"{len(problems)} FAILED")
    return (1 if problems else 0) + shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
