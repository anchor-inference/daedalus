"""The files an operator attached to a message, as the chat draws them, in a real browser.

A message that carried files used to show the list the host writes for the model — ``Attached
files:`` and a path per file — in the bubble, and nothing else: no thumbnail, no card. This opens a
conversation with three such messages, written the way the history on a real installation holds
them (a Windows clipboard paste named ``{GUID}.png``, names with spaces, parentheses and Cyrillic, a
secret handed over with the files, and an upload with no words at all), and checks what the operator
relies on:

- the pictures are a row of square thumbnails above the bubble, right-aligned with it, each drawn
  from the session's attachment route;
- the PDF and the Word document are cards with their own type colour, the name and the kind;
- the bubble holds the operator's words and the secret's chip, and never the list or a path;
- a message that was only a file draws no empty bubble;
- a thumbnail opens the centred viewer, and the PDF card opens the preview;
- nothing scrolls sideways, at 1440 px and on a 390 px phone, in both languages and both themes.

A reply typed to an AskUser question with files attached used to show the same list as its answer,
with no card. The conversation also holds one, carrying a picture named with no extension and a PDF:

- the answered question draws a thumbnail and a card under its line, folded or not, and its answer
  reads as the operator's words alone;
- the picture with no extension opens the centred viewer, as its thumbnail promised, and not the
  panel: where it opens is decided by its type.

``OUT`` names a directory to leave a desktop and a phone picture in (dark).

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/miniapp/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_attach_cards.py
"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import (  # noqa: E402
    DEFAULT_APP,
    INBOX_DOCX,
    INBOX_PASTE,
    INBOX_PDF,
    INBOX_PHOTO,
    INBOX_SHOT,
    expect_app,
    inbox_attachments,
    inbox_list,
)

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
OUT = os.environ.get("OUT", "")

WORDS = {
    "en": {"ask": "check this out (the tester wrote it). Just check it.", "pdf": "PDF", "docx": "Document", "secret": "log in with this",
           "redo": "Redo the login page.", "question": "Which layout should the login page use?", "reply": "Like this sketch, and keep the report's wording"},
    "ru": {"ask": "глянь (тестировщик написал). Просто проверь.", "pdf": "PDF", "docx": "Документ", "secret": "войди с этим",
           "redo": "Переделай страницу входа.", "question": "Какую раскладку взять для страницы входа?", "reply": "Как на этом наброске, и формулировки из отчёта"},
}
ASK_ID = "ask-files"


def messages(lang: str) -> list[dict]:
    base = {"thinking": "", "tool_calls": [], "tool_results": [], "origin": "operator", "internal": False}
    files = (INBOX_SHOT, INBOX_PHOTO, INBOX_PDF, INBOX_DOCX)
    return [
        {**base, "role": "user", "seq": 900, "created_at": "2026-10-30T14:12:21Z", "text": WORDS[lang]["ask"] + "\n\n" + inbox_list(*files), "attachments": inbox_attachments(*files)},
        {**base, "role": "assistant", "seq": 901, "origin": "", "created_at": "2026-10-30T14:12:40Z", "text": "Read both: the form loses the password after a wrong code."},
        {**base, "role": "user", "seq": 902, "created_at": "2026-10-30T14:13:00Z", "text": WORDS[lang]["secret"] + "\n\n" + inbox_list(INBOX_SHOT, root="/home/you/Daedalus/projects/shop/inbox"),
         "attachments": inbox_attachments(INBOX_SHOT), "secrets": [{"name": "demo_pwd", "scope": "session"}]},
        {**base, "role": "assistant", "seq": 903, "origin": "", "created_at": "2026-10-30T14:13:20Z", "text": "Signed in."},
        {**base, "role": "user", "seq": 904, "created_at": "2026-10-30T14:14:00Z", "text": "File attached.\n\n" + inbox_list(INBOX_PDF), "attachments": inbox_attachments(INBOX_PDF)},
        {**base, "role": "assistant", "seq": 905, "origin": "", "created_at": "2026-10-30T14:14:20Z", "text": "Got the report."},
        {**base, "role": "user", "seq": 906, "created_at": "2026-10-30T14:15:00Z", "text": WORDS[lang]["redo"]},
        {**base, "role": "assistant", "seq": 907, "origin": "", "created_at": "2026-10-30T14:15:10Z", "text": "",
         "tool_calls": [{"id": ASK_ID, "name": "AskUser", "arguments": {"questions": [{"question": WORDS[lang]["question"], "options": [{"label": "Centred card"}, {"label": "Split screen"}]}]}}]},
        # The reply as the host records it: the words with the list the model reads, and the files beside them.
        {**base, "role": "tool", "seq": 908, "origin": "", "created_at": "2026-10-30T14:16:00Z", "text": "",
         "tool_results": [{"id": ASK_ID, "is_error": False, "attachments": inbox_attachments(INBOX_PASTE, INBOX_PDF),
                           "content": json.dumps({"answers": [{"question": WORDS[lang]["question"], "selected": [], "custom": WORDS[lang]["reply"] + "\n\n" + inbox_list(INBOX_PASTE, INBOX_PDF)}], "source": "user"}, ensure_ascii=False)}]},
        {**base, "role": "assistant", "seq": 909, "origin": "", "created_at": "2026-10-30T14:16:20Z", "text": "Going with the sketch."},
    ]


def serve(page: Page, lang: str) -> list[str]:
    fetched: list[str] = []

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        if url.path == f"/api/sessions/{shots.S1}" and request.method == "GET":
            detail = deepcopy(shots.detail(shots.S1))
            detail["messages"] = detail["messages"] + messages(lang)
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))
        if url.path == f"/api/sessions/{shots.S1}/attachments/download":
            fetched.append((parse_qs(url.query).get("path") or [""])[0])
        return shots.stub(route)

    page.route("**/api/**", handle)
    return fetched


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def check_answer(page: Page, lang: str, where: str, fetched: list[str], shot: str = "") -> None:
    """The reply to a question that carried files: its thumbnail and card under the answered question,
    its words without the list, and the picture with no extension opening the viewer."""
    words = WORDS[lang]
    heads = page.locator(".thinking-head[aria-expanded='false']")
    while heads.count():
        heads.first.click()
        page.wait_for_timeout(50)
    record = page.locator(".ask-record").filter(has=page.locator(".ask-record-files"))
    expect(record).to_have_count(1)
    record.scroll_into_view_if_needed()
    expect(record.locator(".ask-record-head-a")).to_have_text(words["reply"])
    files = record.locator(".ask-record-files")
    expect(files.locator(".msg-thumb.ready img")).to_have_count(1)
    assert INBOX_PASTE in fetched, f"{where}: the reply's picture was not read by its inbox name: {fetched}"
    expect(files.locator(".msg-file[data-kind='pdf'] .msg-file-name")).to_have_text(INBOX_PDF)
    # Under the line, at the body's indent, and not off to the right with the operator's bubbles.
    line, strip = record.locator(".act").bounding_box(), files.locator(".msg-thumb").bounding_box()
    assert line and strip and strip["y"] >= line["y"] + line["height"] - 1 and strip["x"] < line["x"] + line["width"] / 2, f"{where}: the reply's files are not under its line: {line} {strip}"
    text = record.inner_text()
    assert "Attached files" not in text and "/inbox/" not in text, f"{where}: the answered question prints the list: {text!r}"
    fits(page, where)
    if shot:
        record.evaluate("node => node.scrollIntoView({ block: 'center' })")
        page.mouse.move(1, 1)
        page.wait_for_timeout(300)
        page.screenshot(path=shot)
    record.locator(".act").click()
    expect(record.locator(".ask-record-a")).to_have_text(words["reply"])
    assert "Attached files" not in record.inner_text(), f"{where}: the opened question prints the list"
    # Named with no extension, the picture still opens in the viewer: its type says what it is.
    files.locator(".msg-thumb").click()
    viewer = page.locator(".media-viewer")
    expect(viewer).to_be_visible()
    expect(viewer.locator("img")).to_be_visible()
    page.keyboard.press("Escape")
    expect(viewer).to_have_count(0)


def check(page: Page, lang: str, scheme: str, where: str, shot: str = "", answer_shot: str = "") -> None:
    words = WORDS[lang]
    fetched = serve(page, lang)
    page.goto(f"{BASE}/agents/{shots.S1}?token=t&scheme={scheme}&lang={lang}")
    first = page.locator(".msg-wrap").filter(has=page.locator(".msg.user", has_text=words["ask"]))
    expect(first).to_have_count(1)
    first.scroll_into_view_if_needed()
    # The pictures: one row of square thumbnails, drawn from the attachment route by inbox name.
    thumbs = first.locator(".msg-thumbs .msg-thumb")
    expect(thumbs).to_have_count(2)
    expect(first.locator(".msg-thumb.ready img")).to_have_count(2)
    assert INBOX_SHOT in fetched and INBOX_PHOTO in fetched, f"{where}: the thumbnails were not read by inbox name: {fetched}"
    for index in range(2):
        box = thumbs.nth(index).bounding_box()
        assert box and abs(box["width"] - box["height"]) <= 1, f"{where}: a thumbnail is not square: {box}"
    # The other files: a card each, with the type's colour, the name and the kind.
    pdf = first.locator(".msg-file[data-kind='pdf']")
    docx = first.locator(".msg-file[data-kind='document']")
    expect(pdf.locator(".msg-file-name")).to_have_text(INBOX_PDF)
    expect(pdf.locator(".msg-file-kind")).to_have_text(words["pdf"])
    expect(docx.locator(".msg-file-name")).to_have_text(INBOX_DOCX)
    expect(docx.locator(".msg-file-kind")).to_have_text(words["docx"])
    colours = {kind: first.locator(f".msg-file[data-kind='{kind}'] .msg-file-icon").evaluate("node => getComputedStyle(node).backgroundColor") for kind in ("pdf", "document")}
    assert colours["pdf"] != colours["document"], f"{where}: a PDF and a document share a colour: {colours}"
    # The bubble: the operator's words, never the list written for the model.
    bubble = first.locator(".msg.user")
    text = bubble.inner_text()
    assert text.strip() == words["ask"], f"{where}: the bubble holds more than the operator's words: {text!r}"
    # Above the bubble, and right-aligned with it.
    row, card, said = first.locator(".msg-thumbs").bounding_box(), docx.bounding_box(), bubble.bounding_box()
    assert row and card and said and row["y"] + row["height"] <= said["y"] and card["y"] + card["height"] <= said["y"], f"{where}: the files are not above the bubble"
    assert abs((row["x"] + row["width"]) - (said["x"] + said["width"])) <= 2 and abs((card["x"] + card["width"]) - (said["x"] + said["width"])) <= 2, f"{where}: the files are not aligned with the bubble"
    # A secret handed over with a file keeps its chip in the bubble.
    second = page.locator(".msg-wrap").filter(has=page.locator(".msg.user", has_text=words["secret"]))
    expect(second.locator(".msg-thumb")).to_have_count(1)
    expect(second.locator(".msg.user .msg-secrets")).to_contain_text("demo_pwd")
    assert "Attached files" not in second.locator(".msg.user").inner_text()
    # Only a file and no words: a card and no empty bubble.
    lone = page.locator(".msg-wrap").filter(has=page.locator(".msg-file")).filter(has_not=page.locator(".msg.user"))
    expect(lone).to_have_count(1)
    expect(lone.locator(".msg-file-name")).to_have_text(INBOX_PDF)
    assert not page.locator(".timeline .msg.user", has_text="Attached files").count(), f"{where}: a bubble still prints the list"
    assert not page.locator(".timeline .msg.user", has_text="/inbox/").count(), f"{where}: a bubble still prints a path"
    fits(page, where)
    if shot:
        first.evaluate("node => node.scrollIntoView({ block: 'start' })")
        page.wait_for_timeout(300)
        page.screenshot(path=shot)
    check_answer(page, lang, where, fetched, answer_shot)
    first.scroll_into_view_if_needed()
    # A thumbnail opens the centred viewer, with the picture in it.
    thumbs.first.click()
    viewer = page.locator(".media-viewer")
    expect(viewer).to_be_visible()
    expect(viewer.locator("img")).to_be_visible()
    page.keyboard.press("Escape")
    expect(viewer).to_have_count(0)
    # The PDF card opens the preview.
    pdf.click()
    expect(page.locator(".preview-pdf, .pdf-pages, canvas, .preview-body").first).to_be_visible()
    fits(page, where)


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for scheme in ("dark", "light"):
                for width, height, phone in ((1440, 900, False), (390, 844, True)):
                    where = f"{lang} {scheme} {width}px"
                    shot = answer_shot = ""
                    if OUT and lang == "en" and scheme == "dark":
                        shot = str(Path(OUT) / f"attach-cards-{'phone' if phone else 'desktop'}-dark.png")
                        answer_shot = str(Path(OUT) / f"answer-files-{'phone' if phone else 'desktop'}-dark.png")
                    if OUT and lang == "ru" and scheme == "light" and not phone:
                        answer_shot = str(Path(OUT) / "answer-files-desktop-light-ru.png")
                    if OUT and lang == "ru" and scheme == "light" and not phone:
                        shot = str(Path(OUT) / "attach-cards-desktop-light-ru.png")
                    context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=2, color_scheme=scheme, is_mobile=phone, has_touch=phone)  # type: ignore[arg-type]
                    check(context.new_page(), lang, scheme, where, shot, answer_shot)
                    context.close()
                    print(f"attach cards {where}: ok")
        browser.close()
    return shots.UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
