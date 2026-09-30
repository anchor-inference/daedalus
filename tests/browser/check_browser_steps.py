"""Record the operator's own steps in the Browser tab, draft a skill from them and approve it, in a real
browser. At 1440 × 900, in English and Russian:

- there is no way to record while the agent drives; taking control offers "Record" in the toolbar;
- "Record" opens its explanation (secrets are never recorded) and the one choice, whether typed values
  are kept; starting posts that choice, and a bar over the picture says the recording is on;
- each step the daemon records shows in the bar as it comes, counted, the latest in words;
- "Mark the result" posts the mark without taking the page's selection away; "Stop" ends it;
- the finished recording offers its steps (a sign-in reads as the operator's, not recorded), a goal and
  "Make a skill draft"; the draft opens for editing, and approving saves the operator's words first;
- a recording the give-back ends is kept, and discarding one asks first;
- Settings → Browser lists a procedure with its title, a few lines of it, and edits it in place;
  on a phone it does not scroll sideways.

Exit 0 when every step holds.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402
from browser_stub import BrowserStub, open_page, render_scenes, wait_frames  # noqa: E402
from check_settings_browser import Host  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("STEPS_SHOTS", "")
ROOT = ".panel .bp"
WORDS = {
    "en": {"secret": "never recorded", "steps": "3 steps", "done": "Recorded 3 steps", "yours": "not recorded", "procedure": "procedure", "save": "Save", "later": "Later"},
    "ru": {"secret": "не записываются никогда", "steps": "3 шага", "done": "Записано 3 шага", "yours": "не записано", "procedure": "процедура", "save": "Сохранить", "later": "Позже"},
}


def shot(page, name: str) -> None:  # type: ignore[no-untyped-def]
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / f"{name}.png"))


def calls(bs: BrowserStub, *parts: str) -> list[tuple[str, str]]:
    return [(m, p) for m, p, _ in bs.requests if all(x in p for x in parts)]


def panel(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] {text}")  # noqa: E731
    words = WORDS[lang]
    bs = BrowserStub(scenes)
    group = bs.add("g1", scene="shop", owner_id=S1)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
    page = open_page(context, bs, stub, f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}&panel=browser")
    page.wait_for_selector(f"{ROOT} .bv[data-state='live']", timeout=10000)
    wait_frames(page, ROOT, 1)

    # While the agent drives there is nothing to record.
    if page.locator(f"{ROOT} .bp-rec-btn").count():
        say("Record is offered while the agent drives")
    page.locator(f"{ROOT} .bp-control.take").click()
    page.wait_for_selector(f"{ROOT} .bp-rec-btn", timeout=5000)

    # Record: what it does, what it never keeps, and the one choice.
    page.locator(f"{ROOT} .bp-rec-btn").click()
    pop = page.locator(".bp-rec-start")
    pop.wait_for(timeout=5000)
    text = pop.inner_text()
    if words["secret"] not in text:
        say(f"the start does not say secrets are never recorded: {text!r}")
    page.wait_for_timeout(400)
    shot(page, f"steps-start-{lang}")
    pop.locator("input[type=checkbox]").check()
    pop.locator(".btn.primary").click()
    page.wait_for_selector(f"{ROOT} .bp-recbar", timeout=5000)
    started = bs.posted("/g1/workflow")
    print(f"[{lang}] started with {started}")
    if started[-1:] != [{"values": "literal"}]:
        say(f"the recording started with {started}")
    if page.locator(f"{ROOT} .bp-rec-btn").count():
        say("Record is still offered while recording")

    # Each step shows as it is taken.
    bs.record_step("g1", action="type", element={"role": "searchbox", "name": "Search"}, slot="search", value="blue shoes", submit=True)
    bs.record_step("g1", action="handoff", reason="login", element={"role": "textbox", "name": "Email"})
    bs.record_step("g1", action="click", element={"role": "button", "name": "Add to cart"}, asks=["purchase"])
    bar = page.locator(f"{ROOT} .bp-recbar")
    page.wait_for_function(f"() => document.querySelector('{ROOT} .bp-recbar-count')?.textContent === {words['steps']!r}", timeout=5000)
    latest = bar.locator(".bp-recbar-latest").inner_text()
    print(f"[{lang}] bar: {bar.inner_text()!r}")
    if "Add to cart" not in latest:
        say(f"the bar's latest step reads {latest!r}")
    box = bar.bounding_box()
    stage = page.locator(f"{ROOT} .bv").bounding_box()
    if not box or not stage or box["y"] + box["height"] > stage["y"] + 1:
        say(f"the bar is not over the picture: {box} {stage}")
    shot(page, f"steps-recording-{lang}")

    # The result, marked from the page's selection; then Stop.
    group.selection = "Order placed"
    bar.locator(".bp-recbar-mark").click()
    page.wait_for_timeout(300)
    if not bs.posted("/g1/workflow/mark"):
        say("Mark the result posted nothing")
    bar.locator(".bp-recbar-stop").click()
    card = page.locator(f"{ROOT} .bp-rec[data-state='stopped']")
    card.wait_for(timeout=5000)
    if page.locator(f"{ROOT} .bp-recbar").count():
        say("the bar stayed after Stop")
    head = card.locator(".bp-rec-head b").inner_text()
    if head != words["done"]:
        say(f"the card reads {head!r}")
    card.locator(".bp-rec-toggle").click()
    items = card.locator(".bp-rec-steps li")
    yours = card.locator(".bp-rec-steps li.yours")
    print(f"[{lang}] steps: {items.all_inner_texts()}")
    if items.count() != 4 or yours.count() != 1 or words["yours"] not in yours.inner_text():
        say(f"the steps read {items.all_inner_texts()}")
    if "hunter" in card.inner_text():
        say("a secret reached the card")

    # A draft from them, edited and approved.
    card.locator(".bp-rec-goal").fill("Order blue shoes")
    shot(page, f"steps-stopped-{lang}")
    card.locator(".btn.primary").click()
    editor = page.locator(f"{ROOT} .bp-rec[data-state='draft'] .bp-proc")
    editor.wait_for(timeout=5000)
    drafted = [b for m, p, b in bs.requests if m == "POST" and p.endswith("/draft")]
    if drafted[-1:] != [{"goal": "Order blue shoes"}]:
        say(f"the draft was asked with {drafted}")
    if editor.locator(".bp-proc-title").input_value() != "Order blue shoes" or "Add to cart" not in editor.locator(".bp-proc-text").input_value():
        say("the draft editor does not hold the draft")
    shot(page, f"steps-draft-{lang}")
    editor.locator(".bp-proc-text").fill(editor.locator(".bp-proc-text").input_value() + "\nDone when the cart says Order placed.")
    editor.locator(".btn.primary").click()
    page.wait_for_selector(f"{ROOT} .bp-rec", state="detached", timeout=5000)
    answered = [(m, p) for m, p, _ in bs.requests if "/notes/" in p]
    edited = [b for m, p, b in bs.requests if m == "PATCH"]
    print(f"[{lang}] notes: {answered}")
    if answered != [("PATCH", "/api/browsers/notes/n1"), ("POST", "/api/browsers/notes/n1/approve")] or "Order placed." not in (edited[-1] or {}).get("text", ""):
        say(f"approving did not save the operator's words first: {answered}")
    if bs.notes[0]["status"] != "active":
        say("the procedure was not approved")

    # A recording the give-back ends is kept; discarding it asks first.
    page.locator(f"{ROOT} .bp-rec-btn").click()
    page.locator(".bp-rec-start .btn.primary").click()
    page.wait_for_selector(f"{ROOT} .bp-recbar", timeout=5000)
    bs.record_step("g1", action="click", element={"role": "link", "name": "Running shoes"})
    bs.set_control("g1", "agent")
    card = page.locator(f"{ROOT} .bp-rec[data-state='stopped']")
    card.wait_for(timeout=5000)
    if page.locator(f"{ROOT} .bp-recbar").count():
        say("the bar stayed after the give-back")
    card.locator(".bp-rec-foot .btn:not(.primary)").click()
    page.wait_for_selector(".dialog", timeout=5000)
    page.locator(".dialog .btn.danger, .dialog .btn.primary").last.click()
    page.wait_for_selector(f"{ROOT} .bp-rec", state="detached", timeout=5000)
    if not calls(bs, "/workflows/w") or calls(bs, "/workflows/w")[-1][0] != "DELETE":
        say(f"the recording was not discarded: {calls(bs, '/workflows/')}")
    context.close()


PROCEDURE = {
    "id": "p1", "project_id": "p1", "project": "Bakery", "host": "shop.example.com", "status": "proposed", "by": "operator", "proposed_at": 1790600000, "approved_at": 0,
    "kind": "procedure", "title": "Export the monthly report", "source": "w1",
    "text": "Export the monthly report\nRecorded by the operator in the agent's browser on shop.example.com.\n\n"
            + "\n".join(f"{n}. Click the button \"Step {n}\" (BrowserAct click)." for n in range(1, 13)),
}


def settings(browser, scenes, lang: str, problems: list[str]) -> None:  # type: ignore[no-untyped-def]
    say = lambda text: problems.append(f"[{lang}] {text}")  # noqa: E731
    bs = BrowserStub(scenes)
    bs.notes = [dict(PROCEDURE)]
    host = Host(bs)
    page = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark").new_page()
    page.route("**/api/**", host.route)
    page.goto(f"{BASE}/settings/environments?token=t&lang={lang}")
    row = page.locator(".bs-note[data-kind='procedure']")
    row.wait_for(timeout=15000)
    text = row.inner_text()
    if "Export the monthly report" not in text or WORDS[lang]["procedure"] not in text:
        say(f"the procedure reads {text!r}")
    pre = row.locator(".bs-proc-text")
    folded = pre.evaluate("e => e.scrollHeight > e.clientHeight + 4")
    row.locator(".bs-proc-more").click()
    whole = pre.evaluate("e => e.scrollHeight <= e.clientHeight + 1")
    if not folded or not whole:
        say(f"a long procedure is not folded and then shown whole: {folded} {whole}")
    shot(page, f"steps-settings-{lang}")
    row.locator(".btn:not(.primary):not(.danger)").first.click()
    editor = page.locator(".bs-note[data-kind='procedure'] .bp-proc")
    editor.wait_for(timeout=5000)
    editor.locator(".bp-proc-title").fill("Export the report for a month")
    editor.get_by_role("button", name=WORDS[lang]["save"], exact=True).click()
    editor.get_by_role("button", name=WORDS[lang]["later"], exact=True).click()
    page.wait_for_selector(".bs-note[data-kind='procedure'] .bp-proc", state="detached", timeout=5000)
    patched = [b for m, p, b in bs.requests if m == "PATCH"]
    if not patched or patched[-1].get("title") != "Export the report for a month":
        say(f"the edit was saved as {patched}")
    page.locator(".bs-note[data-kind='procedure'] .btn.primary").click()
    page.wait_for_selector(".bs-note[data-kind='procedure'][data-status='active']", timeout=5000)
    page.context.close()

    phone = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark").new_page()
    bs.notes = [dict(PROCEDURE)]
    phone.route("**/api/**", Host(bs).route)
    phone.goto(f"{BASE}/settings/environments?token=t&lang={lang}")
    phone.wait_for_selector(".bs-note[data-kind='procedure']", timeout=15000)
    phone.locator(".bs-note[data-kind='procedure'] .btn:not(.primary):not(.danger)").first.click()
    phone.wait_for_selector(".bs-note .bp-proc", timeout=5000)
    width = phone.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    print(f"[{lang}] phone widths with the editor open: {width}")
    if width[0] > width[1]:
        say(f"Settings scrolls sideways on a phone with a procedure open: {width}")
    phone.context.close()


def main() -> int:
    problems: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        scenes = render_scenes(browser)
        for lang in ("en", "ru"):
            panel(browser, scenes, lang, problems)
            settings(browser, scenes, lang, problems)
        browser.close()
    print("problems:", problems or "none")
    return 1 if problems else 0


if __name__ == "__main__":
    expect_app(BASE)
    failed = main()
    sys.exit(failed or UNHANDLED.report())
