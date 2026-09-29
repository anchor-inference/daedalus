"""The clipboard between the operator and the agent's browser, in a real browser, and refuse what does not
behave.

In English and Russian, at 1440 × 900 with a keyboard and at 390 × 844 with fingers:

- while the operator drives, Ctrl+V puts the operator's clipboard on the page as `text` and sends no
  key, so the page's browser never pastes its own, empty clipboard;
- Ctrl+C asks the page for its selection before the key goes, and the answer lands on the operator's
  clipboard; Ctrl+X does the same, its key following the copy so the cut deletes what was read;
- a copy while the focus is on a password field copies nothing and says so;
- the panel's menu offers Paste and Copy while the operator drives;
- a phone's row of keys has Paste and Copy, and a clipboard the browser will not let the app read
  points at the line under the page;
- the app does not copy or paste for a window that does not hold the page.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_browser_clipboard.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, open_page, render_scenes, wait_frames  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ORIGIN = "{0.scheme}://{0.netloc}".format(urlsplit(BASE))
ROOT = ".panel .bp"
CLIPS = ["clipboard-read", "clipboard-write"]

WORDS = {
    "en": {"withheld": "A password field is never copied", "copied": "Copied 19 characters", "long_press": "long-press the line below", "paste": "Paste onto the page", "copy": "Copy from the page"},
    "ru": {"withheld": "Поле пароля не копируется", "copied": "Скопировано 19 символов", "long_press": "удерживайте строку ниже", "paste": "Вставить на страницу", "copy": "Скопировать со страницы"},
}
PASSWORD = "пароль-1 & \"secret\""
EMAIL = "someone@example.com"


def clipboard(page) -> str:  # type: ignore[no-untyped-def]
    return page.evaluate("() => navigator.clipboard.readText()")


def set_clipboard(page, text: str) -> None:  # type: ignore[no-untyped-def]
    page.evaluate("(t) => navigator.clipboard.writeText(t)", text)


def toast_text(page) -> str:  # type: ignore[no-untyped-def]
    toast = page.locator(".toast")
    toast.wait_for(timeout=5000)
    return toast.inner_text()


def desktop(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] desktop: {text}")  # noqa: E731
    bs = BrowserStub(scenes)
    group = bs.add("g1", scene="shop", owner_id=S1)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.grant_permissions(CLIPS, origin=ORIGIN)
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}&panel=browser")
    page.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=10000)
    wait_frames(page, ROOT, 1)
    live = next(c for c in bs.clients if c.tier == "live" and not c.closed)

    # Not driving: the keyboard is the app's, and nothing crosses.
    set_clipboard(page, "left alone")
    page.keyboard.press("Control+c")
    page.wait_for_timeout(300)
    if any(i["t"] == "copy" for i in bs.inputs("g1")):
        say("a copy went to the page from a window that does not hold it")

    page.locator(f"{ROOT} .bp-control.take").click()
    page.wait_for_selector(f"{ROOT} .bv.driving", timeout=5000)
    viewer = page.locator(f"{ROOT} .bv").bounding_box()
    page.mouse.click(viewer["x"] + 300, viewer["y"] + 300)
    page.wait_for_timeout(200)

    # Ctrl+V: the operator's clipboard, as text, and no key the page's browser would paste with.
    set_clipboard(page, PASSWORD)
    page.locator(f"{ROOT} .bv-ime").focus()
    start = len(bs.inputs("g1", live))
    page.keyboard.press("Control+v")
    page.wait_for_timeout(400)
    sent = bs.inputs("g1", live)[start:]
    print(f"[{lang}] Ctrl+V sent: {sent}")
    if [i.get("text") for i in sent if i["t"] == "text"] != [PASSWORD]:
        say(f"Ctrl+V put {sent} on the page, not the clipboard's text")
    if any(i["t"] == "key" for i in sent):
        say(f"Ctrl+V was also sent as keys: {sent}")

    # Ctrl+C: the copy is asked for, then the key; the answer is on the operator's clipboard.
    group.selection = EMAIL
    start = len(bs.inputs("g1", live))
    page.keyboard.press("Control+c")
    page.wait_for_timeout(500)
    sent = bs.inputs("g1", live)[start:]
    got = clipboard(page)
    print(f"[{lang}] Ctrl+C sent: {sent}; clipboard {got!r}")
    kinds = [(i["t"], i.get("type"), i.get("key")) for i in sent]
    if kinds[:2] != [("copy", None, None), ("key", "down", "c")]:
        say(f"Ctrl+C went as {kinds}, not a copy and then the key")
    if got != EMAIL:
        say(f"the clipboard holds {got!r} after a copy of {EMAIL!r}")
    if WORDS[lang]["copied"] not in toast_text(page):
        say(f"the copy says {toast_text(page)!r}")

    # Ctrl+X: the same copy, and the cut's key after it.
    group.selection = "cut me"
    start = len(bs.inputs("g1", live))
    page.keyboard.press("Control+x")
    page.wait_for_timeout(500)
    sent = bs.inputs("g1", live)[start:]
    kinds = [(i["t"], i.get("type"), i.get("key")) for i in sent]
    if kinds[:2] != [("copy", None, None), ("key", "down", "x")] or clipboard(page) != "cut me":
        say(f"Ctrl+X went as {kinds} and left {clipboard(page)!r}")

    # A password field: nothing copied, and the operator is told.
    set_clipboard(page, "before")
    group.selection = PASSWORD
    group.selection_withheld = True
    page.locator(f"{ROOT} .bv-ime").focus()
    page.keyboard.press("Control+c")
    page.wait_for_timeout(500)
    if clipboard(page) != "before":
        say(f"a copy from a password field wrote {clipboard(page)!r}")
    if WORDS[lang]["withheld"] not in toast_text(page):
        say(f"a copy from a password field says {toast_text(page)!r}")
    group.selection_withheld = False

    # The menu's Paste and Copy, for a mouse alone.
    set_clipboard(page, "from the menu")
    start = len(bs.inputs("g1", live))
    page.locator(f"{ROOT} .bp-toolbar button[aria-haspopup='menu']").last.click()
    item = page.get_by_role("menuitem", name=WORDS[lang]["paste"])
    if not item.count():
        say("the menu has no Paste while the operator drives")
    else:
        item.click()
        page.wait_for_timeout(400)
        texts = [i.get("text") for i in bs.inputs("g1", live)[start:] if i["t"] == "text"]
        if texts != ["from the menu"]:
            say(f"the menu's Paste sent {texts}")
    group.selection = "menu copy"
    page.locator(f"{ROOT} .bp-toolbar button[aria-haspopup='menu']").last.click()
    item = page.get_by_role("menuitem", name=WORDS[lang]["copy"])
    if not item.count():
        say("the menu has no Copy while the operator drives")
    else:
        item.click()
        page.wait_for_timeout(500)
        if clipboard(page) != "menu copy":
            say(f"the menu's Copy left {clipboard(page)!r}")
    context.close()


def phone(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] phone: {text}")  # noqa: E731
    bs = BrowserStub(scenes)
    gid = f"g-{lang}"
    group = bs.add(gid, scene="shop", owner_id=S1)
    context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.browser.announced', JSON.stringify(['" + gid + "'])); } catch (e) {}")
    context.grant_permissions(CLIPS, origin=ORIGIN)
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}", wait=".browser-headbtn")
    page.locator(".browser-headbtn").tap()
    page.wait_for_selector(".panel-sheet .bp .bv[data-state='live']", timeout=10000)
    page.locator(".panel-sheet .bp-control.take").tap()
    page.wait_for_selector(".bp-drive .bv.driving", timeout=5000)
    live = [c for c in bs.clients if c.tier == "live" and not c.closed][-1]
    root = ".bp-drive"

    paste = page.locator(f"{root} .bp-drive-clip[data-clip='paste']")
    copy = page.locator(f"{root} .bp-drive-clip[data-clip='copy']")
    if not paste.count() or not copy.count():
        say("the row of keys has no Paste or Copy")
        context.close()
        return
    fits = page.evaluate("() => document.scrollingElement.scrollWidth")
    if fits > 390:
        say(f"the row of keys scrolls the page sideways: {fits}")

    set_clipboard(page, PASSWORD)
    start = len(bs.inputs(gid, live))
    paste.tap()
    page.wait_for_timeout(400)
    texts = [i.get("text") for i in bs.inputs(gid, live)[start:] if i["t"] == "text"]
    print(f"[{lang}] phone paste: {texts}")
    if texts != [PASSWORD]:
        say(f"Paste put {texts} on the page")

    group.selection = EMAIL
    copy.tap()
    page.wait_for_timeout(500)
    if clipboard(page) != EMAIL:
        say(f"Copy left {clipboard(page)!r} on the clipboard")

    # A clipboard the browser will not let the app read: the line below is where a paste still works.
    context.clear_permissions()
    start = len(bs.inputs(gid, live))
    paste.tap()
    page.wait_for_timeout(400)
    words = toast_text(page)
    print(f"[{lang}] refused paste: {words!r}")
    if WORDS[lang]["long_press"] not in words:
        say(f"a refused paste says {words!r}")
    if any(i["t"] == "text" for i in bs.inputs(gid, live)[start:]):
        say("a refused paste still sent text")
    context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            desktop(browser, scenes, lang, problems)
            phone(browser, scenes, lang, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
