"""The extension's side panel page (/app/panel), against a stubbed panel door and a fake extension.

The Daedalus extension frames /app/panel and answers the page's handshake from its own origin. A
check cannot be that origin, so the fake here stands in one step later: an init script catches the
page's READY on its way to the parent and answers with the HELLO a real panel sends (the credential,
the current tab, a MessageChannel port), then plays the extension on the port and records every
request. Everything else is the built app against a stub of ``/api/panel/*``. What is checked, at a
side panel's width:

- opened outside the panel, the page says what it is and asks for nothing;
- the conversation of the active tab is shown, with its pending action as a card that has the focus:
  Enter allows it once through the panel's door, Esc refuses the next one;
- the active tab moving to another conversation's group moves the panel there; pinned, it stays;
- "This page" asks the extension for the page's text and attaches it as a file;
- the composer's approval chip offers the three modes and sets one through the panel's door, and the
  composer's row does not spill at 360 or 420 px;
- a running conversation shows one compact activity line inside the composer, with no Stop of its
  own; the composer's Stop goes to the door, and a stop the stream never confirms says so after three
  seconds; the line stays one row at 360 and 420 px; Take over pauses the group and shows its tab,
  and the note it leaves sits under the header, clear of the composer, until clicked away;
- a new conversation shows the consent summary once and the app's whole composer (attach, dictation,
  the model, the approval mode, the page's offers) on one row at 360 and 420 px, starts on the
  operator's words, gives the current tab to its group before the first message, and sends it;
- no request leaves for an operator's route, and the page carries the panel's credential only;
- no sideways scroll at 360 px; the words follow the language the extension says, ru and en.

    cd miniapp && npm run build
    mkdir -p /tmp/app-root && ln -s "$PWD/dist" /tmp/app-root/app
    python3 tests/browser/serve_app.py 8163 /tmp/app-root &
    APP_URL=http://127.0.0.1:8163/app python3 tests/browser/check_side_panel.py

Exit 0 when every step holds.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_stub import DEFAULT_APP, expect_app  # noqa: E402

BASE = os.environ.get("APP_URL", DEFAULT_APP)
CHROMIUM = os.environ.get("CHROMIUM", "/usr/local/bin/chromium")
SHOTS = Path(os.environ.get("SHOTS", "/tmp/side-panel-shots"))
EXTENSION = "chrome-extension://lkhhklicnjlplokpadpccndipomikoaj"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAAA4UlEQVR42u3dsQmAMBBAUSMuYB8wkGUcw9Ecw2WEbOAKbhBtBDnea9Pd59KFpJyXgbhGIxAYgREYgREYgRFYYKKY+sfzsZvR/13rZoNd0QiMwAiMwAiMwAgsMAIjMAIjMAIjsMAIjMAIzPeS56M2GIERGIERGIERWGCieHhdWEo1o/daO20wAiMwAiOwwAiMwAiMwAiMwAIjMAIjMAIjMAILjMAIjMAIjMAIjMACIzACIzACIzACC4zACIzACIzACCywEQiMwAiMwAiMwAgsMFH4u9AGIzACIzACIzACC0wYN66lB8/TbcQGAAAAAElFTkSuQmCC"

# The extension, one step after its origin: it hears the page's READY and answers as the panel does.
FAKE_EXTENSION = """
(() => {
  const EXT = "__EXT__";
  const shell = { requests: [], port: null, lang: window.__panelLang || "en", current: window.__panelCurrent || null };
  window.__shell = shell;
  const answers = {
    "page.text": { tab: 7, url: "https://bank.example/bill", title: "Your bill", text: "Amount due: 42.00" },
    "page.selection": { tab: 7, url: "https://bank.example/bill", title: "Your bill", text: "Amount due" },
    "capture.visible": { tab: 7, url: "https://bank.example/bill", title: "Your bill", image: "data:image/png;base64,__PNG__" },
    "tab.grant": { id: 7, group: "g", url: "https://bank.example/bill" },
    "tab.focus": { tab: 7 },
    "group.pause": { group: "g", paused: true },
    "open.app": {},
    "token.refresh": { token: "pnl_check", expires: "" },
  };
  const real = window.postMessage.bind(window);
  window.postMessage = function (message, target, transfer) {
    if (message && message.t === "DAEDALUS_PANEL_READY") {
      shell.ready = { message, target };
      const channel = new MessageChannel();
      shell.port = channel.port1;
      channel.port1.onmessage = (e) => {
        const { id, op, args } = e.data;
        shell.requests.push({ op, args });
        channel.port1.postMessage({ id, ok: true, value: op === "tab.current" ? shell.current : answers[op] ?? null });
      };
      const hello = { t: "DAEDALUS_PANEL_HELLO", proto: 1, nonce: message.nonce, generation: 1, token: "pnl_check", expires: "", lang: shell.lang,
        current: shell.current, show: null, profile: "Work", connected: true };
      setTimeout(() => window.dispatchEvent(new MessageEvent("message", { data: hello, origin: EXT, source: window, ports: [channel.port2] })), 0);
      return;
    }
    return real(message, target, transfer);
  };
  shell.emit = (event, value) => shell.port.postMessage({ event, value });
})();
""".replace("__EXT__", EXTENSION).replace("__PNG__", PNG)


def tab(session: str | None, title: str, url: str) -> dict:
    return {"tab": {"id": 7, "url": url, "title": title, "readable": True}, "owner": {"group": f"s-{session}-u", "session": session, "title": title, "paused": False} if session else None}


def messages(*, refusal: str | None = None) -> list[dict]:
    out = [
        {"role": "user", "text": "Pay this month's electricity bill", "thinking": "", "created_at": "2026-10-20T10:00:00Z", "seq": 1, "tool_calls": [], "tool_results": []},
        {"role": "assistant", "text": "", "thinking": "", "created_at": "2026-10-20T10:00:02Z", "seq": 2, "tool_calls": [{"id": "c1", "name": "BrowserNavigate", "arguments": {"url": "https://bank.example/bill"}}], "tool_results": []},
    ]
    if refusal:
        out.append({"role": "tool", "text": "", "thinking": "", "created_at": "2026-10-20T10:00:03Z", "seq": 3, "tool_calls": [],
                    "tool_results": [{"id": "c1", "content": f"needs the operator's approval: bank.example is a new site in your Chrome. Approval key: {refusal}", "is_error": True}]})
    return out


class Door:
    """The panel's door: the conversations of this Chrome, and what was sent to each."""

    def __init__(self) -> None:
        self.sessions = [
            {"id": "s1", "title": "Pay the electricity bill", "status": "idle", "last_message_at": "2026-10-20T10:00:03Z", "needs_you": True, "group": "s-s1-u"},
            {"id": "s2", "title": "Find flights to Lisbon", "status": "idle", "last_message_at": "2026-10-20T09:00:00Z", "needs_you": False, "group": "s-s2-u"},
        ]
        self.consented = True
        self.details = {
            "s1": {"id": "s1", "title": "Pay the electricity bill", "status": "idle", "messages": messages(refusal="0123456789ab"), "model": "Claude Opus", "workspace": "/w", "folders": [], "pending": None, "thinking": True, "reasoning_effort": "medium"},
            "s2": {"id": "s2", "title": "Find flights to Lisbon", "status": "idle", "messages": messages()[:1], "model": "Claude Opus", "workspace": "/w", "folders": [], "pending": None, "thinking": True, "reasoning_effort": "medium"},
        }
        self.posts: list[tuple[str, object]] = []
        self.foreign: list[str] = []
        self.auth: set[str] = set()

    def __call__(self, route) -> None:  # type: ignore[no-untyped-def]
        request = route.request
        path = request.url.split("?", 1)[0]
        rel = path[path.index("/api/"):]
        self.auth.add(request.headers.get("authorization", ""))
        if not rel.startswith("/api/panel/"):
            self.foreign.append(f"{request.method} {rel}")
            return route.fulfill(status=401, content_type="application/json", body=json.dumps({"detail": "authentication required"}))
        if request.method != "GET":
            raw = request.post_data_buffer or b""
            body: object = json.loads(raw) if raw and "json" in (request.headers.get("content-type") or "") else raw.decode("utf-8", "replace")
            self.posts.append((rel, body))
        parts = rel.split("/")
        if rel == "/api/panel/sessions" and request.method == "GET":
            return self.json(route, {"sessions": self.sessions, "consented": self.consented, "consent": "The agent will work in your own Chrome, signed in as you.", "chrome": {"connected": True, "name": "Work"}})
        if rel == "/api/panel/sessions" and request.method == "POST":
            self.consented = True
            made = {"id": "s9", "title": "Find a birthday gift", "status": "idle", "last_message_at": "2026-10-20T11:00:00Z", "needs_you": False, "group": "s-s9-u"}
            self.sessions.insert(0, made)
            self.details["s9"] = {**self.details["s2"], "id": "s9", "title": made["title"], "messages": []}
            return self.json(route, {"id": "s9", "title": made["title"], "group": "s-s9-u", "model": "Claude Opus"})
        if rel == "/api/panel/settings":
            return self.json(route, {"presets": {"opus": {"label": "Claude Opus", "provider": "anthropic", "model": "claude-opus"}}, "providers": {"anthropic": {"kind": "anthropic", "name": "Anthropic", "billing": ""}}, "model": {"preset": "opus"}})
        if rel == "/api/panel/asr":
            return self.json(route, {"configured": True, "reason": "", "provider": "local", "model": "whisper", "max_seconds": 300, "autosend": False})
        if len(parts) >= 5 and parts[3] == "sessions":
            sid = parts[4]
            tail = "/".join(parts[5:])
            if sid not in self.details:
                return route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "no such conversation in this Chrome"}))
            if tail == "" and request.method == "GET":
                return self.json(route, self.details[sid])
            if tail == "stream":
                return route.fulfill(status=200, content_type="text/event-stream", body='event: hello\ndata: {"event_seq": 0}\n\n')
            if tail.startswith("policy/similar/"):
                return self.json(route, {"similar": None})
            if tail in ("messages", "upload"):
                return self.json(route, {"run_id": "r1", "receipt": {"status": "consumed"}})
            if tail == "browser-approvals":
                sent = body if request.method == "PUT" and isinstance(body, dict) else {}
                return self.json(route, {"mode": sent.get("mode", "manual"), "env": "user", "warning": ""})
            if tail == "tab":
                return self.json(route, {"group": f"s-{sid}-u", "created": True})
            return self.json(route, {"ok": True, "stopped": True})
        self.foreign.append(f"{request.method} {rel}")
        return route.fulfill(status=404, content_type="application/json", body="{}")

    @staticmethod
    def json(route, body: object) -> None:  # type: ignore[no-untyped-def]
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def posted(self, rel: str) -> list[object]:
        return [body for path, body in self.posts if path == rel]


def requests(page, op: str) -> list:  # type: ignore[no-untyped-def]
    return [r for r in page.evaluate("window.__shell.requests") if r["op"] == op]


def open_panel(browser, door: Door, *, lang: str = "en", current: dict | None = None, width: int = 400):  # type: ignore[no-untyped-def]
    ctx = browser.new_context(viewport={"width": width, "height": 760})
    ctx.add_init_script(f"window.__panelLang = {json.dumps(lang)}; window.__panelCurrent = {json.dumps(current or tab('s1', 'Your bill', 'https://bank.example/bill'))};")
    ctx.add_init_script(FAKE_EXTENSION)
    page = ctx.new_page()
    page.route("**/api/**", door)
    page.goto(f"{BASE}/panel")
    return ctx, page


def run() -> int:
    expect_app(BASE)
    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROMIUM)

        # Outside the panel: nothing answers the handshake, and the page says what it is.
        ctx = browser.new_context(viewport={"width": 400, "height": 600})
        page = ctx.new_page()
        alone = Door()
        page.route("**/api/**", alone)
        page.goto(f"{BASE}/panel")
        expect(page.locator(".sp-outside")).to_contain_text("side panel", timeout=10000)
        assert not alone.posts and not alone.foreign, (alone.posts, alone.foreign)
        ctx.close()

        door = Door()
        ctx, page = open_panel(browser, door)
        ready = page.evaluate("window.__shell.ready")
        assert ready["target"] == EXTENSION and len(ready["message"]["nonce"]) >= 16, ready
        expect(page.locator(".sp-title")).to_contain_text("Pay the electricity bill")
        card = page.locator(".sp-card")
        expect(card).to_contain_text("https://bank.example/bill")
        expect(card).to_be_focused()
        page.screenshot(path=str(SHOTS / "side-panel-approval.png"))
        page.keyboard.press("Enter")
        expect(card).to_have_count(0)
        assert door.posted("/api/panel/sessions/s1/policy/grant") == [{"key": "0123456789ab"}], door.posts

        # A second refusal comes in and is refused from the keyboard.
        door.details["s1"]["messages"] = messages(refusal="abcdefabcdef")
        page.evaluate("window.__shell.emit('event', {session: 's1', kind: 'needs_you', title: '', waiting: 1})")
        page.evaluate("window.__shell.emit('current', " + json.dumps(tab("s2", "Flights", "https://air.example/")) + ")")
        expect(page.locator(".sp-title")).to_contain_text("Find flights to Lisbon")
        page.evaluate("window.__shell.emit('current', " + json.dumps(tab("s1", "Your bill", "https://bank.example/bill")) + ")")
        expect(page.locator(".sp-title")).to_contain_text("Pay the electricity bill")
        expect(page.locator(".sp-card")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator(".sp-card")).to_have_count(0)
        assert door.posted("/api/panel/sessions/s1/policy/refuse") == [{"key": "abcdefabcdef"}], door.posts

        # Pinned, the panel stays where it is when the tab moves.
        page.get_by_role("button", name="Stay on this conversation").click()
        page.evaluate("window.__shell.emit('current', " + json.dumps(tab("s2", "Flights", "https://air.example/")) + ")")
        page.wait_for_timeout(300)
        expect(page.locator(".sp-title")).to_contain_text("Pay the electricity bill")
        page.get_by_role("button", name="Follow the active tab").click()
        expect(page.locator(".sp-title")).to_contain_text("Find flights to Lisbon")
        page.evaluate("window.__shell.emit('current', " + json.dumps(tab("s1", "Your bill", "https://bank.example/bill")) + ")")
        expect(page.locator(".sp-title")).to_contain_text("Pay the electricity bill")

        # This page, attached as a file.
        page.get_by_role("button", name="This page").click()
        expect(page.locator(".composer")).to_contain_text("page.txt")
        assert requests(page, "page.text"), "the page's text was never asked for"

        # The approval mode, from the composer's chip; the composer's row fits a narrow panel with it.
        chip = page.locator(".composer .approvals-chip")
        expect(chip).to_have_attribute("data-mode", "manual")
        chip.click()
        expect(page.locator(".approvals-menu [role=menuitemradio]")).to_have_count(3)
        page.locator('.approvals-menu [data-mode="auto"]').click()
        expect(chip).to_have_attribute("data-mode", "auto")
        assert door.posted("/api/panel/sessions/s1/browser-approvals") == [{"mode": "auto"}], door.posts
        for width in (360, 420):
            page.set_viewport_size({"width": width, "height": 760})
            page.wait_for_timeout(50)
            spill = page.evaluate("(() => { const row = document.querySelector('.composer-row'); return row.scrollWidth - row.clientWidth; })()")
            assert spill <= 0, f"the composer's row spills by {spill}px at {width} px"
        page.set_viewport_size({"width": 400, "height": 760})

        # A running conversation: the strip, Stop, the unconfirmed stop, Take over.
        door.details["s1"]["status"] = "running"
        door.details["s1"]["run_id"] = "r1"
        page.evaluate("window.__shell.emit('current', " + json.dumps(tab("s2", "Flights", "https://air.example/")) + ")")
        expect(page.locator(".sp-title")).to_contain_text("Find flights to Lisbon")
        page.evaluate("window.__shell.emit('current', " + json.dumps(tab("s1", "Your bill", "https://bank.example/bill")) + ")")
        line = page.locator(".sp-live")
        expect(line).to_be_visible()
        # One Stop only: the composer's round button. The line carries the tab buttons as icons.
        assert line.get_by_role("button", name="Stop").count() == 0, "the activity line has a Stop of its own beside the composer's"
        expect(line.get_by_role("button", name="Take over")).to_be_visible()
        # The field holds the attached page, so the composer's Stop is the small one beside Queue.
        page.locator(".composer").get_by_role("button", name="Stop the run").first.click()
        assert door.posted("/api/panel/sessions/s1/stop"), door.posts
        expect(page.locator(".sp-live-warn")).to_contain_text("did not reach Daedalus", timeout=6000)
        page.screenshot(path=str(SHOTS / "side-panel-running.png"))
        # The line stays one row across the widths a side panel takes, and sits inside the composer.
        for width in (360, 420):
            page.set_viewport_size({"width": width, "height": 760})
            page.wait_for_timeout(100)
            rows = page.evaluate("""() => {
              const line = document.querySelector('.sp-live');
              const tops = new Set([...line.children].map((el) => Math.round(el.getBoundingClientRect().top)));
              return { tops: tops.size, height: line.getBoundingClientRect().height, inside: !!line.closest('.composer') };
            }""")
            assert rows["tops"] == 1 and rows["height"] <= 32 and rows["inside"], (width, rows)
            page.screenshot(path=str(SHOTS / f"side-panel-running-{width}.png"))
        page.set_viewport_size({"width": 400, "height": 760})
        line.get_by_role("button", name="Take over").click()
        expect(page.locator(".sp-live")).to_contain_text("Paused")
        # The note it leaves sits under the header and never over the composer or its controls.
        expect(page.locator(".sp-toast")).to_be_visible()
        for width in (360, 420):
            page.set_viewport_size({"width": width, "height": 760})
            page.wait_for_timeout(50)
            overlap = page.evaluate("""() => {
              const c = document.querySelector('.composer').getBoundingClientRect();
              return [...document.querySelectorAll('.sp-toast')].some((el) => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.left < c.right && r.right > c.left && r.top < c.bottom && r.bottom > c.top;
              });
            }""")
            assert page.locator(".sp-toast").count() and not overlap, f"a note covers the composer at {width} px"
            page.screenshot(path=str(SHOTS / f"side-panel-note-{width}.png"))
        page.locator(".sp-toast").first.click()
        expect(page.locator(".sp-toast")).to_have_count(0)
        page.set_viewport_size({"width": 400, "height": 760})
        assert requests(page, "group.pause")[-1]["args"] == {"session": "s1", "paused": True}
        assert requests(page, "tab.focus")[-1]["args"] == {"session": "s1"}

        # No sideways scroll at a narrow panel.
        page.set_viewport_size({"width": 360, "height": 760})
        wide = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert wide <= 0, f"the panel scrolls sideways by {wide}px at 360 px"
        assert not door.foreign, door.foreign
        assert door.auth == {"Panel pnl_check"}, door.auth
        ctx.close()

        # A new conversation, the first one: the consent once, the tab given before the first message.
        door = Door()
        door.consented = False
        door.sessions = []
        ctx, page = open_panel(browser, door, current=tab(None, "Gift ideas", "https://shop.example/gifts"))
        expect(page.locator(".sp-consent")).to_contain_text("signed in as you")
        expect(page.locator(".sp-new-tab")).to_contain_text("Gift ideas")
        page.screenshot(path=str(SHOTS / "side-panel-new.png"))
        # The app's whole composer, every control of it on one row at a narrow panel.
        new = page.locator(".sp-new .composer")
        for selector in (".iconbtn.plus", ".mic", ".model-select", ".approvals-chip"):
            expect(new.locator(selector)).to_be_visible()
        expect(new.locator(".sp-attach .chip")).to_have_count(3)
        for width in (360, 420):
            page.set_viewport_size({"width": width, "height": 760})
            page.wait_for_timeout(50)
            fit = page.evaluate("""() => {
              const row = document.querySelector('.sp-new .composer-row');
              const box = row.getBoundingClientRect();
              const out = [...row.querySelectorAll('button')].filter((b) => { const r = b.getBoundingClientRect(); return r.width > 0 && (r.right > box.right + 0.5 || r.left < box.left - 0.5); });
              return { spill: row.scrollWidth - row.clientWidth, out: out.length, page: document.documentElement.scrollWidth - document.documentElement.clientWidth };
            }""")
            assert fit["spill"] <= 0 and fit["out"] == 0 and fit["page"] <= 0, (width, fit)
            page.screenshot(path=str(SHOTS / f"side-panel-new-{width}.png"))
        page.set_viewport_size({"width": 400, "height": 760})
        field = new.locator("textarea")
        field.fill("Find a birthday gift under 50 euros")
        field.press("Enter")
        expect(page.locator(".sp-title")).to_contain_text("Find a birthday gift")
        assert door.posted("/api/panel/sessions") == [{"title": "Find a birthday gift under 50 euros", "consent": True}], door.posts
        grant = requests(page, "tab.grant")
        assert grant and grant[0]["args"]["group"] == "s-s9-u" and grant[0]["args"]["session"] == "s9", grant
        order = [path for path, _ in door.posts]
        assert order.index("/api/panel/sessions/s9/tab") < order.index("/api/panel/sessions/s9/messages"), order
        sent = door.posted("/api/panel/sessions/s9/messages")[0]
        assert "Find a birthday gift under 50 euros" in sent["text"] and "https://shop.example/gifts" in sent["text"], sent  # type: ignore[index]
        assert not door.foreign, door.foreign
        ctx.close()

        # In Russian, as the extension says.
        door = Door()
        ctx, page = open_panel(browser, door, lang="ru")
        expect(page.locator(".sp-card")).to_contain_text("Разрешить один раз")
        expect(page.locator(".sp-attach")).to_contain_text("Эта страница")
        page.screenshot(path=str(SHOTS / "side-panel-approval-ru.png"))
        ctx.close()
        browser.close()
    print(f"side panel: ok (pictures in {SHOTS})")
    return 0


if __name__ == "__main__":
    started = time.monotonic()
    code = run()
    sys.exit(code)
