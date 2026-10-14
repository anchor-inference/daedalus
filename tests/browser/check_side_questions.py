"""Side questions in an ordinary chat, at 1440 px and on a 390 px phone, in both languages.

What the operator asked for, checked the way they would use it. An agent that leaves questions for
later gets a Questions tab beside its chat — first in the panel, with the count — and the chat carries
one line for the list; a chat without any has no such tab. An answer may bring files: a screenshot
pasted into the answer field (a synthetic clipboard event carrying an image), a file picked with the
paperclip, each shown as a chip that can be taken off again. A question the agent takes back leaves
the list at once with its reason; one the operator does not need goes with "Not needed" and wakes
nobody. Send carries the ready answers and their files in one multipart request, each answer naming
its own files by their places among the uploads. On a phone the header's button opens the same list
as a sheet, its round controls a thumb's size. Nothing scrolls sideways.

``SHOTS`` names a directory to write the phone's screenshots into, in the dark and the light theme.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, Unhandled, expect_app, fulfil_shared  # noqa: E402
from check_message_actions import DETAIL  # noqa: E402
from event_feed import EventFeed  # noqa: E402

UNHANDLED = Unhandled()
BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = os.environ.get("SHOTS", "")
SID = "sess-side"
REASON = "found it in the README"
PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000" "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")

WORDS = {
    "en": {
        "tab": "Questions", "line": "4 questions waiting", "asker": "Side question", "withdrawn": "The agent took it back", "aside": "Put aside",
        "sent": "2 answers sent", "sent1": "1 answer sent", "empty": "When the agent has a question that can wait",
        "port": "Which port should the dev server use?", "look": "What should the empty state look like?", "readme": "Is the old README still current?", "tabs": "Tabs or spaces in the new files?",
    },
    "ru": {
        "tab": "Вопросы", "line": "4 вопроса ждут ответа", "asker": "Попутный вопрос", "withdrawn": "Агент снял вопрос", "aside": "Убрано без ответа",
        "sent": "Отправлено 2 ответа", "sent1": "Отправлен 1 ответ", "empty": "Когда у агента появится вопрос",
        "port": "Какой порт взять для dev-сервера?", "look": "Как должно выглядеть пустое состояние?", "readme": "Старый README ещё актуален?", "tabs": "Табы или пробелы в новых файлах?",
    },
}


def side(ask_id: str, short: str, text: str, options: list[str] | None = None, at: str = "10:00") -> dict:
    """One entry of ``GET /api/questions?session=…``, as ``daedalus/extensions/questions.py`` draws a side question."""
    return {
        "id": ask_id, "short_id": short, "project_id": None, "origin": "session", "kind": "question", "staff_id": None, "staff_session_id": None, "task_id": None,
        "request_ref": "", "text": text, "detail": {"options": options or []}, "routed_to": "operator", "suggestion": "",
        "created_at": f"2026-10-14T{at}:00Z", "routed_at": f"2026-10-14T{at}:00Z", "resolved_at": None, "resolved_by": None, "resolution": {}, "dispatch_id": None,
        "title": "", "heading": text, "session_id": SID, "project_name": "", "asker": "agent", "section": "questions", "options": options or [], "multi": False,
        "host": False, "always": False, "always_server": "", "urgent": False, "files": True,
    }


class SideStub:
    """The host's side of one session's side questions: the open list, and what was sent to it."""

    def __init__(self, lang: str) -> None:
        w = WORDS[lang]
        self.open = [
            side("ask-port", "qp0rt1", w["port"], ["8080", "3000"], "10:00"),
            side("ask-look", "ql00k2", w["look"], None, "10:01"),
            side("ask-readme", "qr3ad3", w["readme"], None, "10:02"),
            side("ask-tabs", "qt4bs4", w["tabs"], ["Tabs", "Spaces"], "10:03"),
        ]
        self.sends: list[dict] = []
        self.dismissed: list[str] = []
        self.detail = copy.deepcopy(DETAIL)
        self.detail.update(id=SID, title="Dev server")

    def withdraw(self, ask_id: str) -> None:
        self.open = [q for q in self.open if q["id"] != ask_id]

    def route(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else url.path
        if path.endswith("/stream"):
            return route.fulfill(status=200, content_type="text/event-stream", body="event: hello\ndata: {}\n\n")
        if path == "/api/questions" and request.method == "GET":
            wanted = parse_qs(url.query).get("session", [""])[0]
            return self.json(route, {"questions": self.open if wanted == SID else []})
        if path == f"/api/sessions/{SID}/asks/answer" and request.method == "POST":
            return self.json(route, self.answer(request))
        if path.startswith(f"/api/sessions/{SID}/asks/") and path.endswith("/dismiss") and request.method == "POST":
            ask_id = path.split("/")[5]
            self.dismissed.append(ask_id)
            self.withdraw(ask_id)
            return self.json(route, {"ask": {"id": ask_id, "resolution": {"dismissed": True}}})
        if path == f"/api/sessions/{SID}" and request.method == "GET":
            return self.json(route, self.detail)
        if path == "/api/auth/me":
            return self.json(route, {"user_id": 1, "via": "token"})
        if path == "/api/sessions" and request.method == "GET":
            return self.json(route, {"sessions": [], "projects": []})
        if fulfil_shared(route):
            return None
        if path.startswith(f"/api/sessions/{SID}/"):
            # The session's own lesser reads (its files, jobs, unread mark): nothing to show.
            return self.json(route, {})
        UNHANDLED.record(path)
        return self.json(route, [])

    def answer(self, request) -> dict:  # type: ignore[no-untyped-def]
        kind = request.headers.get("content-type", "")
        sent: dict = {"type": kind, "items": None, "files": []}
        if kind.startswith("multipart/form-data"):
            message = BytesParser(policy=email_policy).parsebytes(f"Content-Type: {kind}\r\n\r\n".encode() + (request.post_data_buffer or b""))
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if name == "items":
                    sent["items"] = json.loads(part.get_content())
                elif name == "files":
                    sent["files"].append({"name": part.get_filename(), "type": part.get_content_type(), "size": len(part.get_payload(decode=True) or b"")})
        else:
            sent["items"] = request.post_data_json
        self.sends.append(sent)
        results = []
        for item in sent["items"] or []:
            results.append({"ask_id": item["ask_id"], "state": "answered", "applied": True, "delivered": True, "error": ""})
            self.withdraw(item["ask_id"])
        return {"batch_id": "b-stub", "results": results}

    @staticmethod
    def json(route, body) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"
    clipped = page.evaluate("""() => [...document.querySelectorAll('.q-card, .q-card .q-files, .q-card .attachment')]
      .map((el) => el.getBoundingClientRect()).filter((r) => r.width && (r.right > window.innerWidth + 0.5 || r.left < -0.5)).length""")
    assert clipped == 0, f"{where}: {clipped} part(s) of a card run off the side"


def card(scope, short: str):  # type: ignore[no-untyped-def]
    return scope.locator(f".questions-panel .q-card[data-ask='{short}']")


def paste_image(page: Page, field_selector: str) -> None:
    """A screenshot pasted from the clipboard, the way a browser delivers it: a paste event whose
    data carries an image file named "image.png"."""
    page.evaluate(
        """([selector, bytes]) => {
          const field = document.querySelector(selector);
          const data = new DataTransfer();
          data.items.add(new File([new Uint8Array(bytes)], 'image.png', { type: 'image/png' }));
          field.focus();
          field.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
        }""",
        [field_selector, list(PNG)],
    )


def open_page(page: Page, stub: SideStub, feed: EventFeed | None, lang: str) -> None:
    page.route("**/api/**", stub.route)
    if feed is not None:
        page.route("**/api/events**", feed.route)
    page.goto(f"{BASE}/agents/{SID}?token=t&lang={lang}")


def desktop(page: Page, feed: EventFeed, lang: str) -> None:
    words = WORDS[lang]
    stub = SideStub(lang)
    open_page(page, stub, feed, lang)
    chat = page.locator(".chat")
    expect(chat.locator(".questions-line")).to_contain_text(words["line"])
    chat.locator(".questions-line").click()
    tab = page.locator(".panel .panel-tab[data-tab='questions']")
    expect(tab).to_have_attribute("aria-selected", "true")
    expect(page.locator(".panel .panel-tab").first).to_have_attribute("data-tab", "questions")
    expect(tab.locator(".count")).to_have_text("4")
    expect(card(page, "qp0rt1").locator(".q-meta")).to_contain_text(words["asker"])
    expect(card(page, "qp0rt1").locator(".q-attach")).to_be_visible()
    fits(page, f"{lang} 1440 list")

    # A screenshot pasted into the answer field becomes a chip on that answer, named for what it is.
    paste_image(page, ".q-card[data-ask='ql00k2'] .q-field")
    look = card(page, "ql00k2")
    expect(look.locator(".attachment")).to_have_count(1)
    expect(look.locator(".attachment-name")).to_have_text(re.compile(r"^screenshot-\d{8}-\d{6}\.png$"))
    expect(look.locator(".attachment-open img")).to_have_count(1)
    expect(look).to_have_attribute("data-state", "ready")
    expect(look.locator(".q-field")).to_have_value("")

    # A file picked with the paperclip; a second one taken off again.
    port = card(page, "qp0rt1")
    port.locator(".q-chip[data-option='3000']").click()
    with page.expect_file_chooser() as chosen:
        port.locator(".q-attach").click()
    chosen.value.set_files([{"name": "ports.txt", "mimeType": "text/plain", "buffer": b"8080 is taken"}, {"name": "extra.log", "mimeType": "text/plain", "buffer": b"x"}])
    expect(port.locator(".attachment")).to_have_count(2)
    port.locator(".attachment", has_text="extra.log").locator(".attachment-x").click()
    expect(port.locator(".attachment-name")).to_have_text(["ports.txt"])
    fits(page, f"{lang} 1440 with files")

    # The agent settles one itself: it leaves the list at once, with its reason.
    for _ in range(50):
        if feed.connected():
            break
        page.wait_for_timeout(100)
    assert feed.connected(), f"{lang}: the page never opened the event stream"
    stub.withdraw("ask-readme")
    feed.send("ask.side", {"change": "withdrawn", "ask_ids": ["ask-readme"], "reason": REASON}, session=SID)
    gone = card(page, "qr3ad3")
    expect(gone).to_have_attribute("data-state", "withdrawn")
    expect(gone.locator(".q-fate")).to_contain_text(words["withdrawn"])
    expect(gone.locator(".q-fate")).to_contain_text(REASON)
    expect(gone).to_have_count(0, timeout=10000)
    expect(tab.locator(".count")).to_have_text("3")

    # Send: one multipart request, each answer naming its own files among the uploads.
    page.locator(".questions-send-btn").click()
    expect(page.get_by_text(words["sent"])).to_be_visible()
    assert len(stub.sends) == 1, stub.sends
    sent = stub.sends[0]
    assert sent["type"].startswith("multipart/form-data"), sent["type"]
    assert sent["items"] == [{"ask_id": "ask-port", "selected": ["3000"], "files": [0]}, {"ask_id": "ask-look", "text": "", "files": [1]}], sent["items"]
    assert [f["name"] for f in sent["files"]][0] == "ports.txt" and re.fullmatch(r"screenshot-\d{8}-\d{6}\.png", sent["files"][1]["name"]), sent["files"]
    assert sent["files"][1]["type"] == "image/png" and sent["files"][1]["size"] == len(PNG), sent["files"]
    for short in ("qp0rt1", "ql00k2"):
        expect(card(page, short)).to_have_count(0, timeout=10000)

    # The one left is not needed: it goes, and nothing is sent for it.
    card(page, "qt4bs4").locator(".q-aside").click()
    expect(card(page, "qt4bs4")).to_have_count(0, timeout=10000)
    assert stub.dismissed == ["ask-tabs"] and len(stub.sends) == 1, (stub.dismissed, stub.sends)
    # The tab stays while it is open, calmly empty; closed and opened again, a chat with nothing
    # waiting has no Questions tab at all.
    expect(page.locator(".questions-empty")).to_contain_text(words["empty"])
    page.locator(".panel .panel-actions button").last.click()
    expect(page.locator(".panel")).to_have_count(0)
    page.locator(".chat-head button[aria-pressed]").last.click()
    expect(page.locator(".panel .panel-tab").first).to_be_visible()
    expect(page.locator(".panel .panel-tab[data-tab='questions']")).to_have_count(0)
    expect(chat.locator(".questions-line")).to_have_count(0)
    fits(page, f"{lang} 1440 after the send")


def phone(page: Page, lang: str, theme: str) -> None:
    words = WORDS[lang]
    stub = SideStub(lang)
    open_page(page, stub, None, lang)
    button = page.locator(".questions-headbtn").first
    expect(button).to_be_visible()
    expect(page.locator(".questions-line")).to_contain_text(words["line"])
    fits(page, f"{lang} 390 chat")
    button.tap()
    sheet = page.locator(".panel-sheet")
    expect(sheet).to_be_visible()
    expect(sheet.locator(".panel-tab[data-tab='questions']")).to_have_attribute("aria-selected", "true")
    expect(sheet.locator(".q-card")).to_have_count(4)
    for control in (".q-attach", ".q-aside"):
        # Both boxes in one read: the sheet is still sliding up, and two reads see it in two places.
        box, icon, radius = sheet.locator(f".q-card[data-ask='qp0rt1'] {control}").evaluate(
            "(e) => { const r = e.getBoundingClientRect(), i = e.querySelector('.ic').getBoundingClientRect();"
            " return [{x: r.x, y: r.y, w: r.width, h: r.height}, {x: i.x, y: i.y, w: i.width, h: i.height}, getComputedStyle(e).borderRadius]; }")
        assert box["w"] >= 43.5 and abs(box["w"] - box["h"]) < 1 and radius == "50%", f"{lang} 390: {control} is {box} ({radius}), not a round thumb-sized control"
        assert abs((icon["x"] + icon["w"] / 2) - (box["x"] + box["w"] / 2)) < 1 and abs((icon["y"] + icon["h"] / 2) - (box["y"] + box["h"] / 2)) < 1, f"{lang} 390: the icon of {control} is off centre"
    look = sheet.locator(".q-card[data-ask='ql00k2']")
    with page.expect_file_chooser() as chosen:
        look.locator(".q-attach").tap()
    chosen.value.set_files([{"name": "empty-state.png", "mimeType": "image/png", "buffer": PNG}])
    expect(look.locator(".attachment-name")).to_have_text(["empty-state.png"])
    look.locator(".q-field").fill("Like this, with a hint")
    fits(page, f"{lang} 390 sheet with a file")
    if SHOTS:
        page.screenshot(path=str(Path(SHOTS) / f"side-questions-phone-{lang}-{theme}.png"))
    send = sheet.locator(".questions-send-btn")
    send_box = send.bounding_box()
    assert send_box and send_box["y"] + send_box["height"] <= 844 + 0.5, f"{lang} 390: Send is below the screen ({send_box})"
    send.tap()
    expect(page.get_by_text(words["sent1"])).to_be_visible()
    [sent] = stub.sends
    assert sent["items"] == [{"ask_id": "ask-look", "text": "Like this, with a hint", "files": [0]}], sent["items"]
    assert [f["name"] for f in sent["files"]] == ["empty-state.png"], sent["files"]
    expect(sheet.locator(".q-card[data-ask='ql00k2']")).to_have_count(0, timeout=10000)
    expect(sheet.locator(".panel-tab[data-tab='questions'] .count")).to_have_text("3")
    fits(page, f"{lang} 390 after the send")


def main() -> int:
    expect_app(BASE)
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
    feed = EventFeed()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROMIUM)
            for lang in ("en", "ru"):
                context = browser.new_context(viewport={"width": 1440, "height": 900})
                desktop(context.new_page(), feed, lang)
                context.close()
                for theme in ("dark", "light"):
                    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, color_scheme=theme)  # type: ignore[arg-type]
                    phone(context.new_page(), lang, theme)
                    context.close()
                print(f"side questions {lang}: ok")
            browser.close()
    finally:
        feed.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    sys.exit(main())
