"""The terminal on a phone, at 390 px with a touch screen, and refuse what does not behave.

The row of keys sends the bytes a shell expects, arrows included in the cursor mode the program asked
for; Ctrl is sticky for one key and locks on a double tap; the compose line sends text as a bracketed
paste when the program turned it on and presses Enter after it when asked; a keyboard's doubled word
is sent once; the soft keyboard (a shorter visible area) takes rows and never columns, with one RESIZE;
a pinch changes the font once, with one RESIZE after the gesture; a long press opens the text with the
phone's own selection and copies it; a staff member's terminal answers its request from buttons above
the keys and sends what is written to the member rather than into the program. Nothing scrolls
sideways at 390 or 360 px, and the Russian page has Russian words.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_terminal_phone.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Page, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FOCUS_WORDS, FocusStub, expect_app  # noqa: E402
from screenshots import S1, UNHANDLED, phone_tool, stub  # noqa: E402
from terminal_stub import DEBUG, TerminalStub, open_session, wait_live  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
TID = "t1aaaaaaaaaa"
PHONE = {"width": 390, "height": 844}

WORDS = {
    "en": {"compose": "Type a command…", "ctrl": "Control: tap for one key, twice to lock", "select": "Select text", "staff": "Write to Ira…", "needs": "Needs you"},
    "ru": {"compose": "Введите команду…", "ctrl": "Control: нажмите для одной клавиши, дважды — чтобы закрепить", "select": "Выделить текст", "staff": "Написать: Ira…", "needs": "Нужны вы"},
}


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def that(self, ok: bool, problem: str) -> None:
        if not ok:
            self.problems.append(problem)


def sideways(page: Page) -> int:
    return int(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))


def open_phone_terminal(context, term: TerminalStub, lang: str) -> Page:  # type: ignore[no-untyped-def]
    page = open_session(context, term, stub, BASE, S1, lang=lang)
    phone_tool(page, "terminal")
    page.wait_for_selector(".term-sheet .term-sheet-row", timeout=5000)
    page.locator(".term-sheet .term-sheet-row").first.click()
    page.wait_for_selector(f".term-phone .term-view[data-terminal-view='{TID}']", timeout=5000)
    wait_live(page, TID)
    page.wait_for_timeout(500)
    return page


def sent_after(term: TerminalStub, before: int) -> bytes:
    return term.inputs(TID)[before:]


def tap_key(page: Page, key: str) -> None:
    page.locator(f".term-keys .term-key[data-key='{key}']").tap()
    page.wait_for_timeout(120)


def keys(page: Page, term: TerminalStub, check: Check) -> None:
    for key, expected in (("esc", b"\x1b"), ("tab", b"\t"), ("up", b"\x1b[A"), ("left", b"\x1b[D"), ("pipe", b"|"), ("ctrl-c", b"\x03")):
        before = len(term.inputs(TID))
        tap_key(page, key)
        got = sent_after(term, before)
        check.that(got == expected, f"the {key} key sent {got!r}, not {expected!r}")
    # The program turns application cursor keys on (vim, less): the arrows follow it.
    term.emit(TID, "\x1b[?1h")
    page.wait_for_timeout(300)
    before = len(term.inputs(TID))
    tap_key(page, "up")
    tap_key(page, "right")
    got = sent_after(term, before)
    check.that(got == b"\x1bOA\x1bOC", f"after ESC[?1h the arrows sent {got!r}, not ESC O A, ESC O C")
    term.emit(TID, "\x1b[?1l")
    page.wait_for_timeout(300)
    # Holding an arrow repeats it, as a hardware key does.
    box = page.locator(".term-keys .term-key[data-key='down']").bounding_box()
    assert box
    before = len(term.inputs(TID))
    cdp = page.context.new_cdp_session(page)
    point = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2, "id": 0}
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    page.wait_for_timeout(800)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(200)
    repeated = sent_after(term, before).count(b"\x1b[B")
    print("held arrow repeated:", repeated)
    check.that(repeated >= 3, f"a held ↓ sent {repeated} times, not a repeat")


def sticky(page: Page, term: TerminalStub, check: Check, words: dict[str, str]) -> None:
    ctrl = page.locator(".term-keys .term-key[data-key='ctrl']")
    check.that(ctrl.get_attribute("aria-label") == words["ctrl"], f"Ctrl is labelled {ctrl.get_attribute('aria-label')!r}")
    page.locator(".term-phone .term-screen").tap()
    page.wait_for_timeout(200)
    # Tap Ctrl, then type c on the keyboard: one ^C, and Ctrl lets go.
    tap_key(page, "ctrl")
    check.that(ctrl.get_attribute("aria-pressed") == "true", "Ctrl did not arm")
    before = len(term.inputs(TID))
    page.keyboard.type("c")
    page.wait_for_timeout(200)
    got = sent_after(term, before)
    check.that(got == b"\x03", f"Ctrl then c sent {got!r}, not ^C")
    check.that(ctrl.get_attribute("aria-pressed") == "false", "Ctrl stayed armed after one key")
    before = len(term.inputs(TID))
    page.keyboard.type("c")
    page.wait_for_timeout(200)
    check.that(sent_after(term, before) == b"c", "the next c was not a plain c")
    # Ctrl on a key of the row: Ctrl+↑ is xterm's modified arrow.
    tap_key(page, "ctrl")
    before = len(term.inputs(TID))
    tap_key(page, "up")
    got = sent_after(term, before)
    check.that(got == b"\x1b[1;5A", f"Ctrl then ↑ sent {got!r}")
    # A quick double tap locks it until it is tapped again.
    ctrl.tap()
    ctrl.tap()
    page.wait_for_timeout(150)
    check.that("locked" in (ctrl.get_attribute("class") or ""), "a double tap did not lock Ctrl")
    page.locator(".term-phone .term-screen").tap()
    before = len(term.inputs(TID))
    page.keyboard.type("ab")
    page.wait_for_timeout(200)
    got = sent_after(term, before)
    check.that(got == b"\x01\x02", f"a locked Ctrl sent {got!r} for ab")
    tap_key(page, "ctrl")
    check.that(ctrl.get_attribute("aria-pressed") == "false", "a third tap did not let Ctrl go")


def doubled(page: Page, term: TerminalStub, check: Check) -> None:
    # What Gboard does after a space: the letters one by one, then the whole word again when its
    # composition ends; and a chunk delivered twice in the same instant.
    before = len(term.inputs(TID))
    page.evaluate(f"() => {{ const h = window.__terminals; h.feed('{TID}', ' '); h.feed('{TID}', 'l'); h.feed('{TID}', 's'); h.feed('{TID}', 'ls'); h.feed('{TID}', ' '); h.feed('{TID}', 'git'); h.feed('{TID}', 'git'); }}")
    page.wait_for_timeout(300)
    got = sent_after(term, before)
    check.that(got == b" ls git", f"the doubled input reached the shell as {got!r}, not ' ls git'")


def compose(page: Page, term: TerminalStub, check: Check, words: dict[str, str]) -> None:
    field = page.locator(".term-compose-field")
    check.that(field.get_attribute("placeholder") == words["compose"], f"the compose line says {field.get_attribute('placeholder')!r}")
    size = field.evaluate("(e) => parseFloat(getComputedStyle(e).fontSize)")
    check.that(size >= 16, f"the compose line is {size}px, and Safari would zoom into it")
    before = len(term.inputs(TID))
    field.fill("echo hi")
    page.locator(".term-compose-send").tap()
    page.wait_for_timeout(300)
    got = sent_after(term, before)
    check.that(got == b"echo hi\r", f"without bracketed paste the line sent {got!r}")
    check.that(field.input_value() == "", "the compose line kept its text after sending")
    term.emit(TID, "\x1b[?2004h")
    page.wait_for_timeout(300)
    before = len(term.inputs(TID))
    field.fill("echo a\necho b")
    page.locator(".term-compose-send").tap()
    page.wait_for_timeout(300)
    got = sent_after(term, before)
    check.that(got == b"\x1b[200~echo a\recho b\x1b[201~\r", f"with 2004 on the line sent {got!r}")
    page.locator(".term-compose-enter").tap()
    check.that(page.locator(".term-compose-enter").get_attribute("aria-pressed") == "false", "the Enter toggle did not turn off")
    before = len(term.inputs(TID))
    field.fill("ls")
    page.locator(".term-compose-send").tap()
    page.wait_for_timeout(300)
    got = sent_after(term, before)
    check.that(got == b"\x1b[200~ls\x1b[201~", f"with Enter off the line sent {got!r}")
    page.locator(".term-compose-enter").tap()
    term.emit(TID, "\x1b[?2004l")


def keyboard(page: Page, term: TerminalStub, check: Check) -> None:
    before = term.resizes(TID)
    size = page.evaluate(f"() => window.__terminals.size('{TID}')")
    # The soft keyboard opening: the visible area shrinks to 500 px and the page is told in steps.
    for height in (780, 700, 600, 520, 500):
        page.set_viewport_size({"width": 390, "height": height})
        page.wait_for_timeout(30)
    page.wait_for_timeout(700)
    after = term.resizes(TID)[len(before):]
    print("keyboard up:", size, "→", after)
    check.that(len(after) == 1, f"the keyboard's slide sent {len(after)} sizes, not one: {after}")
    if after:
        check.that(after[-1][2] == size["cols"], f"the keyboard changed the columns: {size['cols']} → {after[-1][2]}")
        check.that(after[-1][3] < size["rows"], "the keyboard did not take rows")
    check.that(page.evaluate("document.documentElement.dataset.keyboard") == "open", "the page does not know the keyboard is up")
    box = page.locator(".term-phone").bounding_box()
    keys = page.locator(".term-keys").bounding_box()
    check.that(bool(box and keys and box["y"] + box["height"] <= 500.5 and keys["y"] + keys["height"] <= 500.5), f"the layer is not the visible area: {box}, keys {keys}")
    page.set_viewport_size(PHONE)
    page.wait_for_timeout(700)
    back = term.resizes(TID)[len(before) + len(after):]
    check.that(len(back) == 1 and back[-1][3] == size["rows"] and back[-1][2] == size["cols"], f"closing the keyboard did not give the rows back once: {back}")


def pinch(page: Page, term: TerminalStub, check: Check) -> None:
    box = page.locator(".term-phone-screen").bounding_box()
    assert box
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    base = page.evaluate(f"() => window.__terminals.fontSize('{TID}')")
    size = page.evaluate(f"() => window.__terminals.size('{TID}')")
    before = len(term.resizes(TID))
    cdp = page.context.new_cdp_session(page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": cx - 50, "y": cy, "id": 0}, {"x": cx + 50, "y": cy, "id": 1}]})
    for spread in range(55, 72, 2):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": cx - spread, "y": cy, "id": 0}, {"x": cx + spread, "y": cy, "id": 1}]})
        page.wait_for_timeout(30)
    during = len(term.resizes(TID)) - before
    scaled = page.evaluate("document.querySelector('.term-phone-zoom').style.transform")
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(800)
    font = page.evaluate(f"() => window.__terminals.fontSize('{TID}')")
    stored = page.evaluate("localStorage.getItem('daedalus.term.font')")
    sizes = term.resizes(TID)[before:]
    print("pinch:", base, "→", font, "stored", stored, "live transform", scaled, "sizes", sizes)
    check.that(scaled.startswith("scale("), "the screen did not follow the fingers while pinching")
    check.that(during == 0, f"the pinch sent {during} sizes while the fingers moved")
    check.that(font == round(base * 71 / 50) or font == 22, f"the pinch set the font to {font}, not {round(base * 71 / 50)}")
    check.that(stored == str(font), f"the pinched size was not kept for the device: {stored}")
    check.that(len(sizes) == 1, f"the pinch sent {len(sizes)} sizes after the gesture, not one: {sizes}")
    if sizes:
        check.that(sizes[0][2] < size["cols"], "a larger font did not give fewer columns")
    check.that(page.evaluate("document.querySelector('.term-phone-zoom').style.transform") == "", "the pinch left the screen scaled")


def long_press(page: Page, term: TerminalStub, check: Check, words: dict[str, str]) -> None:
    box = page.locator(".term-phone-screen").bounding_box()
    assert box
    cdp = page.context.new_cdp_session(page)
    point = {"x": box["x"] + 60, "y": box["y"] + 40, "id": 0}
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [point]})
    page.wait_for_timeout(750)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_selector(".term-select .term-select-text", timeout=3000)
    text = page.locator(".term-select-text").inner_text()
    print("selection layer:", text[-120:].replace("\n", " | "))
    check.that("bakery (main) $ ls" in text and "package.json" in text, "the selection layer does not hold the terminal's text")
    check.that(page.locator(".term-select").get_attribute("aria-label") == words["select"], "the selection layer is not named")
    select = page.locator(".term-select-text").evaluate("(e) => getComputedStyle(e).userSelect || getComputedStyle(e).webkitUserSelect")
    check.that(select in ("text", "auto"), f"the text cannot be selected natively ({select})")
    # Select a word the way the handles would, and copy it.
    page.evaluate("""() => {
      const pre = document.querySelector('.term-select-text');
      const node = [...pre.childNodes].find((n) => n.nodeType === 3);
      const at = node.data.indexOf('package.json');
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + 'package.json'.length);
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    }""")
    page.wait_for_timeout(200)
    page.locator(".term-select-head .btn.primary").tap()
    page.wait_for_timeout(300)
    copied = page.evaluate("navigator.clipboard.readText()")
    check.that(copied == "package.json", f"Copy put {copied!r} on the clipboard")
    check.that(not page.locator(".term-select").count(), "the selection layer stayed after copying")
    # The header's menu opens it too.
    page.locator(".term-phone-head button[aria-haspopup]").last.tap()
    page.locator(".menu button", has_text=words["select"]).tap()
    page.wait_for_selector(".term-select", timeout=3000)
    page.locator(".term-select-head .iconbtn").tap()
    check.that(not page.locator(".term-select").count(), "the selection layer did not close")


def fits(page: Page, check: Check, where: str) -> None:
    for width in (390, 360):
        page.set_viewport_size({"width": width, "height": 844})
        page.wait_for_timeout(300)
        over = sideways(page)
        check.that(over <= 0, f"{where} at {width}px scrolls sideways by {over}px")
        layer = page.locator(".term-phone").bounding_box()
        check.that(bool(layer and layer["width"] <= width + 0.5), f"{where} at {width}px: the layer is {layer}")
    page.set_viewport_size(PHONE)
    page.wait_for_timeout(300)


def shell(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    term = TerminalStub(S1)
    term.add(TID, title="bash · bakery")
    term.emit(TID, "bakery (main) $ ls\r\nsrc  package.json  README.md\r\nbakery (main) $ ")
    context = browser.new_context(viewport=PHONE, color_scheme="dark", is_mobile=True, has_touch=True, permissions=["clipboard-read", "clipboard-write"])
    context.add_init_script(DEBUG)
    context.add_init_script("try { localStorage.removeItem('daedalus.term.font'); localStorage.setItem('daedalus.term.renderer', 'dom'); } catch (e) {}")
    page = open_phone_terminal(context, term, lang)
    first = term.resizes(TID)
    print(lang, "first size:", first)
    check.that(len(first) == 1, f"opening the phone's terminal sent {len(first)} sizes")
    fits(page, check, f"{lang} phone terminal")
    if lang == "en":
        keys(page, term, check)
        sticky(page, term, check, words)
        doubled(page, term, check)
        compose(page, term, check, words)
        keyboard(page, term, check)
        pinch(page, term, check)
        long_press(page, term, check, words)
    else:
        sticky(page, term, check, words)
        compose(page, term, check, words)
        long_press(page, term, check, words)
    page.locator(".term-phone-head button[aria-label]").first.tap()
    page.wait_for_timeout(300)
    check.that(not page.locator(".term-phone").count(), "Back did not close the phone's terminal")
    context.close()


def staff(browser, lang: str, check: Check) -> None:  # type: ignore[no-untyped-def]
    """Ira's terminal: her request answered above the keys, a message to her through the team."""
    words, invented = WORDS[lang], FOCUS_WORDS[lang]
    focus = FocusStub.bakery(lang)
    focus.ask_from_ira(lang)
    term = TerminalStub(S1)
    term.add("tm-ira", title="claude · Ira", owner_kind="staff", owner_id="st-ira", project_id=focus.projects[0]["id"], owner_label="Ira")
    term.emit("tm-ira", "⏺ Update(src/lib/cart.ts)\r\n  ⎿  Updated with 6 additions\r\n")
    context = browser.new_context(viewport=PHONE, color_scheme="dark", is_mobile=True, has_touch=True)
    context.add_init_script(DEBUG)
    page = context.new_page()

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):]
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = focus.answer(request.method, path, url.query, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return stub(route)

    page.route("**/api/**", handle)
    term.install(page)
    page.goto(f"{BASE}/terminals/tm-ira?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".term-phone .term-view[data-terminal-view='tm-ira']", timeout=15000)
    wait_live(page, "tm-ira")
    page.wait_for_selector(".term-phone-actions .ask-answers-row .btn", timeout=5000)
    options = [b.strip() for b in page.locator(".term-phone-actions .ask-answers-row .btn").all_inner_texts()]
    print(lang, "Ira's request above the keys:", options)
    check.that(options[:2] == [invented["ask.before"], invented["ask.after"]], f"the actions slot shows {options}")
    actions = page.locator(".term-phone-actions").bounding_box()
    keys_box = page.locator(".term-keys").bounding_box()
    check.that(bool(actions and keys_box and actions["y"] + actions["height"] <= keys_box["y"] + 0.5), "the request's answers are not above the keys")
    field = page.locator(".term-compose-field")
    check.that(field.get_attribute("placeholder") == words["staff"], f"Ira's compose line says {field.get_attribute('placeholder')!r}")
    typed = len(term.inputs("tm-ira"))
    field.fill("use the owner's sheet")
    page.locator(".term-compose-send").tap()
    page.wait_for_timeout(500)
    check.that(focus.sent[-1:] == [("st-ira", {"text": "use the owner's sheet", "when": "now"})], f"the message to Ira went as {focus.sent}")
    check.that(len(term.inputs("tm-ira")) == typed, "the message to Ira was typed into her program")
    page.locator(".term-phone-actions .ask-answers-row .btn").first.tap()
    page.wait_for_timeout(800)
    check.that(focus.answers[-1:] == [("ask-ira", {"selected": [invented["ask.before"]]})], f"the answer went as {focus.answers}")
    check.that(not page.locator(".term-phone-actions").count(), "the answered request stayed above the keys")
    check.that(sideways(page) <= 0, "Ira's terminal scrolls sideways")
    context.close()


def main() -> int:
    expect_app(BASE)
    check = Check()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            shell(browser, lang, check)
            staff(browser, lang, check)
        browser.close()
    for problem in check.problems:
        print("PROBLEM:", problem)
    if check.problems:
        return 1
    print("phone terminal: ok")
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
