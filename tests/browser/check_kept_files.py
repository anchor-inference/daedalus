"""Files kept by handle, as the operator sees them in the main chat and in a project orchestrator's
chat, at 1440 px and on a 390 px phone, in both languages.

What is checked is what the operator relies on. The operator's message shows their own words; the
host's list of what they attached (a line of ``att:`` handles written for the model) is not printed
in the bubble, and each file is a card under it instead. A card is also drawn under an event card
that names a member's file and under an answer that names one. A card downloads the bytes from
``/api/files/<id>/download`` and opens the file in the preview, whatever machine it came from. Both
chats offer to attach a file. Nothing scrolls sideways.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, FocusStub, MainStub, expect_app  # noqa: E402
from screenshots import UNHANDLED  # noqa: E402
from screenshots import stub as installation

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
PID = "b4k3ry20f0c5"

SPEC_ID, ESTIMATE_ID = "0123456789ab", "fedcba987654"
SPEC_NAME = "подсказки-free-pro.md"
SPEC = "# Подсказки free/pro\n\nFree users get three hints a day.\n"
FILES = {
    SPEC_ID: {"id": SPEC_ID, "handle": f"att:{SPEC_ID}", "name": SPEC_NAME, "mime": "text/markdown", "size": len(SPEC.encode()), "sha256": "a" * 64, "origin": "operator", "created_at": "2026-09-26T11:37:00Z"},
    ESTIMATE_ID: {"id": ESTIMATE_ID, "handle": f"att:{ESTIMATE_ID}", "name": "estimate.md", "mime": "text/markdown", "size": 20, "sha256": "b" * 64, "origin": "staff", "created_at": "2026-09-26T11:50:00Z"},
}
BODIES = {SPEC_ID: SPEC, ESTIMATE_ID: "Estimate: two days.\n"}

WORDS = {
    "en": {"ask": "Pass this to langpt, an estimate first", "from_you": "from you", "from_staff": "from staff", "download": "Download", "attach": "Attach files"},
    "ru": {"ask": "Передай langpt, сначала пусть оценит", "from_you": "от вас", "from_staff": "от сотрудника", "download": "Скачать", "attach": "Приложить файлы"},
}

ATTACHED = (
    "\n\nAttached files (kept by handle; Read one with Peek(op='read', path=…); hand them to staff with Assign or Tell (files=[…])):\n"
    f"- att:{SPEC_ID} {SPEC_NAME} (text/markdown, {len(SPEC.encode())} bytes)"
)


def messages(lang: str, *, seq: int, events_head: str) -> list[dict]:
    """The operator's message with a file, the event that brought the member's file back, and the
    answer that passes it on: what the chats of the chain look like after a hand-over."""
    base = {"thinking": "", "tool_calls": [], "tool_results": []}
    return [
        {**base, "role": "user", "seq": seq, "origin": "operator", "text": WORDS[lang]["ask"] + ATTACHED, "created_at": "2026-09-26T11:37:00Z"},
        {**base, "role": "assistant", "seq": seq + 1, "text": "langpt has it.", "created_at": "2026-09-26T11:37:20Z"},
        {**base, "role": "user", "seq": seq + 2, "origin": "events", "created_at": "2026-09-26T11:50:00Z",
         "text": f"{events_head}\n- 11:50 langpt reported done: \"estimated\" — files: att:{ESTIMATE_ID} estimate.md (20 B)"},
        {**base, "role": "assistant", "seq": seq + 3, "text": f"The estimate is in att:{ESTIMATE_ID}.", "created_at": "2026-09-26T11:50:10Z"},
    ]


def fits(page: Page, where: str) -> None:
    overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"{where}: the page scrolls sideways by {overflow}px"


def serve(page: Page, answer) -> list[str]:  # type: ignore[no-untyped-def]
    """The routes of the chats, the files by handle and their bytes; everything else the installation's."""
    downloads: list[str] = []

    def handle(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        url = urlsplit(request.url)
        path = url.path[url.path.index("/api/"):] if "/api/" in url.path else ""
        if path == "/api/files" and request.method == "GET":
            ids = [i.removeprefix("att:") for i in (parse_qs(url.query).get("ids") or [""])[0].split(",") if i]
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"files": [FILES[i] for i in ids if i in FILES]}))
        found = re.fullmatch(r"/api/files/([0-9a-f]{12})/download", path)
        if found and request.method == "GET":
            downloads.append(found.group(1))
            return route.fulfill(status=200, content_type="text/markdown; charset=utf-8", body=BODIES[found.group(1)])
        body = request.post_data_json if request.method in ("POST", "PUT", "PATCH") and request.post_data else None
        answered = answer(request.method, path, url.query, body)
        if answered is not None:
            status, payload = answered
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return installation(route)

    page.route("**/api/**", handle)
    return downloads


def check_chat(page: Page, chat: str, lang: str, where: str, downloads: list[str]) -> None:
    words = WORDS[lang]
    bubble = page.locator(f"{chat} .msg.user", has_text=words["ask"])
    expect(bubble).to_be_visible()
    text = bubble.inner_text()
    assert "att:" not in text and "Attached files" not in text, f"{where}: the host's list is printed in the bubble: {text!r}"
    # The operator's file is a card under their message; the member's under the event and the answer.
    spec = page.locator(f"{chat} .kept-files .artifact", has_text=SPEC_NAME)
    expect(spec).to_have_count(1)
    expect(spec.locator(".artifact-meta")).to_contain_text(words["from_you"])
    estimate = page.locator(f"{chat} .kept-files .artifact", has_text="estimate.md")
    expect(estimate).to_have_count(2)
    expect(estimate.first.locator(".artifact-meta")).to_contain_text(words["from_staff"])
    link = spec.locator("a.iconbtn")
    href = link.get_attribute("href") or ""
    assert href.startswith(f"/api/files/{SPEC_ID}/download?path=") and "token=" in href, href
    # The card opens the file in the preview: its bytes, read by handle.
    spec.locator(".artifact-main").click()
    expect(page.get_by_text("Free users get three hints a day.").first).to_be_visible()
    assert SPEC_ID in downloads, f"{where}: the preview did not read the file by its handle"
    page.keyboard.press("Escape")
    fits(page, where)
    # The chat takes attachments: the composer's plus menu offers them (a sheet of tiles on a phone).
    page.locator(f"{chat} .composer .plus, {chat} .composer [aria-haspopup='menu']").first.click()
    expect(page.get_by_role("menuitem", name=words["attach"]).or_(page.locator(".ph-plus-sheet .ph-tile", has_text=words["attach"]))).to_be_visible()
    page.keyboard.press("Escape")


def main_chat(page: Page, lang: str, where: str) -> None:
    main = MainStub(lang)
    main.opened = True
    main.detail["messages"] = main.detail["messages"] + messages(lang, seq=10, events_head="[reports · 1 since 11:50]")
    downloads = serve(page, lambda method, path, query, body: main.answer(method, path, body))
    page.goto(f"{BASE}/orchestration?token=t&lang={lang}")
    expect(page.locator(".chat .timeline")).to_be_visible()
    check_chat(page, ".chat", lang, where, downloads)


def orchestrator_chat(page: Page, lang: str, where: str) -> None:
    focus = FocusStub.bakery(lang)
    detail = focus.details["orch-bakery"]
    detail["messages"] = detail["messages"] + messages(lang, seq=100, events_head="[events · Bakery 2.0 · 1 since 11:50]")
    downloads = serve(page, focus.answer)
    page.goto(f"{BASE}/orchestration/project/{PID}?token=t&lang={lang}")
    chat = ".chat.in-project.orchestrator"
    expect(page.locator(chat)).to_be_visible()
    check_chat(page, chat, lang, where, downloads)


def main() -> int:
    expect_app(BASE)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)
        for lang in ("en", "ru"):
            for width, height, touch in ((1440, 900, False), (390, 844, True)):
                for name, run in (("main chat", main_chat), ("orchestrator chat", orchestrator_chat)):
                    page = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch, is_mobile=touch).new_page()
                    run(page, lang, f"{lang} {width}px {name}")
                    page.context.close()
            print(f"kept files {lang}: ok")
        browser.close()
    return UNHANDLED.report()


if __name__ == "__main__":
    raise SystemExit(main())
