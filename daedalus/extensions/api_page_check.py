"""A custom page looked at before the operator sees it: served on a short-lived address of its own and
loaded in the agent's browser at a phone's size and a desktop's, in the dark scheme and the light one.

Why a server of its own rather than the page's real address: the real one is behind the operator's
sign-in on the API's port, and that port is sealed from the agent's browser, because whatever holds
the session cookie holds the whole API. A page is the operator's and may call any route; a check is
the agent's and runs the agent's draft, so letting the check carry the cookie would hand the agent
everything the seal keeps from it. The check's server holds no credential at all: it serves the
page's files, the kit, and a frame that plays the app's side of the bridge, and it answers every
``/api/`` request with 401, which the result reports as what it is.

The server listens on a free port of the services range, the one range the browser's network wall
lets the agent's browser reach, for as long as one check lasts, and it answers only a browser that
first opened the address carrying its random token. It is an ``api_`` module because it serves HTTP.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import socket
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.websockets import WebSocket

from daedalus.extensions.api_pages import FRAME_POLICY, content_type, link_page, static_file
from daedalus.extensions.api_proxy import forward_http, forward_websocket
from daedalus.host.pages import PageError, PageStore, Served, title_text

CHECK_COOKIE = "daedalus_page_check"
VIEWS: tuple[tuple[str, int, int], ...] = (("phone", 390, 844), ("desktop", 1440, 900))
"""The sizes a page is looked at: a common phone, and a desktop window wide enough for the rail."""
SCHEMES: tuple[str, ...] = ("dark", "light")
"""The app's default theme, and the light scheme a page most often forgets."""
REPORT_SECONDS = 25.0
"""How long one look waits for the frame to say what it measured; past it the screenshot is taken
anyway and the result says the page never finished loading."""
SETTLE_MS = 700
"""After the page has loaded and shaken hands, a moment for fonts, a CDN's script and a first render."""


class _QuietServer(uvicorn.Server):
    """uvicorn without the signal handlers it installs while it serves: a check's server is one of
    two in this process, and taking SIGTERM for itself would leave the application's own server and
    the rest of the bot running through a stop that only ended a page check."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def bind_free(lo: int, hi: int, host: str, taken: set[int]) -> socket.socket:
    """A listening socket on the first free port of ``lo``–``hi`` that no service holds.

    The socket is made here and handed to uvicorn, rather than uvicorn being given a port: uvicorn
    answers a port it cannot bind with ``sys.exit``, which inside a task of this process would stop
    the whole event loop, and the services allocator's own probe closes its socket before anyone
    binds, so another service could take the port in between."""
    for port in range(lo, hi + 1):
        if port in taken:
            continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            sock.listen(64)
        except OSError:
            sock.close()
            continue
        sock.setblocking(False)
        return sock
    raise PageError(f"no free port in the services range {lo}-{hi} for the check; stop a service and check again", 503)


@dataclass(slots=True)
class Exchange:
    """One request the checked page made to its own address, and what the check's server answered."""

    method: str
    path: str
    status: int


@dataclass(slots=True)
class Shot:
    name: str
    view: str
    scheme: str
    width: int
    height: int
    data: bytes
    mime: str


@dataclass(slots=True)
class CheckResult:
    slug: str
    kind: str
    draft: bool
    runs: list[dict[str, Any]] = field(default_factory=list)
    shots: list[Shot] = field(default_factory=list)
    console: list[dict[str, Any]] | None = None
    failed: list[dict[str, Any]] | None = None
    exchanges: list[Exchange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


HARNESS = """<!DOCTYPE html>
<html lang="en" data-scheme="__SCHEME__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<link rel="icon" href="data:,">
<title>Page check</title>
<link rel="stylesheet" href="/pages/_kit/kit.css">
<style>html,body{margin:0;height:100%;overflow:hidden;background:var(--bg,#151515)}iframe{border:0;display:block;width:100%;height:100%}</style>
</head>
<body>
<iframe id="page" title="page" allow="clipboard-read; clipboard-write; fullscreen; geolocation; camera; microphone; autoplay; xr-spatial-tracking"></iframe>
<script>
(function () {
  "use strict";
  var config = __CONFIG__;
  var frame = document.getElementById("page");
  var started = Date.now();
  var report = { run: config.run, scheme: config.scheme, handshake: null, calls: [], loaded: false, load_ms: null };
  var names = [];
  var sent = false;

  // The app's side of the bridge, as miniapp/src/pagebridge.ts answers it: the theme as computed
  // values of every token, from this frame's own root, which carries the scheme being checked.
  function snapshot() {
    var style = getComputedStyle(document.documentElement);
    var tokens = {};
    names.forEach(function (name) {
      var value = style.getPropertyValue(name).trim();
      if (value) tokens[name] = value;
    });
    return { tokens: tokens, scheme: config.scheme, theme: config.scheme === "light" ? "light" : "", lang: "en", phone: window.innerWidth < 1024 };
  }

  window.addEventListener("message", function (e) {
    if (e.source !== frame.contentWindow || e.origin !== location.origin) return;
    var d = e.data;
    if (!d || d.daedalus !== 1 || typeof d.id !== "number" || typeof d.method !== "string") return;
    var p = d.params || {};
    report.calls.push({ method: d.method, at: Date.now() - started });
    var reply;
    if (d.method === "ready" || d.method === "theme") {
      if (!report.handshake) report.handshake = { version: typeof p.version === "string" ? p.version : null, at: Date.now() - started };
      reply = { result: { theme: snapshot(), protocol: 1 } };
    } else if (d.method === "toast" || d.method === "navigate" || d.method === "openFile") {
      reply = { result: true };
    } else {
      reply = { error: "unknown method " + d.method };
    }
    frame.contentWindow.postMessage(Object.assign({ daedalus: 1, id: d.id }, reply), location.origin);
  });

  function describe(el) {
    var text = el.tagName.toLowerCase();
    if (el.id) text += "#" + el.id;
    var classes = (typeof el.className === "string" ? el.className : "").trim().split(/\\s+/).filter(Boolean).slice(0, 2);
    if (classes.length) text += "." + classes.join(".");
    return text;
  }

  // An element past the edge inside a box that scrolls or clips, and that box itself fits, is a
  // table or a carousel doing its job, not the page overflowing.
  function contained(win, el, limit, body) {
    for (var a = el.parentElement; a && a !== body; a = a.parentElement) {
      if (win.getComputedStyle(a).overflowX !== "visible") return a.getBoundingClientRect().right <= limit + 1;
    }
    return false;
  }

  function measure() {
    var doc, win;
    try {
      doc = frame.contentDocument;
      win = frame.contentWindow;
    } catch (error) {
      return { reachable: false };
    }
    if (!doc || !doc.documentElement || !win) return { reachable: false };
    var root = doc.documentElement;
    var body = doc.body;
    var width = root.clientWidth;
    var out = {
      reachable: true,
      title: doc.title || "",
      width: width,
      scroll_width: Math.max(root.scrollWidth, body ? body.scrollWidth : 0),
      viewport_meta: !!doc.querySelector('meta[name="viewport"]'),
      bridge: (win.Daedalus && win.Daedalus.version) || null,
      kit: !!doc.querySelector('link[href*="/pages/_kit/kit.css"]'),
      elements: doc.getElementsByTagName("*").length,
      text_chars: body ? (body.innerText || "").trim().length : 0,
      wide: [],
      small: 0,
      small_examples: []
    };
    var all = body ? body.getElementsByTagName("*") : [];
    var count = Math.min(all.length, 4000);
    for (var i = 0; i < count; i++) {
      var el = all[i];
      var r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      if (r.right > width + 1 && out.wide.length < 8) {
        var parent = el.parentElement;
        var outer = parent ? parent.getBoundingClientRect() : null;
        if ((!outer || outer.right <= width + 1) && !contained(win, el, width, body)) {
          out.wide.push({ el: describe(el), right: Math.round(r.right), width: Math.round(r.width) });
        }
      }
      var tag = el.tagName;
      if (/^(A|BUTTON|SELECT|INPUT|TEXTAREA)$/.test(tag) || el.getAttribute("role") === "button") {
        if (el.type === "hidden") continue;
        var cs = win.getComputedStyle(el);
        if (cs.visibility === "hidden" || cs.display === "none" || cs.opacity === "0" || cs.pointerEvents === "none") continue;
        // A link inside a sentence is sized by its text and exempt, as the accessibility guidelines say;
        // a box or a radio inside its label is pressed through the label, which is the target.
        if (tag === "A" && cs.display === "inline") continue;
        if ((el.type === "checkbox" || el.type === "radio") && el.closest("label")) continue;
        if (r.width < 24 || r.height < 24) {
          out.small++;
          if (out.small_examples.length < 5) out.small_examples.push(describe(el) + " " + Math.round(r.width) + "x" + Math.round(r.height));
        }
      }
    }
    return out;
  }

  function finish() {
    if (sent) return;
    sent = true;
    report.measure = measure();
    fetch("/__report?run=" + config.run, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(report) });
  }

  frame.addEventListener("load", function () {
    report.loaded = true;
    report.load_ms = Date.now() - started;
    var deadline = Date.now() + config.handshake_ms;
    (function waiting() {
      if (report.handshake || Date.now() >= deadline) setTimeout(finish, config.settle_ms);
      else setTimeout(waiting, 100);
    })();
  });
  setTimeout(finish, config.give_up_ms);

  fetch("/pages/_kit/kit.css")
    .then(function (r) { return r.ok ? r.text() : ""; })
    .then(function (text) {
      var seen = {};
      (text.match(/--[a-z0-9-]+(?=\\s*:)/g) || []).forEach(function (name) { seen[name] = true; });
      names = Object.keys(seen);
    })
    .catch(function () {})
    .then(function () { frame.src = config.src; });
})();
</script>
</body>
</html>
"""


class CheckServer:
    """The address one check is served at, for as long as the check lasts.

    ``resolve_port`` answers a service page's port; it refuses one outside the services range, so a
    check can never be pointed at the installation's own doors through a page's target.
    """

    def __init__(self, store: PageStore, manifest: dict[str, Any], *, draft: bool, kit_dir: Path,
                 resolve_port: Callable[[dict[str, Any], Served], Awaitable[int]]) -> None:
        served = store.served(manifest, draft=draft)
        if served is None:
            raise PageError(f"the page {manifest['slug']!r} has nothing published yet; check its draft", 409)
        self.store = store
        self.manifest = manifest
        self.slug: str = manifest["slug"]
        self.served = served
        self.draft = draft
        self.kit_dir = kit_dir
        self.resolve_port = resolve_port
        self.token = secrets.token_urlsafe(24)
        self.reports: dict[int, dict[str, Any]] = {}
        self._arrived: dict[int, asyncio.Event] = {}
        self.exchanges: list[Exchange] = []
        self.port = 0
        self._server: _QuietServer | None = None
        self._task: asyncio.Task[None] | None = None

    # -- the address --------------------------------------------------------------------------

    def url(self, run: int, scheme: str) -> str:
        """What the browser opens for one look. ``127.0.0.1`` is this machine for a native browser,
        and the browser service rewrites it to the Docker host for a browser in its own container,
        where the services range is published."""
        return f"http://127.0.0.1:{self.port}/?t={self.token}&run={run}&scheme={scheme}"

    async def wait_report(self, run: int, timeout: float = REPORT_SECONDS) -> dict[str, Any] | None:
        event = self._arrived.setdefault(run, asyncio.Event())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout)
        return self.reports.get(run)

    def _allowed(self, request: Request | WebSocket) -> bool:
        return secrets.compare_digest(request.cookies.get(CHECK_COOKIE, ""), self.token)

    # -- the routes ---------------------------------------------------------------------------

    def build(self) -> FastAPI:
        api = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        slug = self.slug

        @api.middleware("http")
        async def record(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
            response = await call_next(request)
            if request.url.path not in ("/", "/__report"):
                self.exchanges.append(Exchange(request.method, request.url.path + (f"?{request.url.query}" if request.url.query else ""), response.status_code))
            return response

        @api.get("/")
        async def harness(request: Request) -> Response:
            if not secrets.compare_digest(request.query_params.get("t", ""), self.token):
                return Response(status_code=404)
            run = int(request.query_params.get("run") or 0)
            scheme = "light" if request.query_params.get("scheme") == "light" else "dark"
            config = {"run": run, "scheme": scheme, "src": f"/pages/{slug}/", "handshake_ms": 1500, "settle_ms": SETTLE_MS, "give_up_ms": int(REPORT_SECONDS * 1000) - 3000}
            body = HARNESS.replace("__SCHEME__", scheme).replace("__CONFIG__", json.dumps(config))
            response = HTMLResponse(body, headers={"Cache-Control": "no-store"})
            response.set_cookie(CHECK_COOKIE, self.token, path="/", httponly=True, samesite="strict")
            return response

        @api.post("/__report")
        async def reported(request: Request) -> Response:
            if not self._allowed(request):
                return Response(status_code=404)
            try:
                run = int(request.query_params.get("run") or 0)
                data = json.loads((await request.body())[:1_000_000] or b"{}")
            except ValueError:
                return Response(status_code=400)
            if isinstance(data, dict):
                self.reports[run] = data
                self._arrived.setdefault(run, asyncio.Event()).set()
            return Response(status_code=204)

        @api.get("/pages/_kit/{name}")
        async def kit(name: str) -> Response:
            path = self.kit_dir / name
            if name not in ("kit.css", "daedalus.js") or not path.is_file():
                return Response(status_code=404)
            return FileResponse(path, media_type=content_type(name), headers={"Cache-Control": "no-cache"})

        @api.api_route("/api/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
        async def no_api(rest: str) -> Response:
            return JSONResponse({"detail": "a page check runs without the operator's sign-in; in the app this request carries it"}, status_code=401)

        @api.get(f"/pages/{slug}")
        async def root() -> Response:
            return RedirectResponse(f"/pages/{slug}/")

        @api.api_route(f"/pages/{slug}/{{path:path}}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
        async def page(path: str, request: Request) -> Response:
            if not self._allowed(request):
                return Response(status_code=404)
            served = self.served
            if served.kind == "service":
                try:
                    port = await self.resolve_port(self.manifest, served)
                except PageError as exc:
                    return HTMLResponse(f"<p>{exc}</p>", status_code=503)
                upstream = f"http://127.0.0.1:{port}/{path}" + (f"?{request.url.query}" if request.url.query else "")
                return await forward_http(request, upstream, f"/pages/{slug}", strip_cookies=(CHECK_COOKIE,), page=True, read_timeout=60.0)
            if request.method not in ("GET", "HEAD"):
                return Response(status_code=405)
            headers = {"Content-Security-Policy": FRAME_POLICY, "Cache-Control": "no-store"}
            if served.kind == "link":
                return HTMLResponse(link_page(title_text(self.manifest["title"]), str(served.target)), headers=headers)
            return static_file(served, path, headers)

        @api.websocket(f"/pages/{slug}/{{path:path}}")
        async def page_socket(websocket: WebSocket, path: str) -> None:
            if not self._allowed(websocket) or self.served.kind != "service":
                await websocket.close(code=1008)
                return
            try:
                port = await self.resolve_port(self.manifest, self.served)
            except PageError:
                await websocket.close(code=1011)
                return
            query = websocket.url.query
            await forward_websocket(websocket, f"ws://127.0.0.1:{port}/{path}" + (f"?{query}" if query else ""), f"/pages/{slug}", strip_cookies=(CHECK_COOKIE,))

        @api.api_route("/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
        async def elsewhere(rest: str) -> Response:
            return Response(status_code=404)

        return api

    # -- the life of the server ---------------------------------------------------------------

    async def start(self, sock: socket.socket) -> None:
        self.port = int(sock.getsockname()[1])
        config = uvicorn.Config(self.build(), log_level="warning", access_log=False, lifespan="off")
        self._server = _QuietServer(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]), name=f"page-check-{self.slug}")
        for _ in range(500):
            if self._server.started:
                return
            if self._task.done():
                break
            await asyncio.sleep(0.01)
        await self.stop()
        sock.close()
        raise PageError("the page check's server did not start", 503)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, 5.0)
            if not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task


async def drive(browsers: Any, owner: Any, actor: str, server: CheckServer, result: CheckResult, *,
                views: tuple[tuple[str, int, int], ...] = VIEWS, schemes: tuple[str, ...] = SCHEMES) -> None:
    """Look at the page through the browser service: every view in every scheme, a screenshot of each,
    then the console and the failed requests of the whole visit.

    A throwaway browser of the session's is used, so the check sees no login of the operator's and
    leaves nothing in the agent's own profile; it is closed afterwards, unless the agent already had
    it open, in which case only the tab the check opened is closed."""
    opened = await browsers.open(owner, fresh=True, actor=actor, viewport={"w": views[0][1], "h": views[0][2]})
    group = str(opened["group"]["id"])
    created = bool(opened.get("created"))
    origin = browsers.origin(actor=actor)
    tab = str((opened.get("tab") or {}).get("id") or "")
    own_tab = False
    try:
        if not created or not tab:
            made = await browsers.call(group, "tab.new", {"group_id": group, "url": "about:blank", "origin": origin}, what="opening a tab for the page check", timeout=40.0)
            tab = str(made.get("id") or (made.get("tab") or {}).get("id") or "")
            own_tab = True
        run = 0
        for view, width, height in views:
            await browsers.resize(group, width, height)
            for scheme in schemes:
                run += 1
                navigated = await browsers.call(group, "page.navigate", {"tab_id": tab, "url": server.url(run, scheme), "origin": origin, "timeout_ms": 30000},
                                                what="opening the page for the check", timeout=55.0)
                report = await server.wait_report(run)
                result.runs.append({"name": f"{view}-{scheme}", "view": view, "scheme": scheme, "width": width, "height": height,
                                    "report": report, "navigation_error": (navigated or {}).get("error")})
                shot = await browsers.call(group, "page.screenshot", {"tab_id": tab, "full_page": False, "max_width": min(width, 1440), "format": "jpeg",
                                                                      "quality": 80, "origin": origin}, what="taking the check's screenshot", timeout=40.0)
                data = base64.b64decode((shot or {}).get("data_b64") or "")
                if data:
                    mime = "image/png" if (shot or {}).get("format") == "png" else "image/jpeg"
                    result.shots.append(Shot(f"{view}-{scheme}", view, scheme, int(shot.get("width") or width), int(shot.get("height") or height), data, mime))
        try:
            logs = await browsers.call(group, "page.logs", {"tab_id": tab, "after": 0, "limit": 100, "level": "warning", "origin": origin}, what="reading the page's console", timeout=20.0)
            result.console = [e for e in (logs or {}).get("entries") or [] if isinstance(e, dict)]
        except Exception as exc:  # noqa: BLE001 — an older browser service cannot read a console; the rest of the check stands
            result.notes.append(f"the console could not be read: {getattr(exc, 'message', exc)}")
        try:
            network = await browsers.call(group, "page.network", {"tab_id": tab, "after": 0, "limit": 200, "failed": True, "origin": origin}, what="listing the page's failed requests", timeout=20.0)
            result.failed = [r for r in (network or {}).get("requests") or [] if isinstance(r, dict)]
        except Exception as exc:  # noqa: BLE001 — as above, for the request log
            result.notes.append(f"the failed requests could not be listed: {getattr(exc, 'message', exc)}")
    finally:
        with contextlib.suppress(Exception):
            if created:
                await browsers.close_group(group, actor=actor, reason="closed")
            elif own_tab and tab:
                await browsers.call(group, "tab.close", {"tab_id": tab, "origin": origin}, what="closing the check's tab")


__all__ = ["CHECK_COOKIE", "SCHEMES", "VIEWS", "CheckResult", "CheckServer", "Exchange", "Shot", "bind_free", "drive"]
