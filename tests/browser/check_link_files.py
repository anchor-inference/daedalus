"""Files and tasks over an agent link, in both languages, schemes and widths.

- a conversation's card in the Agents tab has its tasks over the thread: one under way with Cancel, one done with
  its result; Cancel posts to the task and the card then says it was cancelled;
- the thread draws what came as files: a picture as a thumbnail fetched with the app's credential, any other file
  as a name, a size and the start of its hash, each with Download;
- a held message with files lists each with a tick; the one unticked is posted as `drop` and the rest go;
- the files a guest's turn on the chat's own link wants to send wait over the composer as their own approval,
  posted to the link's outbox; the tab's badge counts them;
- a link's conversation in the Agent link sheet has the guest's task with Cancel, the files of each message and
  the same approval for the held files;
- nothing scrolls sideways, on a phone either.

    APP_URL=http://127.0.0.1:8293/app SHOTS=/tmp/shots python3 tests/browser/check_link_files.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_stub  # noqa: E402
from api_stub import DEFAULT_APP, FakeGuests, FakeTalks, expect_app  # noqa: E402
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402
from screenshots import S1, UNHANDLED, stub  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")

WORDS = {
    "en": {"working": "Working", "done": "Done", "cancelled": "Cancelled", "files": "Files for Mira's assistant", "actions": "Session actions", "menu": "Agent link…", "read": "Read", "largest": "Largest file", "perday": "Files per day", "free": "No limit"},
    "ru": {"working": "В работе", "done": "Готова", "cancelled": "Отменена", "files": "Файлы для Mira's assistant", "actions": "Действия с сессией", "menu": "Ссылка для агента…", "read": "Читать", "largest": "Размер файла", "perday": "Файлы за день", "free": "Без лимита"},
}


def snap(page: Page, name: str) -> None:
    if os.environ.get("SHOTS"):
        page.wait_for_timeout(500)  # a phone's sheet slides in; a picture taken in the middle of it shows both layers
        page.screenshot(path=f"{os.environ['SHOTS']}/{name}.png", full_page=False)


def snap_element(page: Page, selector: str, name: str) -> None:
    if os.environ.get("SHOTS"):
        page.wait_for_timeout(300)
        page.locator(selector).first.screenshot(path=f"{os.environ['SHOTS']}/{name}.png")


def fits(page: Page, where: str) -> None:
    over = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert over <= 0, f"{over}px of horizontal scroll: {where}"


def thumbnail_drawn(row) -> None:  # type: ignore[no-untyped-def]
    """The picture is a blob the app fetched, and it decoded: a broken one is an <img> with no width."""
    image = row.locator("[data-link-file-thumb] img")
    expect(image).to_have_attribute("src", re.compile(r"^blob:"))
    assert image.evaluate("img => img.complete && img.naturalWidth > 0"), "the thumbnail did not decode"


def check_tab(browser, lang: str, scheme: str, width: int, height: int) -> None:  # type: ignore[no-untyped-def]
    words = WORDS[lang]
    name = f"{lang}-{scheme}-{width}"
    api_stub.GUESTS = FakeGuests(exchange=True)
    talks = FakeTalks(S1, exchange=True)
    api_stub.TALKS = talks
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = context.new_page()
    page.route("**/api/**", stub)

    # Over the composer: the held message with its two files, and the guest's files as their own approval.
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}", wait_until="domcontentloaded")
    above = page.locator(".composer [data-talk-held='3']")
    expect(above).to_be_visible()
    expect(above.locator("[data-link-picks] input[type='checkbox']")).to_have_count(2)
    guest = page.locator(".composer [data-guest-held='ob-1']")
    expect(guest).to_be_visible()
    expect(guest).to_have_attribute("aria-label", words["files"])
    expect(guest.locator("[data-link-picks] input[type='checkbox']")).to_have_count(2)
    snap(page, f"approvals-{name}")
    fits(page, f"approvals {name}")

    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}&panel=agents", wait_until="domcontentloaded")
    tab = page.locator("[data-agents-tab]")
    card = tab.locator("[data-talk='t1']")
    expect(card).to_be_visible()
    # The badge counts the held message and the guest's set of files.
    if width >= 1024:
        expect(page.locator(".panel-tab[data-tab='agents']")).to_contain_text("2")
    expect(tab.locator("[data-group='guest-held'] [data-guest-held='ob-1']")).to_be_visible()

    working = card.locator("[data-link-task='k-order']")
    expect(working.locator("[data-link-task-state]")).to_have_text(words["working"])
    done = card.locator("[data-link-task='k-menu']")
    expect(done.locator("[data-link-task-state]")).to_have_text(words["done"])
    expect(done.locator("[data-link-task-result]")).to_contain_text("twelve cakes")
    expect(done.locator("[data-link-task-cancel]")).to_have_count(0)

    thread = card.locator("[data-talk-messages]")
    picture = thread.locator("[data-link-file='101']")
    expect(picture).to_be_visible()
    thumbnail_drawn(picture)
    pdf = thread.locator("[data-link-file='102']")
    expect(pdf.locator("[data-link-file-thumb]")).to_have_count(0)
    expect(pdf.locator("[data-link-file-hash]")).to_have_attribute("title", re.compile(r"^[0-9a-f]{64}$"))
    expect(pdf.locator("[data-link-file-save]")).to_be_visible()
    expect(thread.locator("[data-link-kind='task']").first).to_be_visible()
    expect(card.locator("[data-talk-limit='file-mb']")).to_contain_text(words["largest"])
    expect(card.locator("[data-talk-limit='day-mb']")).to_contain_text(words["perday"])
    snap(page, f"tab-{name}")
    fits(page, f"tab {name}")

    # The file limits: "No limit" sends 0.
    card.get_by_role("switch", name=f"{words['largest']}: {words['free']}", exact=True).click()
    settings = [body for verb, _, body in talks.calls if verb == "PUT"]
    assert {"file_mb": 0} in settings, settings

    # Cancel the task under way.
    working.locator("[data-link-task-cancel]").click()
    expect(working.locator("[data-link-task-state]")).to_have_text(words["cancelled"])
    assert any(verb == "POST" and path.endswith("/tasks/k-order/cancel") for verb, path, _ in talks.calls), talks.calls

    # Untick the text file of the held message and send: only it is dropped.
    held = card.locator("[data-talk-held='3']")
    thumbnail_drawn(held.locator("[data-link-file='103']"))
    held.locator("[data-link-file='104'] input[type='checkbox']").uncheck()
    snap_element(page, "[data-agents-tab] [data-talk='t1'] [data-talk-held='3']", f"held-{name}")
    held.locator("[data-action='send']").click()
    expect(page.locator("[data-talk-held]")).to_have_count(0)
    sent = [body for verb, path, body in talks.calls if verb == "POST" and path.endswith("/messages/3/send")]
    assert sent == [{"drop": [104]}], sent
    expect(thread.locator("[data-link-file='104'] [data-link-file-word='dropped']")).to_be_visible()

    # The guest's files: keep the picture back, send the quote.
    group = tab.locator("[data-guest-held='ob-1']")
    group.locator("[data-link-file='204'] input[type='checkbox']").uncheck()
    group.locator("[data-action='send']").click()
    expect(page.locator("[data-guest-held]")).to_have_count(0)
    outbox = [body for verb, path, body in api_stub.GUESTS.calls if verb == "POST" and path.endswith("/outbox/ob-1/send")]
    assert outbox == [{"drop": [204]}], outbox
    snap(page, f"tab-after-{name}")
    fits(page, f"tab after {name}")
    context.close()


def check_link(browser, lang: str, scheme: str, width: int, height: int) -> None:  # type: ignore[no-untyped-def]
    """The owner's side: a link's conversation in the Agent link sheet."""
    words = WORDS[lang]
    name = f"{lang}-{scheme}-{width}"
    guests = FakeGuests(exchange=True)
    api_stub.GUESTS = guests
    api_stub.TALKS = FakeTalks(S1)
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = context.new_page()
    page.route("**/api/**", stub)
    page.goto(f"{BASE}/agents/{S1}?lang={lang}&scheme={scheme}", wait_until="domcontentloaded")
    open_sheet(page, words)
    page.locator("[data-guest-link='1a2b']").get_by_role("button", name=words["read"], exact=True).click()
    reader = page.locator("[data-guest-reader]")
    expect(reader).to_be_visible()
    task = reader.locator("[data-link-task='g-cake']")
    expect(task.locator("[data-link-task-state]")).to_have_text(words["working"])
    expect(reader.locator("[data-guest-held='ob-1'] [data-link-picks] input[type='checkbox']")).to_have_count(2)
    message = reader.locator(".guest-msg[data-kind='task']")
    thumbnail_drawn(message.locator("[data-link-file='201']"))
    expect(message.locator("[data-link-file='202'] .link-file-name")).to_have_text("order-form.docx")
    expect(reader.locator("[data-link-kind='status']")).to_contain_text(words["working"])
    snap(page, f"reader-{name}")
    fits(page, f"reader {name}")

    task.locator("[data-link-task-cancel]").click()
    expect(task.locator("[data-link-task-state]")).to_have_text(words["cancelled"])
    reader.locator("[data-guest-held='ob-1'] [data-action='refuse']").click()
    expect(reader.locator("[data-guest-held]")).to_have_count(0)
    assert any(path.endswith("/outbox/ob-1/refuse") for verb, path, _ in guests.calls if verb == "POST"), guests.calls

    # The form: a direct link has the two file limits, a stand-in none.
    page.locator("[data-guest-reader] .guest-bar button").first.click()
    page.locator("[data-guest-edit='3c4d']").click()
    expect(page.locator("[data-guest-limit='file_mb']")).to_contain_text(words["largest"])
    expect(page.locator("[data-guest-limit='day_mb']")).to_contain_text(words["perday"])
    page.locator("[data-guest-level='stand_in'] input").check()
    expect(page.locator("[data-guest-limit='file_mb']")).to_have_count(0)
    context.close()


def open_sheet(page: Page, words: dict[str, str]) -> None:
    """The Agent link sheet, from the chat's actions, as check_guest_links.py opens it."""
    page.get_by_role("button", name=words["actions"], exact=True).click()
    # A phone's actions are rows of the bar's sheet; a desktop's, a menu under its header button.
    page.wait_for_selector(".ph-session-menu, [role='menu']", timeout=5000)
    sheet = page.locator(".ph-session-menu")
    if sheet.count():
        sheet.get_by_role("button", name=words["menu"], exact=True).click()
    else:
        page.get_by_role("menuitem", name=words["menu"], exact=True).click()
    expect(page.locator("[data-guest-sheet]")).to_be_visible()


def main() -> None:
    expect_app(BASE)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM, headless=True, args=["--no-sandbox"])
        for lang in WORDS:
            for scheme in ("light", "dark"):
                for width, height in ((1280, 800), (390, 844)):
                    check_tab(browser, lang, scheme, width, height)
                    check_link(browser, lang, scheme, width, height)
        browser.close()
    if UNHANDLED.report():
        raise SystemExit(1)
    print("link files: ok")


if __name__ == "__main__":
    main()
