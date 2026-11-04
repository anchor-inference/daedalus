"""The Notes tab, driven end to end against the stubbed host.

    APP_URL=http://127.0.0.1:8163/app CHROMIUM=/path/to/chrome python3 tests/browser/check_notes.py

A plain chat has only its own notes; a chat in a real project has Project | Chat sub-tabs with counts
and remembers the last one. A note is created by typing, saved by itself, and found again after the
editor closes. A pasted picture uploads at the caret and becomes a reference, `[[` picks a file, the
reading view draws the picture and opens it in the viewer, and a task box ticks its own line. A save
refused because the note changed elsewhere opens the prompt instead of overwriting; a change made
elsewhere while nothing is unsaved arrives live. Delete offers Undo. A phone reaches all of it from
the chat's menu, in the drawer, without a sideways scroll.
"""

from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notes_seed  # noqa: E402
from api_stub import DEFAULT_APP, NOTES, expect_app, preview_png  # noqa: E402
from event_feed import EventFeed  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402
from screenshots import S1, S6, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PLAIN = S6  # "Expense tracker": a chat in a scratch project of its own
problems: list[str] = []


def check(ok: bool, what: str) -> None:
    if not ok:
        problems.append(what)
        print(f"PROBLEM: {what}")


def open_notes(page: Page, session: str, lang: str = "en") -> None:
    page.goto(f"{BASE}/agents/{session}?token=t&scheme=dark&lang={lang}&panel=notes")
    expect(page.locator(".notes-panel")).to_be_visible(timeout=15000)


def writes(method: str, fragment: str) -> list:
    return [w for w in NOTES.writes if w[0] == method and fragment in w[1]]


def paste_picture(page: Page) -> None:
    data = base64.b64encode(preview_png("pasted", 320, 200)).decode()
    page.evaluate(
        """(data) => {
            const field = document.querySelector('.ne-field');
            field.focus();
            const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
            const clip = new DataTransfer();
            clip.items.add(new File([bytes], 'image.png', { type: 'image/png' }));
            field.dispatchEvent(new ClipboardEvent('paste', { clipboardData: clip, bubbles: true, cancelable: true }));
        }""",
        data,
    )


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    check(overflow <= 1, f"{where}: the page scrolls sideways by {overflow}px")


def plain_chat(page: Page) -> None:
    """Create, autosave, reopen; a pasted picture, a [[ reference, the viewer; no sub-tabs."""
    NOTES.reset()
    open_notes(page, PLAIN)
    check(page.locator(".notes-subtabs").count() == 0, "a plain chat shows Project | Chat sub-tabs")
    expect(page.locator(".notes-empty")).to_be_visible()
    page.locator(".notes-empty .btn.primary").click()
    dialog = page.locator(".ne-dialog")
    expect(dialog).to_be_visible()
    # An untouched new note leaves nothing behind.
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    check(not writes("POST", f"/sessions/{PLAIN}/notes"), "closing an empty new note created one")

    page.locator(".notes-new").click()
    page.locator(".ne-title").fill("Trip budget")
    page.locator(".ne-field").click()
    page.keyboard.type("Plan\n- flights")
    page.keyboard.press("Enter")
    page.keyboard.type("hotel")
    expect(page.locator(".ne-state.ok")).to_be_visible(timeout=5000)
    check(page.locator(".ne-field").input_value() == "Plan\n- flights\n- hotel", f"Enter did not continue the list: {page.locator('.ne-field').input_value()!r}")
    created = writes("POST", f"/sessions/{PLAIN}/notes")
    check(len(created) == 1, f"the new note was created {len(created)} times")
    note_id = next(iter(NOTES.notes))
    check(NOTES.notes[note_id]["body"] == "Plan\n- flights\n- hotel", "the autosave did not reach the host")

    # A pasted picture: a stand-in at the caret, then the reference.
    page.locator(".ne-field").press("End")
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")
    paste_picture(page)
    page.wait_for_function("document.querySelector('.ne-field').value.includes('](files/')", timeout=8000)
    body = page.locator(".ne-field").input_value()
    check("uploading:" not in body and body.count("![") == 1, f"the pasted picture did not become one reference: {body!r}")

    # [[ opens the files of the chat; Enter puts the reference in.
    page.keyboard.press("Enter")
    page.keyboard.type("Again: [[scr")
    menu = page.locator(".ne-menu")
    expect(menu).to_be_visible()
    expect(menu.locator(".ne-menu-item.on")).to_contain_text("screenshot-")
    page.keyboard.press("Enter")
    expect(menu).to_have_count(0)
    body = page.locator(".ne-field").input_value()
    check(body.count("](files/") == 2 and "[[" not in body, f"[[ did not insert a reference: {body!r}")
    page.wait_for_function("document.querySelector('.ne-state.ok') !== null", timeout=5000)

    # The reading view draws the picture; a press opens the viewer.
    page.locator(".ne-modes button").nth(2).click()
    picture = page.locator(".ne-read .note-img").first
    page.wait_for_function("document.querySelector('.ne-read .note-img.loaded') !== null", timeout=8000)
    picture.click()
    expect(page.locator(".media-viewer")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator(".media-viewer")).to_have_count(0)
    expect(dialog).to_be_visible()
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)

    # Reopened, the note is what was typed; the card says so too.
    card = page.locator(".note-card").first
    expect(card.locator(".note-title")).to_have_text("Trip budget")
    expect(card.locator(".note-thumb img")).to_be_visible()
    card.locator(".note-open").click()
    expect(page.locator(".ne-read")).to_contain_text("hotel")
    page.keyboard.press("Escape")


def project_chat(page: Page, lang: str) -> None:
    """Sub-tabs with counts that remember; a task ticks its line; delete and Undo."""
    ids = notes_seed.seed(NOTES, lang)
    NOTES.writes.clear()
    page.evaluate("localStorage.removeItem('daedalus.notes.subtab')")
    open_notes(page, S1, lang)
    tabs = page.locator(".notes-subtab")
    expect(tabs).to_have_count(2)
    expect(tabs.nth(0).locator(".notes-subtab-count")).to_have_text("3")
    expect(tabs.nth(1).locator(".notes-subtab-count")).to_have_text("2")
    expect(page.locator(".note-card")).to_have_count(3)
    # Pinned first.
    check("pinned" in (page.locator(".note-card").first.get_attribute("class") or ""), f"{lang}: the pinned note is not first")
    tabs.nth(1).click()
    expect(page.locator(".note-card")).to_have_count(2)
    page.reload()
    expect(page.locator(".notes-subtab.on")).to_have_attribute("data-scope", "chat")

    # The search marks what it found and counts per sub-tab.
    page.locator(".notes-search input").fill("croiss" if lang == "en" else "круасс")
    expect(page.locator(".note-card")).to_have_count(1)
    expect(page.locator(".notes-subtab").nth(1).locator(".notes-subtab-count")).to_have_text("1")
    page.locator(".notes-search-clear").click()

    # A task box in the reading view ticks its own line and saves.
    page.locator(f".note-card[data-note='{ids['shoot']['id']}'] .note-open").click()
    box = page.locator(".ne-read .task-toggle").first
    expect(box).to_have_attribute("aria-checked", "false")
    box.click()
    page.wait_for_function("document.querySelector('.ne-state.ok') !== null", timeout=5000)
    check("- [x]" in NOTES.notes[ids["shoot"]["id"]]["body"], f"{lang}: the ticked task was not saved")
    expect(page.locator(".ne-read .task-toggle").first).to_have_attribute("aria-checked", "true")
    page.keyboard.press("Escape")
    expect(page.locator(".ne-dialog")).to_have_count(0)

    # Delete from the card's menu; Undo brings it back from the host.
    card = page.locator(f".note-card[data-note='{ids['questions']['id']}']")
    card.hover()
    card.locator("button[aria-haspopup='menu'], .menu-trigger").first.click()
    page.locator(".menu [role='menuitem'].danger").click()
    expect(card).to_have_count(0)
    expect(page.locator(".toast")).to_be_visible()
    page.locator(".toast .btn").click()
    expect(page.locator(f".note-card[data-note='{ids['questions']['id']}']")).to_have_count(1)
    check(bool(writes("POST", "/restore")), f"{lang}: Undo did not restore the note on the host")


def conflict_and_live(page: Page, feed: EventFeed) -> None:
    ids = notes_seed.seed(NOTES, "en")
    page.route("**/api/events**", feed.route)
    open_notes(page, S1)
    for _ in range(50):
        if feed.connected():
            break
        page.wait_for_timeout(100)
    check(bool(feed.connected()), "the page never opened the event stream")
    page.locator(".notes-subtab[data-scope='project']").click()

    # Live: the agent rewrites the open note while nothing here is unsaved; it arrives in place.
    launch = ids["launch"]["id"]
    page.locator(f".note-card[data-note='{launch}'] .note-open").click()
    expect(page.locator(".ne-read")).to_contain_text("Menu data file")
    NOTES.changed_elsewhere(launch, body="- [x] Everything is done", by="agent")
    feed.send("notes.changed", {"scope": "project", "scope_id": notes_seed.P1, "change": "updated", "by": "agent", "note_id": launch, "version": 2}, project=notes_seed.P1)
    expect(page.locator(".ne-read")).to_contain_text("Everything is done", timeout=5000)
    expect(page.locator(".ne-state.flash")).to_be_visible()

    # Conflict: someone saves while words typed here are unsaved; the save asks instead of overwriting.
    page.locator(".ne-modes button").first.click()
    NOTES.changed_elsewhere(launch, body="- [ ] The agent's own list", by="agent")
    page.locator(".ne-field").press("End")
    page.keyboard.press("Enter")
    page.keyboard.type("mine")
    prompt = page.locator(".ne-conflict")
    expect(prompt).to_be_visible(timeout=5000)
    expect(prompt.locator(".ne-diff-row.mine").last).to_contain_text("- [ ] mine")
    expect(prompt.locator(".ne-diff-row.theirs")).to_contain_text("The agent's own list")
    check(NOTES.notes[launch]["body"] == "- [ ] The agent's own list", "a stale save overwrote the newer note")
    prompt.locator(".btn.primary").click()
    expect(prompt).to_have_count(0)
    page.wait_for_function("document.querySelector('.ne-state.ok') !== null", timeout=5000)
    forced = [w for w in writes("PATCH", f"/notes/{launch}") if isinstance(w[2], dict) and w[2].get("force")]
    check(len(forced) == 1 and NOTES.notes[launch]["body"].endswith("- [ ] mine"), "Keep mine did not save over the newer version")
    page.keyboard.press("Escape")
    expect(page.locator(".ne-dialog")).to_have_count(0)


def phone(page: Page, lang: str) -> None:
    notes_seed.seed(NOTES, lang)
    page.goto(f"{BASE}/agents/{S1}?token=t&scheme=dark&lang={lang}", wait_until="networkidle")
    page.locator(".ph-chat-top button[aria-haspopup='menu']").first.click()
    tile = page.locator(".ph-session-menu [data-tile='notes']")
    expect(tile).to_be_visible()
    tile.click()
    sheet = page.locator(".panel-sheet")
    expect(sheet.locator(".panel-tab[data-tab='notes'].on")).to_be_visible()
    expect(sheet.locator(".notes-subtabs")).to_be_visible()
    fits(page, f"{lang} phone list")
    for el in ("notes-new", "notes-shelf-toggle"):
        height = sheet.locator(f".{el}").bounding_box()["height"]
        check(height >= 43.5, f"{lang} phone: .{el} is {height}px tall, under the 44 px target")
    sheet.locator(".note-card .note-open").first.click()
    dialog = page.locator(".ne-dialog")
    expect(dialog).to_be_visible()
    box = dialog.bounding_box()
    check(box["width"] >= 389 and box["height"] >= 800, f"{lang} phone: the editor is not the whole screen ({box})")
    fits(page, f"{lang} phone editor")
    dialog.locator(".ne-modes button").first.click()
    expect(dialog.locator(".ne-field")).to_be_visible()
    check(dialog.locator(".ne-modes button").count() == 2, f"{lang} phone: the editor offers a split view")
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)


def run() -> int:
    expect_app(BASE)
    feed = EventFeed()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROMIUM)
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.set_default_timeout(10000)
            page.route("**/api/**", stub)
            plain_chat(page)
            print("notes: plain chat ok")
            for lang in ("en", "ru"):
                project_chat(page, lang)
                print(f"notes: project chat {lang} ok")
            context.close()
            context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark")
            page = context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.set_default_timeout(10000)
            page.route("**/api/**", stub)
            conflict_and_live(page, feed)
            print("notes: conflict and live ok")
            context.close()
            for lang in ("en", "ru"):
                context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme="dark")
                page = context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.set_default_timeout(10000)
                page.route("**/api/**", stub)
                phone(page, lang)
                print(f"notes: phone {lang} ok")
                context.close()
            browser.close()
            for error in errors:
                check(False, f"the page threw: {error}")
    finally:
        feed.close()
    return 1 if problems else UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(run())
