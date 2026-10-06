"""Drive the Browser tab of the right panel in a real browser and refuse what does not behave.

At 1440 × 900, in English and Russian:

- the tab is offered to a session that has a browser, and not to one that has none;
- frames arrive and are drawn, each acknowledged once drawn;
- the agent's action moves the cursor to the element's centre, as the picture shows it, and frames
  the element with its name;
- while the agent drives, a click on the picture sends nothing and takes nothing;
- "Take control" names this window's live client; a click and typed text then go to the page as
  exactly the INPUT frames the contract describes (mouse down and up at the page's pixels, text as
  `text`, Enter as a key with its carriage return);
- "Give back" posts the note, and the page is the agent's again;
- a request for the operator turns the banner and the button amber, and a page dialog shows its chip;
- the log starts as a button under the picture; opening it lists the actions, and a row moves the cursor;
- on the page of its own the opened log is a column beside the picture;
- the picture asks the page to become the box it is drawn in;
- a socket closed by a restarting host (1012) comes back with a new ticket and draws again;
- the address and the history buttons are the operator's only while they drive.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_browser_panel.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, cursor_expected, open_page, render_scenes, wait_frames  # noqa: E402
from screenshots import S1, S2, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
ROOT = ".panel .bp"

WORDS = {
    "en": {"take": "Take control", "give": "Give back", "tab": "Browser", "forbidden": "password or payment field"},
    "ru": {"take": "Взять управление", "give": "Вернуть", "tab": "Браузер", "forbidden": "поле пароля или оплаты"},
}


def near(a: float, b: float, tol: float = 1.5) -> bool:
    return abs(a - b) <= tol


def check(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] {text}")  # noqa: E731
    bs = BrowserStub(scenes)
    group = bs.add("g1", scene="shop", owner_id=S1, acting=True)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")

    # A session with no browser has no Browser tab.
    page = open_page(context, bs, stub, f"{BASE}/agents/{S2}?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(".panel .panel-tab", timeout=10000)
    page.wait_for_timeout(500)
    if page.locator(".panel-tab[data-tab='browser']").count():
        say("a session without a browser offers the Browser tab")
    page.close()

    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}")
    tab = page.locator(".panel-tab[data-tab='browser']")
    tab.wait_for(timeout=10000)
    if tab.inner_text().strip() != WORDS[lang]["tab"]:
        say(f"the tab reads {tab.inner_text()!r}")
    tab.click()
    page.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=10000)
    if "panel=browser" not in page.url:
        say(f"the Browser tab is not in the address: {page.url}")
    frames = wait_frames(page, ROOT, 1)
    # By default the page keeps its own size (1280×800) and the picture is scaled into the pane: the
    # agent's page does not reflow when the panel is resized. Fitting it to the pane is a button.
    page.wait_for_timeout(1200)
    if bs.posted("/viewport"):
        say(f"the panel resized the page without being asked: {bs.posted('/viewport')}")
    fit = page.locator(f"{ROOT} .bp-fit")
    if fit.get_attribute("aria-pressed") != "false":
        say("the fit button is not off by default")
    fit.click()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not bs.posted("/viewport"):
        page.wait_for_timeout(100)
    views = bs.posted("/viewport")
    box = page.locator(f"{ROOT} .bv").bounding_box()
    print(f"[{lang}] fitted viewport: {views[-1] if views else None} box: {box}")
    if not views or box is None:
        say("the fit button did not ask the page to fill the picture")
    elif abs(views[-1]["w"] - box["width"]) > 24 or abs(views[-1]["h"] - box["height"]) > 24:
        say(f"the page was asked for {views[-1]}, not the picture {box['width']:.0f}×{box['height']:.0f}")
    fitted = len(views)
    fit.click()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(bs.posted("/viewport")) == fitted:
        page.wait_for_timeout(100)
    back = bs.posted("/viewport")[fitted:]
    if back[-1:] != [{"w": 1280, "h": 800}]:
        say(f"turning fitting off did not give the page back its own size: {back}")
    if fit.get_attribute("aria-pressed") != "false":
        say("the fit button did not turn off")
    # The choice is the device's: another window of it keeps fitting off and sends nothing, the page
    # having been given its size back already.
    page.wait_for_timeout(500)
    sent = len(bs.posted("/viewport"))
    other = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}&panel=browser")
    other.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=10000)
    other.wait_for_timeout(1200)
    if other.locator(f"{ROOT} .bp-fit").get_attribute("aria-pressed") != "false":
        say("another window of this device did not keep fitting off")
    if len(bs.posted("/viewport")) != sent:
        say(f"another window with fitting off resized the page: {bs.posted('/viewport')[sent:]}")
    other.close()
    live = next(c for c in bs.clients if c.tier == "live" and not c.closed)
    page.wait_for_timeout(300)
    print(f"[{lang}] frames drawn: {frames}; acks: {live.of('ack')}; attach: {live.of('attach')}")
    if 1 not in live.of("ack"):
        say("the first frame was never acknowledged")
    attach = live.of("attach")[0]
    if attach.get("tier") != "live" or not (64 <= attach.get("max_w", 0) <= 1600):
        say(f"the live view attached with {attach}")
    painted = page.evaluate(f"""() => {{
        const c = document.querySelector('{ROOT} .bv-canvas');
        const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
        const seen = new Set();
        for (let i = 0; i < d.length; i += 4 * 997) seen.add(d[i] + ',' + d[i + 1] + ',' + d[i + 2]);
        return {{ w: c.width, h: c.height, colours: seen.size }};
    }}""")
    print(f"[{lang}] canvas: {painted}")
    if painted["w"] != 1280 or painted["colours"] < 20:
        say(f"the picture is not the page's: {painted}")

    # The agent clicks "Add to cart": the cursor glides to its centre and the element is framed.
    before = len(bs.inputs("g1"))
    event = bs.act("g1", "click", "add", name="Add to cart", element="the Add to cart button")
    page.wait_for_selector(f"{ROOT} .bv-overlay[data-action='{event['id']}']", timeout=5000)
    page.wait_for_timeout(350)
    want = cursor_expected(page, ROOT, event["point"])
    cur = page.locator(f"{ROOT} .bv-cursor")
    got = (float(cur.get_attribute("data-x")), float(cur.get_attribute("data-y")))
    glide = page.evaluate(f"() => getComputedStyle(document.querySelector('{ROOT} .bv-cursor')).transitionDuration")
    print(f"[{lang}] cursor at {got}, expected {want}; glide {glide}")
    if not (near(got[0], want[0]) and near(got[1], want[1])):
        say(f"the cursor is at {got}, not at the button's centre {want}")
    if glide not in ("0.18s", "180ms"):
        say(f"the cursor does not glide: transition {glide}")
    label = page.locator(f"{ROOT} .bv-box .bv-box-label")
    if not label.count() or label.inner_text().strip() != "Add to cart":
        say("the element is not framed with its name")
    box = [float(v) for v in page.locator(f"{ROOT} .bv-box").get_attribute("data-box").split(",")]
    corner = cursor_expected(page, ROOT, {"x": event["box"]["x"], "y": event["box"]["y"]})
    if not (near(box[0], corner[0], 2) and near(box[1], corner[1], 2)):
        say(f"the frame is at {box[:2]}, not {corner}")

    # While the agent drives, the picture is only a picture.
    viewer = page.locator(f"{ROOT} .bv").bounding_box()
    page.mouse.click(viewer["x"] + 200, viewer["y"] + 120)
    page.wait_for_timeout(300)
    if len(bs.inputs("g1")) != before or bs.posted("/control"):
        say("a click on the picture while the agent drives sent input or took control")
    if page.locator(f"{ROOT} .bp-address input").get_attribute("readonly") is None:
        say("the address is editable while the agent drives")
    if not page.locator(f"{ROOT} .bp-toolbar button[aria-label]").first.is_disabled():
        say("the back button works while the agent drives")

    # Take control: this window's live client is named, and the page is ours.
    take = page.locator(f"{ROOT} .bp-control.take")
    if take.inner_text().strip() != WORDS[lang]["take"]:
        say(f"the take button reads {take.inner_text()!r}")
    take.click()
    page.wait_for_selector(f"{ROOT} .bv.driving", timeout=5000)
    posted = bs.posted("/control")
    print(f"[{lang}] take posted: {posted}")
    if posted[:1] != [{"owner": "human", "client_id": live.id}]:
        say(f"taking control posted {posted}, not this window's client {live.id}")
    if page.locator(f"{ROOT} .bp-banner").get_attribute("data-banner") != "you":
        say("the banner does not say the operator drives")

    # A click on "Search" and typed text reach the page as the contract's frames.
    search = bs.scenes["shop"].boxes["search"]
    target = {"x": search["x"] + search["w"] / 2, "y": search["y"] + search["h"] / 2}
    cx, cy = cursor_expected(page, ROOT, target)
    viewer = page.locator(f"{ROOT} .bv").bounding_box()
    start = len(bs.inputs("g1", live))
    page.mouse.click(viewer["x"] + cx, viewer["y"] + cy)
    page.wait_for_timeout(200)
    page.keyboard.type("rye")
    page.keyboard.press("Enter")
    page.wait_for_timeout(400)
    sent = bs.inputs("g1", live)[start:]
    print(f"[{lang}] input: {sent}")
    mouse = [i for i in sent if i["t"] == "mouse" and i["type"] in ("down", "up")]
    if [m["type"] for m in mouse] != ["down", "up"]:
        say(f"the click went as {mouse}")
    elif not all(near(m["x"], target["x"], 2) and near(m["y"], target["y"], 2) and m["button"] == "left" and m["clicks"] == 1 for m in mouse):
        say(f"the click landed at {[(m['x'], m['y']) for m in mouse]}, not the field's centre {target}")
    typed = "".join(i["text"] for i in sent if i["t"] == "text")
    if typed != "rye":
        say(f"the typed text reached the page as {typed!r}")
    enter = [i for i in sent if i["t"] == "key" and i["key"] == "Enter"]
    if [(k["type"], k.get("text")) for k in enter] != [("down", "\r"), ("up", None)]:
        say(f"Enter went as {enter}")
    if page.locator(f"{ROOT} .bp-address input").get_attribute("readonly") is not None:
        say("the address is read-only while the operator drives")

    # The address bar navigates while the operator drives.
    field = page.locator(f"{ROOT} .bp-address input")
    field.click()
    field.fill("https://shop.example.com/cart")
    field.press("Enter")
    page.wait_for_timeout(500)
    nav = [i for i in bs.inputs("g1", live) if i["t"] == "nav"]
    if nav[-1:] != [{"t": "nav", "action": "url", "url": "https://shop.example.com/cart"}]:
        say(f"the address bar sent {nav}")
    bs.navigate("g1", "shop")

    # Give back, with a note.
    page.locator(f"{ROOT} .bp-control.give").click()
    page.locator(".bp-give textarea").fill("Signed in; the cart is ready.")
    page.locator(".bp-give button[type='submit']").click()
    page.wait_for_selector(f"{ROOT} .bv:not(.driving)", timeout=5000)
    given = bs.posted("/control")[-1]
    print(f"[{lang}] give back posted: {given}")
    if given != {"owner": "agent", "note": "Signed in; the cart is ready."}:
        say(f"giving back posted {given}")
    if page.locator(f"{ROOT} .bp-banner").get_attribute("data-banner") not in ("acting", "idle"):
        say("the banner still says the operator drives")

    # The agent asks for the operator: amber banner and button, the reason in words.
    bs.needs_you("g1", "login", "Sign in to accounts.example.com")
    page.wait_for_selector(f"{ROOT} .bp-banner[data-banner='needs']", timeout=5000)
    if "accounts.example.com" not in page.locator(f"{ROOT} .bp-banner").inner_text():
        say("the banner does not say what the agent needs")
    if not page.locator(f"{ROOT} .bp-control.take.needs").count():
        say("the take button is not amber while the agent needs the operator")

    # One the daemon raised itself, with no sentence: the reason is put in words.
    bs.needs_you("g1", "field_forbidden", "", by="daemon")
    page.wait_for_function(f"() => document.querySelector('{ROOT} .bp-banner').textContent.includes({WORDS[lang]['forbidden']!r})", timeout=5000)

    # A page dialog shows its chip; it can be answered only by someone driving.
    bs.open_dialog("g1", "Leave site? Changes you made may not be saved.")
    page.wait_for_selector(f"{ROOT} .bp-dialog", timeout=5000)
    if page.locator(f"{ROOT} .bp-dialog button").count():
        say("the dialog can be answered without taking control")

    # The log: open it, and a row points the cursor at its element again.
    bs.set_control("g1", "agent")
    group.needs = None
    page.locator(f"{ROOT} .bp-log-head").click()
    page.wait_for_selector(f"{ROOT} .bp-log.open .bp-log-row", timeout=5000)
    rows = page.locator(f"{ROOT} .bp-log-row").all_inner_texts()
    print(f"[{lang}] log: {rows}")
    if not any("Add to cart" in r for r in rows):
        say(f"the log does not list the click: {rows}")
    size_event = bs.act("g1", "click", "size", name="5 kg", element="the 5 kg size option")
    page.wait_for_selector(f"{ROOT} .bv-overlay[data-action='{size_event['id']}']", timeout=5000)
    page.locator(f"{ROOT} .bp-log-row[data-action='{event['id']}']").click()
    page.wait_for_selector(f"{ROOT} .bv-overlay[data-action='{event['id']}']", timeout=5000)
    page.wait_for_timeout(300)
    want = cursor_expected(page, ROOT, event["point"])
    got = (float(cur.get_attribute("data-x")), float(cur.get_attribute("data-y")))
    if not (near(got[0], want[0]) and near(got[1], want[1])):
        say(f"the log row put the cursor at {got}, not {want}")

    # The host restarts: the socket closes with 1012, a new ticket is asked for, frames draw again.
    tickets = len(bs.tickets)
    drawn = int(page.locator(f"{ROOT} .bv-canvas").get_attribute("data-frames") or 0)
    bs.drop("g1", 1012)
    page.wait_for_function(f"() => Number(document.querySelector('{ROOT} .bv-canvas').dataset.frames || 0) > {drawn}", timeout=10000)
    newest = [c for c in bs.clients if c.tier == "live" and not c.closed]
    print(f"[{lang}] after 1012: tickets {tickets} -> {len(bs.tickets)}, live clients {[c.id for c in newest]}")
    if len(bs.tickets) <= tickets or not newest or not newest[-1].of("attach"):
        say("the view did not come back after the host restarted")

    # Nothing leaks: the overlay stays a handful of nodes after many actions.
    for _ in range(12):
        bs.act("g1", "click", "size", name="5 kg", element="the 5 kg size option")
    page.wait_for_timeout(600)
    nodes = page.evaluate(f"() => document.querySelectorAll('{ROOT} .bv-overlay *').length")
    if nodes > 12:
        say(f"the overlay grew to {nodes} nodes")
    page.close()

    # The page of its own, for a second monitor. The log starts as the button under the picture;
    # opening it puts the list in a column beside the picture.
    page = open_page(context, bs, stub, f"{BASE}/browser/g1?token=t&scheme=dark&lang={lang}", wait=".browser-full .bp .bv[data-state='live']")
    wait_frames(page, ".browser-full .bp", 1)
    closed = page.evaluate("""() => {
      const pic = document.querySelector('.browser-full .bp-stage-wrap').getBoundingClientRect();
      const log = document.querySelector('.browser-full .bp-log').getBoundingClientRect();
      const row = document.querySelector('.browser-full .bp-log-row');
      return { beside: log.left >= pic.right - 1, below: log.top >= pic.bottom - 2, shown: !!(row && row.getClientRects().length) };
    }""")
    print(f"[{lang}] own window closed: {closed}")
    if closed["beside"] or not closed["below"] or closed["shown"]:
        say(f"the log is open before it is asked for: {closed}")
    page.locator(".browser-full .bp-log-head").click()
    page.wait_for_selector(".browser-full .bp-log.open .bp-log-row", timeout=5000)
    side = page.evaluate("""() => {
      const pic = document.querySelector('.browser-full .bp-stage-wrap').getBoundingClientRect();
      const log = document.querySelector('.browser-full .bp-log').getBoundingClientRect();
      return { beside: log.left >= pic.right - 1, log_w: Math.round(log.width), rows: document.querySelectorAll('.browser-full .bp-log-row').length };
    }""")
    print(f"[{lang}] own window: {side}")
    if not side["beside"] or side["log_w"] != 280 or side["rows"] == 0:
        say(f"the page of its own does not put the opened log beside the picture: {side}")
    page.close()
    context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            check(browser, scenes, lang, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
