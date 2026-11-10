"""A slow accepted send stays visible in the chat before the host answers."""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshots as shots  # noqa: E402
from api_stub import expect_app  # noqa: E402

BASE = os.environ.get("APP_URL", shots.BASE)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
WORDS = "A message whose acknowledgement takes several seconds"
UPLOAD_WORDS = "Please read the sample file"


def run() -> None:
    expect_app(BASE)
    sent: list[dict[str, str]] = []

    def stub(route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = urlsplit(request.url).path
        if path == "/api/canvases" and "session_id=" in urlsplit(request.url).query:
            item = {"id": "0123456789abcdef0123456789abcdef", "title": "Flow", "version": 1, "created_at": "2020-01-01T00:00:00Z", "updated_at": "2020-01-01T00:00:00Z", "updated_by": "agent",
                    "shared": False, "scope": "chat", "scope_id": shots.S1, "scope_name": "Chat", "owner_session_id": shots.S1, "nodes": 1, "edges": 0}
            return route.fulfill(status=200, content_type="application/json", body=json.dumps({"scopes": [{"kind": "chat", "id": shots.S1, "name": "Chat"}], "items": [item]}))
        if path == f"/api/sessions/{shots.S1}/messages" and request.method == "POST":
            sent.append({"text": request.post_data_json["text"], "id": request.post_data_json["client_message_id"]})
            return route.fulfill(status=200, content_type="application/json", body='{"run_id":"slow-run"}')
        if path == f"/api/sessions/{shots.S1}/upload" and request.method == "POST":
            raw = request.post_data_buffer.decode("utf-8")
            def field(name: str) -> str:
                return raw.split(f'name="{name}"', 1)[1].split("\r\n\r\n", 1)[1].split("\r\n", 1)[0]
            sent.append({"text": field("text") + "\n\nAttached files:\n- /data/workspaces/s1/inbox/sample.txt (text/plain, 7 bytes)", "id": field("client_message_id"), "upload": "yes",
                         "attachments": [{"name": "sample.txt", "mime": "text/plain", "size": 7, "file": "sample.txt"}]})
            return route.fulfill(status=200, content_type="application/json", body='{"run_id":"upload-run"}')
        if path == f"/api/sessions/{shots.S1}" and request.method == "GET":
            page = deepcopy(shots.detail(shots.S1))
            for index, item in enumerate(sent):
                page["messages"].append({"role": "user", "seq": 999999 + index, "text": item["text"], "attachments": item.get("attachments", []), "client_message_id": item["id"], "thinking": "", "tool_calls": [], "tool_results": [], "created_at": "2020-01-01T00:00:00Z" if item.get("upload") else datetime.now(UTC).isoformat()})
            return route.fulfill(status=200, content_type="application/json", body=json.dumps(page))
        return shots.stub(route)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.add_init_script("""(() => {
          const original = window.fetch.bind(window);
          window.fetch = (input, init) => {
            if (String(input).includes('/messages') && init?.method === 'POST')
              return new Promise(resolve => setTimeout(() => resolve(original(input, init)), 4000));
            return original(input, init);
          };
        })()""")
        page.route("**/api/**", stub)
        page.goto(f"{BASE}/agents/{shots.S1}?token=t&lang=en")
        expect(page.locator(".chat-title")).to_be_visible()
        expect(page.get_by_role("link", name="Chat canvases (1)")).to_be_visible()
        page.locator(".composer .composer-field").fill(WORDS)
        page.locator(".composer [data-action=send]").click()
        expect(page.locator(".timeline .msg.user").last).to_contain_text(WORDS, timeout=1000)
        expect(page.locator(".timeline .msg.user").last).to_be_in_viewport(timeout=1000)
        assert not sent, "the temporary bubble should precede the delayed request"
        page.wait_for_timeout(4600)
        assert len(sent) == 1 and sent[0]["text"] == WORDS
        expect(page.locator(".timeline .msg.user").filter(has_text=WORDS)).to_have_count(1, timeout=8000)
        page.locator('.composer input[type="file"]').first.set_input_files({"name": "sample.txt", "mimeType": "text/plain", "buffer": b"example"})
        page.locator(".composer .composer-field").fill(UPLOAD_WORDS)
        page.locator(".composer [data-action=send]").click()
        expect(page.locator(".timeline .msg.user").filter(has_text=UPLOAD_WORDS)).to_have_count(1, timeout=8000)
        assert len(sent) == 2 and sent[1]["id"] != sent[0]["id"]
        # The upload is one message: its words in the bubble, the file as a card above it, and the
        # list written for the model nowhere on the page.
        expect(page.locator(".timeline .msg-wrap").filter(has_text=UPLOAD_WORDS).locator(".msg-file", has_text="sample.txt")).to_have_count(1)
        expect(page.locator(".timeline .msg.user").filter(has_text="Attached files")).to_have_count(0)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if page.get_by_role("dialog", name="Project files panel").is_visible():
            page.keyboard.press("Escape")
        # A phone keeps the chat's canvases as a counted tile of the bar's ⋮ sheet.
        page.locator(".ph-chat-top button[aria-haspopup='menu']").first.click()
        tile = page.locator(".ph-session-menu [data-tile='canvas']")
        expect(tile).to_be_in_viewport()
        expect(tile).to_contain_text("1")
        tile.click()
        expect(page.locator(".canvas-screen")).to_contain_text("Flow")
        browser.close()
    assert not shots.UNHANDLED.report()


if __name__ == "__main__":
    run()
