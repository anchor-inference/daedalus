"""Drive the browser's corner preview and its phone form in a real browser, and refuse what does not behave.

Desktop, English and Russian:

- the card sits inside the conversation's column, 8 px under its top and 12 px from its right edge,
  never over the panel, at 1440 and at 1280, with the panel open and closed;
- it is not there while the panel covers the chat, nor while the Browser tab shows the page;
- in a narrow column it is a pill; in a dual view it belongs to the pane whose session has a browser;
- it draws the thumbnail tier, and the agent's cursor on it;
- a click opens the Browser tab (in the address), live;
- "needs you" turns it amber with the reason; ✕ hides it until something new happens;
- dragged, it snaps to the nearest corner and the device remembers it.

Phone (390 × 844, touch):

- the header's chip listens to the browser on a small read-only stream, and turns amber with "Needs
  you" when the agent needs the operator;
- the first time a browser opens a line says so and offers to view it;
- the button opens the sheet on the Browser tab; the page never scrolls sideways;
- taking control fills the screen; a tap is a click at the page's pixels, a finger dragged up scrolls
  the page down, a long press is a right click, the key row sends keys, the compose line sends text,
  and a phone keyboard's doubled word is filtered;
- giving back returns the sheet.

    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_browser_pip.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, cursor_expected, open_page, render_scenes, wait_frames  # noqa: E402
from screenshots import S1, S2, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

RECTS = """(pane) => {
  const root = pane ? document.querySelector(pane) : document;
  const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); return { x: b.x, y: b.y, w: b.width, h: b.height, r: b.right, b: b.bottom }; };
  const pip = root.querySelector('.bp-pip');
  return { pip: r(pip), column: r(pip ? pip.parentElement : root.querySelector('.chat-main')), panel: r(root.querySelector('.panel.shown, .panel')), pill: !!(pip && pip.classList.contains('pill')), corner: pip ? pip.dataset.corner : null };
}"""


def overlaps(a: dict, b: dict) -> bool:
    return a["x"] < b["r"] and b["x"] < a["r"] and a["y"] < b["b"] and b["y"] < a["b"]


def placement(page, say, where: str, pane: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    m = page.evaluate(RECTS, pane)
    if not m["pip"]:
        say(f"{where}: no preview")
        return m
    p, c = m["pip"], m["column"]
    inside = p["x"] >= c["x"] - 0.5 and p["r"] <= c["r"] + 0.5 and p["y"] >= c["y"] - 0.5 and p["b"] <= c["b"] + 0.5
    if not inside:
        say(f"{where}: the preview {p} is not inside the conversation's column {c}")
    if m["panel"] and m["panel"]["w"] > 0 and overlaps(p, m["panel"]):
        say(f"{where}: the preview {p} overlaps the panel {m['panel']}")
    if m["corner"] == "tr" and not m["pill"]:
        if abs(p["y"] - c["y"] - 8) > 1 or abs(c["r"] - p["r"] - 12) > 1:
            say(f"{where}: the preview is not 8 px under the column's top and 12 px from its right: {p} in {c}")
        if abs(p["w"] - 240) > 1:
            say(f"{where}: the card is {p['w']} px wide, not 240")
    return m


def desktop(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] {text}")  # noqa: E731
    bs = BrowserStub(scenes)
    bs.add("g1", scene="shop", owner_id=S1, acting=True)

    for width in (1440, 1280):
        context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme="dark")
        page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}", wait=".bp-pip")
        page.wait_for_timeout(400)
        m = placement(page, say, f"{width}, panel open")
        if not m["panel"] or m["panel"]["w"] <= 0:
            say(f"{width}: the panel is not open beside the conversation")
        print(f"[{lang}] {width} panel open: pip {m['pip']} column {m['column']} pill {m['pill']}")
        # Closed panel: still in the column, now the whole width.
        page.locator(".chat-head button[aria-pressed='true'] .ic-panel").first.click()
        page.wait_for_timeout(500)
        placement(page, say, f"{width}, panel closed")
        context.close()

    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    context.add_init_script("try { localStorage.removeItem('daedalus.browser.pip.corner'); } catch (e) {}")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}", wait=".bp-pip")
    wait_frames(page, ".bp-pip", 1)
    thumb = next(c for c in bs.clients if c.tier == "thumb" and not c.closed)
    if thumb.of("attach")[0].get("tier") != "thumb" or not thumb.read_only:
        say(f"the preview attached as {thumb.of('attach')[0]} (read-only {thumb.read_only})")
    event = bs.act("g1", "click", "size", name="5 kg", element="the 5 kg size option")
    page.wait_for_selector(f".bp-pip .bv-overlay[data-action='{event['id']}']", timeout=5000)
    page.wait_for_timeout(300)
    cur = page.locator(".bp-pip .bv-cursor")
    want = cursor_expected(page, ".bp-pip", event["point"])
    got = (float(cur.get_attribute("data-x")), float(cur.get_attribute("data-y")))
    print(f"[{lang}] preview cursor {got}, expected {want}")
    if abs(got[0] - want[0]) > 1 or abs(got[1] - want[1]) > 1:
        say(f"the preview's cursor is at {got}, not {want}")

    # Expanded panel: no preview over it.
    page.locator(".panel-actions button").first.click()
    page.wait_for_timeout(400)
    if page.locator(".bp-pip").count():
        say("the preview stays while the panel covers the chat")
    page.locator(".panel-actions button").first.click()
    page.wait_for_selector(".bp-pip", timeout=5000)

    # "Needs you": amber, with the reason.
    bs.needs_you("g1", "login", "Sign in to accounts.example.com")
    page.wait_for_selector(".bp-pip.needs .bp-pip-need", timeout=5000)
    if "accounts.example.com" not in page.locator(".bp-pip-need").inner_text():
        say("the amber preview does not say what is needed")
    bs.set_control("g1", "agent")
    bs.groups["g1"].needs = None

    # Drag to the bottom left: it snaps there, and the device remembers.
    card = page.locator(".bp-pip").bounding_box()
    column = page.evaluate(RECTS, None)["column"]
    page.mouse.move(card["x"] + 60, card["y"] + 150)
    page.mouse.down()
    page.mouse.move(column["x"] + 150, column["b"] - 60, steps=8)
    page.mouse.up()
    page.wait_for_timeout(400)
    corner = page.locator(".bp-pip").get_attribute("data-corner")
    stored = page.evaluate("() => localStorage.getItem('daedalus.browser.pip.corner')")
    after = placement(page, say, "after the drag")
    print(f"[{lang}] dragged to {corner}, stored {stored}, at {after['pip']}")
    if corner != "bl" or stored != "bl":
        say(f"the drag left it in {corner} (stored {stored}), not the bottom left")
    if page.locator(".panel-tab[data-tab='browser'].on").count():
        say("the drag opened the Browser tab as if it were a click")
    page.evaluate("() => localStorage.setItem('daedalus.browser.pip.corner', 'tr')")
    page.reload()
    page.wait_for_selector(".bp-pip[data-corner='tr']", timeout=10000)

    # ✕ hides it until the group does something new.
    page.locator(".bp-pip").hover()
    page.locator(".bp-pip-actions button").nth(2).click()
    page.wait_for_timeout(300)
    if page.locator(".bp-pip").count():
        say("✕ did not hide the preview")
    time_before = bs.groups["g1"].last_activity
    bs.act("g1", "click", "add", name="Add to cart", element="the Add to cart button")
    bs.groups["g1"].last_activity = max(bs.groups["g1"].last_activity, time_before + 1)
    page.wait_for_selector(".bp-pip", timeout=15000)

    # A click opens the Browser tab, live, and the preview goes.
    page.locator(".bp-pip .bp-pip-foot").click()
    page.wait_for_selector(".panel-tab[data-tab='browser'].on", timeout=5000)
    page.wait_for_selector(".panel .bp .bv[data-state='live']", timeout=10000)
    if "panel=browser" not in page.url:
        say(f"the click did not put the Browser tab in the address: {page.url}")
    if page.locator(".bp-pip").count():
        say("the preview stays while the Browser tab shows the page")
    context.close()

    # A narrow column (1024 with the sidebar and the panel open): a pill.
    context = browser.new_context(viewport={"width": 1024, "height": 800}, color_scheme="dark")
    context.add_init_script("try { localStorage.setItem('daedalus.sidebar', 'open'); } catch (e) {}")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?panel=details&token=t&scheme=dark&lang={lang}", wait=".bp-pip")
    m = placement(page, say, "1024 with the panel")
    print(f"[{lang}] 1024: pill {m['pill']} column {m['column']}")
    if not m["pill"]:
        say(f"the preview is a card in a {m['column']['w']} px column")
    context.close()

    # A touch tablet in landscape with the panel open: a column of about 650 px, which a mouse keeps
    # as a card, is a pill under a finger, which cannot hover to reach the card's buttons.
    context = browser.new_context(viewport={"width": 1180, "height": 820}, has_touch=True, color_scheme="dark")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?panel=details&token=t&scheme=dark&lang={lang}", wait=".bp-pip")
    m = placement(page, say, "touch tablet with the panel")
    print(f"[{lang}] touch 1180: pill {m['pill']} column {m['column']}")
    if not m["pill"]:
        say(f"on a touch tablet the preview is a card in a {m['column']['w']} px column")
    context.close()

    # A dual view: the preview belongs to the pane whose session has the browser.
    context = browser.new_context(viewport={"width": 1920, "height": 1000}, color_scheme="dark")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S2}?with={S1}&token=t&scheme=dark&lang={lang}", wait=".bp-pip")
    panes = page.evaluate("() => [...document.querySelectorAll('.chat.pane')].map((p) => ({ cls: p.className, pip: !!p.querySelector('.bp-pip') }))")
    print(f"[{lang}] dual view: {panes}")
    if [p["pip"] for p in panes] != [False, True]:
        say(f"in a dual view the preview is in {panes}")
    placement(page, say, "dual view, right pane", ".chat.pane-right")
    context.close()


def phone(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] phone: {text}")  # noqa: E731
    bs = BrowserStub(scenes)
    bs.add(f"g-{lang}", scene="shop", owner_id=S1, acting=True)
    gid = f"g-{lang}"
    context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="dark")
    context.add_init_script("try { localStorage.removeItem('daedalus.browser.announced'); } catch (e) {}")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}", wait=".browser-headbtn")
    if page.locator(".bp-pip").count():
        say("a phone shows the floating preview")
    # The chip draws no picture any more; what it needs is its stream, which says a request has arrived
    # before the listing is read again.
    for _ in range(100):
        if any(c.tier == "thumb" and c.attached and not c.closed for c in bs.clients):
            break
        page.wait_for_timeout(100)
    else:
        say("the header's chip never opened its stream")
    toast = page.locator(".toast")
    toast.wait_for(timeout=5000)
    print(f"[{lang}] phone toast: {toast.inner_text()!r}")
    if not toast.locator(".toast-action").count():
        say("the first browser has no line offering to view it")
    bs.needs_you(gid, "login", "Sign in to accounts.example.com")
    page.wait_for_selector(".browser-headbtn.needs .browser-dot.needs", timeout=5000)
    chip = page.locator(".browser-headbtn").inner_text()
    print(f"[{lang}] phone chip: {chip!r}")
    if not chip.strip():
        say("the header's chip does not say that the agent needs the operator")
    bs.set_control(gid, "agent")
    bs.groups[gid].needs = None

    page.locator(".browser-headbtn").tap()
    page.wait_for_selector(".panel-sheet .panel-tab[data-tab='browser'].on", timeout=5000)
    page.wait_for_selector(".panel-sheet .bp .bv[data-state='live']", timeout=10000)
    wait_frames(page, ".panel-sheet .bp", 1)
    if page.locator(".toast").count():
        say("the offer to view stays once the browser is open")
    wide = page.evaluate("() => document.scrollingElement.scrollWidth")
    if wide > 390:
        say(f"the page scrolls sideways: {wide}")

    page.locator(".panel-sheet .bp-control.take").tap()
    page.wait_for_selector(".bp-drive .bv.driving", timeout=5000)
    live = [c for c in bs.clients if c.tier == "live" and not c.closed][-1]
    if bs.posted("/control")[-1:] != [{"owner": "human", "client_id": live.id}]:
        say(f"taking control posted {bs.posted('/control')}")
    page.wait_for_timeout(300)
    root = ".bp-drive"
    viewer = page.locator(f"{root} .bv").bounding_box()
    add = bs.scenes["shop"].boxes["add"]
    target = {"x": add["x"] + add["w"] / 2, "y": add["y"] + add["h"] / 2}
    cx, cy = cursor_expected(page, root, target)
    start = len(bs.inputs(gid, live))
    page.touchscreen.tap(viewer["x"] + cx, viewer["y"] + cy)
    page.wait_for_timeout(300)
    sent = bs.inputs(gid, live)[start:]
    clicks = [i for i in sent if i["t"] == "mouse" and i["type"] in ("down", "up")]
    print(f"[{lang}] tap: {clicks}")
    if [c["type"] for c in clicks] != ["down", "up"] or not all(abs(c["x"] - target["x"]) < 3 and abs(c["y"] - target["y"]) < 3 and c["button"] == "left" for c in clicks):
        say(f"a tap on Add to cart went as {sent}, not a click at {target}")

    # One finger dragged up: the page scrolls down.
    cdp = context.new_cdp_session(page)
    x0, y0 = viewer["x"] + viewer["width"] / 2, viewer["y"] + viewer["height"] / 2 + 60
    start = len(bs.inputs(gid, live))
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x0, "y": y0}]})
    for step in range(1, 8):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x0, "y": y0 - step * 12}]})
        page.wait_for_timeout(20)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(300)
    sent = bs.inputs(gid, live)[start:]
    wheels = [i for i in sent if i["t"] == "wheel"]
    print(f"[{lang}] drag: {sent}")
    if not wheels or sum(w["dy"] for w in wheels) <= 0 or any(i["t"] == "mouse" and i["type"] == "down" for i in sent):
        say(f"a finger dragged up went as {sent}, not a scroll down")

    # A long press is a right click.
    start = len(bs.inputs(gid, live))
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x0, "y": y0}]})
    page.wait_for_timeout(700)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(200)
    sent = bs.inputs(gid, live)[start:]
    if [i.get("button") for i in sent if i["t"] == "mouse" and i["type"] == "down"] != ["right"]:
        say(f"a long press went as {sent}")

    # The key row, the compose line, and a keyboard's doubled word.
    start = len(bs.inputs(gid, live))
    page.locator(f"{root} .term-key[data-key='Backspace']").tap()
    page.locator(f"{root} .term-compose-field").fill("ржаная мука")
    page.locator(f"{root} .term-compose-send").tap()
    page.wait_for_timeout(300)
    page.evaluate("""() => {
      const f = document.querySelector('.bp-drive .bv-ime');
      f.focus();
      for (const chunk of ['l', 's', 'ls']) { f.value += chunk; f.dispatchEvent(new InputEvent('input', { bubbles: true, data: chunk, inputType: 'insertText' })); }
    }""")
    page.wait_for_timeout(300)
    sent = bs.inputs(gid, live)[start:]
    print(f"[{lang}] keys and text: {sent}")
    keys = [(i["type"], i["key"]) for i in sent if i["t"] == "key"]
    texts = [i["text"] for i in sent if i["t"] == "text"]
    if keys != [("down", "Backspace"), ("up", "Backspace")]:
        say(f"the key row sent {keys}")
    if texts != ["ржаная мука", "l", "s"]:
        say(f"the text reached the page as {texts} (the compose line, then l, s without the replayed 'ls')")
    wide = page.evaluate("() => document.scrollingElement.scrollWidth")
    if wide > 390:
        say(f"driving scrolls sideways: {wide}")

    # Give back.
    page.locator(f"{root} .bp-drive-give").tap()
    page.locator(".bp-give button[type='submit']").tap()
    page.wait_for_selector(".bp-drive", state="detached", timeout=5000)
    if bs.posted("/control")[-1] != {"owner": "agent"}:
        say(f"giving back posted {bs.posted('/control')[-1]}")
    context.close()


def staff(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    """A command-line staff member's browser: the card beside its terminal, and a Browser tab in its aside."""
    import json as _json
    from urllib.parse import urlsplit

    from check_staff_view import stand
    from event_feed import EventFeed

    say = lambda text: problems.append(f"[{lang}] staff: {text}")  # noqa: E731
    focus, term, pid = stand(lang)
    bs = BrowserStub(scenes)
    bs.add("gs", scene="shop", owner_kind="staff", owner_id="st-ira", owner_label="Ira", project_id=pid, acting=True)
    feed = EventFeed()

    def page_at(context, url: str):  # type: ignore[no-untyped-def]
        page = context.new_page()

        def handle(route) -> None:  # type: ignore[no-untyped-def]
            request = route.request
            parts = urlsplit(request.url)
            path = parts.path[parts.path.index("/api/"):]
            body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
            answered = focus.answer(request.method, path, parts.query, body)
            if answered is not None:
                return route.fulfill(status=answered[0], content_type="application/json", body=_json.dumps(answered[1]))
            return stub(route)

        page.route("**/api/**", handle)
        term.install(page)
        bs.install(page)
        page.route("**/api/events**", feed.route)
        page.goto(url)
        return page

    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = page_at(context, f"{BASE}/project/{pid}/staff/st-ira?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".staff-cli .bp-pip", timeout=20000)
    page.wait_for_timeout(400)
    placement(page, say, "staff view", ".staff-cli")
    tag = page.locator(".bp-pip").get_attribute("aria-label") or ""
    if "Ira" not in tag:
        say(f"the card does not name the member: {tag!r}")
    page.locator(".bp-pip .bp-pip-foot").click()
    page.wait_for_selector(".staff-aside .panel-tab[data-tab='browser'].on", timeout=5000)
    page.wait_for_selector(".staff-aside .bp .bv[data-state='live']", timeout=10000)
    if page.locator(".bp-pip").count():
        say("the card stays while the aside shows the browser")
    context.close()

    context = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True, color_scheme="dark")
    page = page_at(context, f"{BASE}/project/{pid}/staff/st-ira?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".staff-cli .browser-headbtn", timeout=20000)
    page.locator(".browser-headbtn").tap()
    page.wait_for_selector(".staff-sheet .bp .bv[data-state='live']", timeout=10000)
    context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            desktop(browser, scenes, lang, problems)
            phone(browser, scenes, lang, problems)
            staff(browser, scenes, lang, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
