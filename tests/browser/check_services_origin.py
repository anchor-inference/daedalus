"""A shared service's page, in a real browser, signed in as the operator, against the real API.

The unit tests send the headers a browser would send; this is the browser. The page below runs in
Chromium with the operator's session cookie in the jar, on the two places a shared page can live, and
tries what an agent-written page would try. It serves itself: the API, the door and a fake service, on
free loopback ports, so it needs no build and no `APP_URL`.

    python3 tests/browser/check_services_origin.py

The door is put on the same host name as the app on purpose. That is the hard case and the one a bare
address and a laptop are in: cookies ignore ports and the two are the same site, so the browser does
attach the session cookie, and only the check on the API's side stands in the way.

Exit 0 when every claim holds.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # this checkout, whichever Python runs the script

import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from daedalus.config import RuntimeConfig, Settings  # noqa: E402
from daedalus.extensions.api import SESSION_COOKIE, build_app, server_config, session_cookie_value  # noqa: E402
from daedalus.extensions.api_services_door import ServicesDoor  # noqa: E402

ATTACK = """<!doctype html><title>start</title>
<script type="module" src="module.js"></script>
<script>
(async () => {
  const out = { origin: self.origin };
  const attempt = async (name, step) => { try { out[name] = await step(); } catch (e) { out[name] = "threw " + e.name; } };
  await attempt("read_api", async () => { const r = await fetch(API + "/api/auth/me", { credentials: "include" }); return r.status + " " + (await r.text()); });
  await attempt("read_own_address_api", async () => { const r = await fetch("/api/auth/me", { credentials: "include" }); return r.status; });
  await attempt("act_on_api", async () => { await fetch(API + "/api/auth/sessions/revoke", { method: "POST", credentials: "include", mode: "no-cors" }); return "sent"; });
  await attempt("own_backend", async () => { const r = await fetch("data.json"); return r.status + " " + (await r.text()); });
  await attempt("own_backend_json_post", async () => { const r = await fetch("data.json", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" }); return r.status + " " + (await r.text()); });
  await attempt("storage", async () => { localStorage.setItem("a", "1"); return localStorage.getItem("a"); });
  await attempt("cookie", async () => { document.cookie = "theirs=1"; return document.cookie; });
  for (let i = 0; i < 100 && !window.moduleRan; i++) await new Promise((done) => setTimeout(done, 10));
  out.module_script = !!window.moduleRan;
  window.outcome = out;
  document.title = "done";
  if (location.search.includes("form")) {
    const form = document.createElement("form");
    form.method = "POST"; form.action = API + "/api/auth/sessions/revoke";
    document.body.append(form);
    form.submit();
  }
})();
</script>"""

HEARD: list[tuple[str, str, str]] = []
API_ADDRESS: dict[str, str] = {}


class Service(BaseHTTPRequestHandler):
    """The server an agent wrote."""

    def answer(self) -> None:
        HEARD.append((self.command, self.path, self.headers.get("cookie") or ""))
        body, kind = b'{"own":true}', "application/json"
        if "module.js" in self.path:
            body, kind = b"window.moduleRan = true;", "text/javascript"
        elif "page" in self.path:
            body, kind = f"<script>const API = {json.dumps(API_ADDRESS['value'])};</script>{ATTACK}".encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src * 'unsafe-inline' 'unsafe-eval'")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = answer  # noqa: N815

    def log_message(self, *args: Any) -> None:
        pass


class Store:
    def __init__(self) -> None:
        self.kv = {"session_secret": "seed"}

    async def kv_get(self, key: str, default: Any = None) -> Any:
        return self.kv.get(key, default)

    async def kv_set(self, key: str, value: Any) -> None:
        self.kv[key] = value


class Shares:
    def __init__(self, port: int) -> None:
        base = {"status": "running", "pid": os.getpid(), "port": port}
        self.rows = {"demo": {**base, "share_mode": "public", "share_key": None}, "vault": {**base, "share_mode": "key", "share_key": "s3cret"}}

    async def by_slug(self, slug: str) -> dict[str, Any] | None:
        return self.rows.get(slug)

    def share_allows(self, row: dict[str, Any], key: str | None) -> bool:
        return row["share_mode"] == "public" or key == row["share_key"]


class Sessions:
    async def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return []


class Installation:
    def __init__(self, service_port: int, door_port: int, *, door: bool) -> None:
        self.settings = Settings(_env_file=None, telegram_bot_token="123:abc", owner_user_id=1, services_public_host="127.0.0.1" if door else "", services_door_port=door_port, miniapp_public_url="")  # type: ignore[call-arg]
        self.config = RuntimeConfig()
        self.manager = Sessions()
        self.front: Any = None
        self.db = Store()
        self.extensions: dict[str, Any] = {"services": Shares(service_port)}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def serve(application: Any, port: int) -> None:
    server = uvicorn.Server(server_config(application, host="127.0.0.1", port=port, lifespan="off"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)


def stand_up(service_port: int, *, door: bool) -> tuple[Installation, str]:
    api_port, door_port = free_port(), free_port()
    installation = Installation(service_port, door_port, door=door)
    api = build_app(installation, "tok")  # type: ignore[arg-type]
    serve(api, api_port)
    if door:
        serve(ServicesDoor(api), door_port)
    address = f"http://127.0.0.1:{api_port}"
    API_ADDRESS["value"] = address
    return installation, address


def visit(browser: Any, installation: Installation, url: str, *, form: bool = False) -> tuple[dict[str, Any], Any, Any]:
    secret = hashlib.sha256(f"session:tok:{installation.db.kv['session_secret']}".encode()).digest()
    context = browser.new_context()
    context.add_cookies([{"name": SESSION_COOKIE, "value": session_cookie_value(secret, 1), "domain": "127.0.0.1", "path": "/", "httpOnly": True, "sameSite": "Lax"}])
    page = context.new_page()
    HEARD.clear()
    page.goto(url, wait_until="commit")
    if form:  # the page leaves by itself, so there is nothing to read back from it
        page.wait_for_url(lambda now: "/api/auth/sessions/revoke" in now, timeout=10000)
        return {}, page, context
    page.wait_for_function("window.outcome !== undefined", timeout=10000)
    return page.evaluate("window.outcome"), page, context


def check(name: str, holds: bool, problems: list[str], detail: Any = "") -> None:
    print(f"{'ok  ' if holds else 'FAIL'} {name}" + (f": {detail}" if not holds else ""))
    if not holds:
        problems.append(name)


def session_cookie_reached_the_service() -> bool:
    return any(SESSION_COOKIE in cookie for _, _, cookie in HEARD)


def main() -> int:
    problems: list[str] = []
    service = ThreadingHTTPServer(("127.0.0.1", 0), Service)
    threading.Thread(target=service.serve_forever, daemon=True).start()
    chromium = os.environ.get("CHROMIUM")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**({"executable_path": chromium} if chromium else {}))

        print("-- at the door: another port of the same host, the same site as the app")
        installation, app = stand_up(service.server_port, door=True)
        seed = installation.db.kv["session_secret"]
        outcome, page, context = visit(browser, installation, f"{app}/s/demo/page")
        check("a link to the app's address arrives at the door", not page.url.startswith(app), problems, page.url)
        check("the page has an origin of its own", outcome["origin"] not in ("null", app), problems, outcome)
        check("the API cannot be read", outcome["read_api"].startswith("threw"), problems, outcome)
        check("the door has no API on it", outcome["read_own_address_api"] == 404, problems, outcome)
        check("a POST with the operator's cookie is refused", installation.db.kv["session_secret"] == seed, problems)
        check("the service never sees the session cookie", not session_cookie_reached_the_service(), problems, HEARD)
        check("a real app works: its backend", outcome["own_backend"].startswith("200") and outcome["own_backend_json_post"].startswith("200"), problems, outcome)
        check("a real app works: local storage", outcome["storage"] == "1", problems, outcome)
        check("a real app works: its cookies", "theirs=1" in outcome["cookie"], problems, outcome)
        check("a real app works: module scripts", outcome["module_script"] is True, problems, outcome)
        context.close()

        outcome, page, context = visit(browser, installation, f"{app}/s/demo/page?form", form=True)
        check("a form posted to the API is refused", installation.db.kv["session_secret"] == seed and "did not come from the app" in page.content(), problems, page.content()[:200])
        context.close()

        outcome, page, context = visit(browser, installation, f"{app}/s/vault/page?key=s3cret")
        check("a key's link arrives at the door without the key in the address", "s3cret" not in page.url and outcome["own_backend"].startswith("200"), problems, (page.url, outcome))
        context.close()

        print("-- the app itself")
        context = browser.new_context()
        secret = hashlib.sha256(f"session:tok:{seed}".encode()).digest()
        context.add_cookies([{"name": SESSION_COOKIE, "value": session_cookie_value(secret, 1), "domain": "127.0.0.1", "path": "/", "httpOnly": True, "sameSite": "Lax"}])
        page = context.new_page()
        page.goto(f"{app}/robots.txt")
        status = page.evaluate("fetch('/api/auth/sessions/revoke', { method: 'POST' }).then((r) => r.status)")
        check("the app's own POST still goes through", status == 200 and installation.db.kv["session_secret"] != seed, problems, status)
        context.close()

        print("-- in the sandbox: no door to use, the page stays on the app's own address")
        installation, app = stand_up(service.server_port, door=False)
        seed = installation.db.kv["session_secret"]
        outcome, page, context = visit(browser, installation, f"{app}/s/demo/page")
        check("the page stays on the app's address", page.url.startswith(app), problems, page.url)
        check("the page has no origin", outcome["origin"] == "null", problems, outcome)
        check("the API cannot be read", outcome["read_api"].startswith("threw") and str(outcome["read_own_address_api"]).startswith("threw"), problems, outcome)
        check("a POST carries no cookie and does nothing", installation.db.kv["session_secret"] == seed, problems)
        check("the service never sees the session cookie", not session_cookie_reached_the_service(), problems, HEARD)
        check("a bundled app still loads: module scripts", outcome["module_script"] is True, problems, outcome)
        check("its backend still answers", outcome["own_backend"].startswith("200") and outcome["own_backend_json_post"].startswith("200"), problems, outcome)
        check("local storage is the known price", outcome["storage"].startswith("threw"), problems, outcome)
        context.close()

        outcome, page, context = visit(browser, installation, f"{app}/s/demo/page?form", form=True)
        check("a form posted to the API carries no cookie", installation.db.kv["session_secret"] == seed, problems, page.content()[:200])
        context.close()

        outcome, page, context = visit(browser, installation, f"{app}/s/vault/page?key=s3cret")
        check("a key's page gets its scripts and its backend", outcome["module_script"] is True and outcome["own_backend"].startswith("200"), problems, outcome)
        context.close()
        browser.close()
    if problems:
        print(f"\n{len(problems)} claim(s) failed")
        return 1
    print("\nevery claim holds")
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)  # the servers run on daemon threads with streams nobody will close
