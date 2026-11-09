"""Live pages on a canvas: the frame's signed address, the files it may read, and the walls around it.

A frame is a file node of a canvas whose file is an HTML page of the agent's workspace. It runs a page
the agent wrote, so it is held by two walls that do not depend on each other:

- it is served only by the services door, another origin than the app's (``api_services_door``),
  and the door strips every cookie; on the app's own origin the route does not exist;
- every answer carries a CSP ``sandbox`` without ``allow-same-origin``, and the board's iframe has the
  same sandbox, so the page has an opaque origin: no cookie, no storage of anything else, no popups,
  no navigation of the window around it.

The address carries a capability, an HMAC over the canvas, the frame, its generation, an expiry, the
app's origin (the only page allowed to frame it) and the origins the operator allowed for this one
load. It is checked against the canvas as it is now: a frame removed from the board, or shown again
by the agent (a new generation), answers nothing on its old address. The key is the canvas's own, in
the database, and signs nothing else.

What the page may load is the content security policy built for it: its own files and, from
elsewhere, only origins the operator allowed. A page that tries anything else is blocked by the
browser, and its bridge script tells the board which origin was refused, so the operator can allow it
"this time" (only this frame, only until the board is closed or the address expires; nothing is
stored), "always on this canvas" or "always on every canvas".

Its files are those under one folder, the root, by default the page's own folder, never the top of the
workspace: a page can read every file under its root and send it to an origin it was allowed, so the
root stays as tight as the page allows. No dot paths, no symbolic links, read only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from daedalus.stores.database import Database

KEY = "canvas_frame_key"
ADDRESS_HOURS = 12
MAX_FRAMES = 24
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ORIGINS = 40
DEVICES: dict[str, tuple[int, int]] = {"desktop": (1280, 800), "tablet": (820, 1180), "phone": (390, 844)}
WIDTH_RANGE, HEIGHT_RANGE = (320, 3840), (320, 4320)
MAX_SNAPSHOT_CHARS = 3_000_000
REPORT_LINES = 20
REPORT_CHARS = 1000
RESERVED = ".daedalus"
"""The folder name under a frame's address that the host answers itself (the bridge script); every
dot path is refused from disk, so no page file can take it."""
EVERY_CANVAS = "*"
"""The canvas column of an origin allowed on every canvas."""


class FrameError(ValueError):
    pass


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def origin_of(value: str) -> str:
    """An origin as a policy names it, ``https://host[:port]``, or :class:`FrameError`. Only https: a
    page on a plain http origin can be anybody on the network, and an allowance is for a CDN."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        raise FrameError(f"{value!r} is not an address") from None
    if parts.scheme != "https" or not parts.hostname or any(ch in parts.hostname for ch in "*'; "):
        raise FrameError(f"{value!r} is not an https origin")
    host = parts.hostname.lower()
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def app_origin_of(value: str) -> str:
    """The app's own origin, which may be plain http on a LAN or loopback."""
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FrameError("the app's origin is unknown")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def device_size(device: str | None, width: int | None, height: int | None) -> tuple[str, int, int]:
    if width or height:
        if not width or not height:
            raise FrameError("give both width and height, or a device")
        if not (WIDTH_RANGE[0] <= width <= WIDTH_RANGE[1] and HEIGHT_RANGE[0] <= height <= HEIGHT_RANGE[1]):
            raise FrameError(f"a frame is {WIDTH_RANGE[0]}..{WIDTH_RANGE[1]} wide and {HEIGHT_RANGE[0]}..{HEIGHT_RANGE[1]} tall")
        return "custom", int(width), int(height)
    name = device or "desktop"
    if name not in DEVICES:
        raise FrameError(f"device is one of {', '.join(DEVICES)}")
    return (name, *DEVICES[name])


def page_and_root(workspace: Path, page: Path, root: Path | None) -> tuple[Path, str]:
    """The root folder and the page's path inside it, both checked: the page an existing ``.html``
    file under the root, the root a folder inside the workspace but not the workspace itself, and no
    symbolic link or dot folder on the way."""
    workspace = workspace.resolve()
    if page.suffix.lower() not in (".html", ".htm"):
        raise FrameError("a frame shows an .html page")
    if not page.is_file() or page.is_symlink():
        raise FrameError(f"no page at {page}")
    resolved = page.resolve()
    chosen = (root or page.parent).resolve()
    if chosen == workspace or workspace not in chosen.parents:
        raise FrameError(
            "the frame's root must be a folder inside the workspace, not the workspace itself: a page can read every file "
            "under its root, so put the page in a folder of its own (for example site/index.html)"
        )
    if chosen not in resolved.parents:
        raise FrameError("the page must be inside the root")
    entry = resolved.relative_to(chosen).as_posix()
    if any(part.startswith(".") for part in entry.split("/")):
        raise FrameError("a frame serves no dot paths")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise FrameError("the page is larger than 10 MB")
    return chosen, entry


def served_file(root: Path, path: str) -> Path | None:
    """The file ``path`` names under ``root``, or None: no dot segment, no symbolic link anywhere on
    the way, nothing outside the root, a folder answers its ``index.html``."""
    parts = [part for part in path.split("/") if part not in ("",)]
    if any(part.startswith(".") or "\\" in part or "\x00" in part for part in parts):
        return None
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return None
    if current.is_dir():
        current = current / "index.html"
        if current.is_symlink():
            return None
    try:
        resolved = current.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if root.resolve() not in resolved.parents or not resolved.is_file():
        return None
    if resolved.stat().st_size > MAX_FILE_BYTES:
        return None
    return resolved


def policy(app_origin: str, allowed: list[str]) -> str:
    """The content security policy of a frame's every answer.

    ``'self'`` is the frame's own folder on the door; the allowed origins are added to the fetch
    directives and nothing else. Inline and evaluated script stay possible: the pages are the agent's
    own drafts, often a single file, and the wall is the origin and the sandbox, not the script source.
    """
    extra = " ".join(allowed)
    return "; ".join([
        "sandbox allow-scripts allow-forms allow-modals allow-downloads",
        f"default-src 'self' data: blob: 'unsafe-inline' 'unsafe-eval' {extra}".rstrip(),
        f"connect-src 'self' data: blob: {extra}".rstrip(),
        "form-action 'none'",
        "base-uri 'self'",
        "object-src 'none'",
        f"frame-ancestors {app_origin}",
    ])


@dataclass(frozen=True, slots=True)
class Capability:
    canvas_id: str
    node_id: str
    generation: int
    expires: int
    app_origin: str
    once: tuple[str, ...]
    """Origins allowed for this one address only."""

    def payload(self) -> bytes:
        return json.dumps([self.canvas_id, self.node_id, self.generation, self.expires, self.app_origin, list(self.once)], separators=(",", ":")).encode()


@dataclass
class Report:
    """What the board relayed of a frame's own words: untrusted text, kept in memory, capped."""

    at: float = 0.0
    loaded: bool = False
    title: str = ""
    lines: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=REPORT_LINES))
    blocked: list[str] = field(default_factory=list)


class Frames:
    """The frames of all canvases: their rows, their signed addresses, and the reports about them."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._key: bytes | None = None
        self.reports: dict[tuple[str, str], Report] = {}

    async def key(self) -> bytes:
        if self._key is None:
            stored = await self.db.kv_get(KEY)
            if not stored:
                stored = b64(secrets.token_bytes(32))
                await self.db.kv_set(KEY, stored)
            self._key = unb64(str(stored))
        return self._key

    # -- addresses -------------------------------------------------------------------------------------

    async def sign(self, cap: Capability) -> str:
        payload = cap.payload()
        mac = hmac.new(await self.key(), payload, hashlib.sha256).digest()
        return f"{b64(payload)}.{b64(mac)}"

    async def verify(self, token: str, now: float | None = None) -> Capability | None:
        encoded, _, mac_text = token.partition(".")
        try:
            payload, mac = unb64(encoded), unb64(mac_text)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(hmac.new(await self.key(), payload, hashlib.sha256).digest(), mac):
            return None
        try:
            canvas_id, node_id, generation, expires, app, once = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if expires < (time.time() if now is None else now):
            return None
        return Capability(str(canvas_id), str(node_id), int(generation), int(expires), str(app), tuple(str(o) for o in once))

    # -- rows ------------------------------------------------------------------------------------------

    async def get(self, canvas_id: str, node_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT canvas_id,node_id,root,entry,generation,updated_at FROM canvas_frames WHERE canvas_id=? AND node_id=?", (canvas_id, node_id))
        return dict(row) if row else None

    async def put(self, canvas_id: str, node_id: str, root: Path, entry: str) -> int:
        """Record the frame's files; showing it again is a new generation, which retires every address
        handed out before (the board fetches a new one when it sees the canvas change)."""
        count = await self.db.fetchone("SELECT COUNT(*) AS n FROM canvas_frames WHERE canvas_id=? AND node_id!=?", (canvas_id, node_id))
        if count is not None and count["n"] >= MAX_FRAMES:
            raise FrameError(f"a canvas holds at most {MAX_FRAMES} live frames; remove one first")
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO canvas_frames(canvas_id,node_id,root,entry,generation,created_at,updated_at) VALUES(?,?,?,?,1,?,?)"
                " ON CONFLICT(canvas_id,node_id) DO UPDATE SET root=excluded.root,entry=excluded.entry,generation=generation+1,updated_at=excluded.updated_at",
                (canvas_id, node_id, str(root), entry, at, at),
            )
            row = await (await conn.execute("SELECT generation FROM canvas_frames WHERE canvas_id=? AND node_id=?", (canvas_id, node_id))).fetchone()
        self.reports.pop((canvas_id, node_id), None)
        return int(row["generation"])

    async def revoke(self, canvas_id: str, node_id: str) -> None:
        """Retire the frame's addresses without moving it: the page navigated somewhere it should not."""
        await self.db.execute("UPDATE canvas_frames SET generation=generation+1 WHERE canvas_id=? AND node_id=?", (canvas_id, node_id))

    async def drop_missing(self, canvas_id: str, node_ids: set[str]) -> None:
        """Forget the frames whose nodes left the board."""
        rows = await self.db.fetchall("SELECT node_id FROM canvas_frames WHERE canvas_id=?", (canvas_id,))
        for row in rows:
            if row["node_id"] not in node_ids:
                await self.db.execute("DELETE FROM canvas_frames WHERE canvas_id=? AND node_id=?", (canvas_id, row["node_id"]))
                await self.db.execute("DELETE FROM canvas_snapshots WHERE canvas_id=? AND node_id=?", (canvas_id, row["node_id"]))
                self.reports.pop((canvas_id, row["node_id"]), None)

    # -- snapshots -------------------------------------------------------------------------------------

    async def save_snapshot(self, canvas_id: str, node_id: str, html: str, width: int, height: int) -> None:
        """Keep the frame's last static picture: its document as the page had drawn it, scripts and
        handlers removed, pictures and styles inlined by the bridge. Only the shared view reads it."""
        if len(html) > MAX_SNAPSHOT_CHARS:
            raise FrameError("the snapshot is larger than 3 MB")
        await self.db.execute(
            "INSERT INTO canvas_snapshots(canvas_id,node_id,html,width,height,captured_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(canvas_id,node_id) DO UPDATE SET html=excluded.html,width=excluded.width,height=excluded.height,captured_at=excluded.captured_at",
            (canvas_id, node_id, html, max(1, int(width)), max(1, int(height)), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )

    async def snapshot(self, canvas_id: str, node_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT html,width,height,captured_at FROM canvas_snapshots WHERE canvas_id=? AND node_id=?", (canvas_id, node_id))
        return dict(row) if row else None

    async def snapshot_times(self, canvas_id: str) -> dict[str, str]:
        rows = await self.db.fetchall("SELECT node_id,captured_at FROM canvas_snapshots WHERE canvas_id=?", (canvas_id,))
        return {row["node_id"]: row["captured_at"] for row in rows}

    # -- allowed origins -------------------------------------------------------------------------------

    async def origins(self, canvas_id: str) -> dict[str, list[str]]:
        rows = await self.db.fetchall("SELECT canvas_id,origin FROM canvas_origins WHERE canvas_id IN (?,?) ORDER BY origin", (canvas_id, EVERY_CANVAS))
        return {
            "canvas": [row["origin"] for row in rows if row["canvas_id"] == canvas_id],
            "all": [row["origin"] for row in rows if row["canvas_id"] == EVERY_CANVAS],
        }

    async def allow(self, canvas_id: str, origin: str, *, everywhere: bool) -> None:
        origin = origin_of(origin)
        target = EVERY_CANVAS if everywhere else canvas_id
        count = await self.db.fetchone("SELECT COUNT(*) AS n FROM canvas_origins WHERE canvas_id=?", (target,))
        if count is not None and count["n"] >= MAX_ORIGINS:
            raise FrameError(f"at most {MAX_ORIGINS} allowed origins")
        await self.db.execute(
            "INSERT OR IGNORE INTO canvas_origins(canvas_id,origin,created_at) VALUES(?,?,?)",
            (target, origin, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )

    async def disallow(self, canvas_id: str, origin: str, *, everywhere: bool) -> None:
        await self.db.execute("DELETE FROM canvas_origins WHERE canvas_id=? AND origin=?", (EVERY_CANVAS if everywhere else canvas_id, origin))

    # -- reports ---------------------------------------------------------------------------------------

    def record(self, canvas_id: str, node_id: str, body: dict[str, Any]) -> Report:
        report = self.reports.setdefault((canvas_id, node_id), Report())
        report.at = time.time()
        if body.get("loaded") is not None:
            report.loaded = bool(body["loaded"])
        if isinstance(body.get("title"), str):
            report.title = body["title"][:200]
        for line in body.get("lines") or []:
            if isinstance(line, dict) and line.get("level") in ("error", "warning", "exception") and isinstance(line.get("text"), str):
                report.lines.append((str(line["level"]), line["text"][:REPORT_CHARS]))
        for origin in body.get("blocked") or []:
            if isinstance(origin, str) and origin[:200] not in report.blocked:
                report.blocked = [*report.blocked, origin[:200]][-REPORT_LINES:]
        return report


HOOK = "canvas_frames"


def frames_for(manager: Any) -> Frames:
    found = manager.service_hooks.get(HOOK)
    if isinstance(found, Frames):
        return found
    frames = Frames(manager.db)
    manager.service_hooks[HOOK] = frames
    return frames


BRIDGE = r"""(() => {
  // The bridge between a page shown in a canvas frame and the board around it. It runs first in the
  // page, in the page's own sandbox, and says only what the board asks about: that the page loaded,
  // what it logged as an error, which origin the browser refused it, and which element the operator
  // picked in comment mode. Everything it sends is the page's own words, so the board treats it all
  // as untrusted text, and the board's own messages it obeys are only the mode and the highlight.
  const parentOrigin = document.currentScript && document.currentScript.dataset.parent;
  if (!parentOrigin || window.parent === window) return;
  const send = (message) => { try { window.parent.postMessage({ daedalus: "canvas-frame", ...message }, parentOrigin); } catch (_) {} };
  const text = (value) => { try { return typeof value === "string" ? value : value instanceof Error ? `${value.name}: ${value.message}` : JSON.stringify(value); } catch (_) { return String(value); } };
  for (const level of ["error", "warn"]) {
    const original = console[level].bind(console);
    console[level] = (...args) => { send({ type: "console", level: level === "warn" ? "warning" : "error", text: args.map(text).join(" ").slice(0, 1000) }); original(...args); };
  }
  addEventListener("error", (event) => send({ type: "console", level: "exception", text: `${event.message || "error"} at ${event.filename || ""}:${event.lineno || 0}`.slice(0, 1000) }));
  addEventListener("unhandledrejection", (event) => send({ type: "console", level: "exception", text: `unhandled rejection: ${text(event.reason)}`.slice(0, 1000) }));
  addEventListener("securitypolicyviolation", (event) => {
    let origin = "";
    try { origin = new URL(event.blockedURI).origin; } catch (_) { origin = event.blockedURI; }
    if (origin && origin !== "null" && /^https?:/.test(origin)) send({ type: "blocked", origin, directive: event.effectiveDirective });
  });
  addEventListener("load", () => send({ type: "loaded", title: document.title.slice(0, 200), path: location.pathname }));

  let mode = "interact";
  let marked = null;
  const outline = document.createElement("div");
  outline.style.cssText = "position:fixed;pointer-events:none;z-index:2147483647;border:2px solid #7c5cff;background:rgba(124,92,255,.12);border-radius:3px;display:none";
  const place = (element) => {
    if (!element) { outline.style.display = "none"; return; }
    const box = element.getBoundingClientRect();
    Object.assign(outline.style, { display: "block", left: `${box.left}px`, top: `${box.top}px`, width: `${box.width}px`, height: `${box.height}px` });
    if (!outline.isConnected) document.documentElement.appendChild(outline);
  };
  const selectorOf = (element) => {
    const parts = [];
    for (let node = element; node && node.nodeType === 1 && node !== document.documentElement && parts.length < 6; node = node.parentElement) {
      if (node.id && /^[A-Za-z][\w-]*$/.test(node.id)) { parts.unshift(`#${node.id}`); break; }
      let part = node.localName;
      const classes = [...node.classList].filter((name) => /^[A-Za-z][\w-]*$/.test(name)).slice(0, 2);
      if (classes.length) part += `.${classes.join(".")}`;
      const siblings = node.parentElement ? [...node.parentElement.children].filter((other) => other.localName === node.localName) : [];
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
      parts.unshift(part);
    }
    return parts.join(" > ");
  };
  addEventListener("mousemove", (event) => { if (mode === "comment") { marked = event.target; place(marked); } }, true);
  const pick = (event) => {
    if (mode !== "comment") return;
    event.preventDefault();
    event.stopPropagation();
    const element = event.target;
    if (!(element instanceof Element)) return;
    const box = element.getBoundingClientRect();
    send({
      type: "picked",
      selector: selectorOf(element),
      tag: element.localName,
      text: (element.innerText || element.textContent || "").replace(/\s+/g, " ").trim().slice(0, 300),
      html: element.outerHTML.slice(0, 2000),
      rect: { x: Math.round(box.left), y: Math.round(box.top), width: Math.round(box.width), height: Math.round(box.height) },
    });
  };
  addEventListener("click", pick, true);
  addEventListener("submit", (event) => { if (mode === "comment") event.preventDefault(); }, true);
  // A static copy of what the page shows now, for a shared view, where a page never runs. Scripts,
  // frames and event handlers are taken out; stylesheets and pictures are fetched and put inline, so
  // the copy needs nothing from anywhere to be drawn. What cannot be fetched is left out.
  const inline = async (url, limit) => {
    const answer = await fetch(url);
    if (!answer.ok) throw new Error("unreadable");
    const blob = await answer.blob();
    if (blob.size > limit) throw new Error("too large");
    return blob;
  };
  const dataUrl = (blob) => new Promise((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => resolve(""); reader.readAsDataURL(blob); });
  const snapshot = async () => {
    try {
      const live = [...document.querySelectorAll("input, textarea, select")];
      const clone = document.documentElement.cloneNode(true);
      const copies = [...clone.querySelectorAll("input, textarea, select")];
      live.forEach((element, index) => {
        const copy = copies[index];
        if (!copy) return;
        if (element instanceof HTMLInputElement && (element.type === "checkbox" || element.type === "radio")) { if (element.checked) copy.setAttribute("checked", ""); else copy.removeAttribute("checked"); }
        else if (element instanceof HTMLTextAreaElement) copy.textContent = element.value;
        else if (element instanceof HTMLSelectElement) [...copy.options].forEach((option, i) => { if (i === element.selectedIndex) option.setAttribute("selected", ""); else option.removeAttribute("selected"); });
        else copy.setAttribute("value", element.value);
      });
      const canvases = [...document.querySelectorAll("canvas")];
      [...clone.querySelectorAll("canvas")].forEach((copy, index) => {
        const picture = document.createElement("img");
        try { picture.src = canvases[index].toDataURL(); } catch (_) {}
        picture.style.cssText = copy.getAttribute("style") || "";
        picture.width = canvases[index] ? canvases[index].clientWidth : 0;
        copy.replaceWith(picture);
      });
      clone.querySelectorAll("script, noscript, iframe, frame, object, embed, base, meta[http-equiv], link:not([rel~=stylesheet])").forEach((node) => node.remove());
      for (const element of clone.querySelectorAll("*")) for (const attribute of [...element.attributes]) {
        if (/^on/i.test(attribute.name) || /^\s*javascript:/i.test(attribute.value)) element.removeAttribute(attribute.name);
      }
      for (const link of [...clone.querySelectorAll("link[rel~=stylesheet]")]) {
        const style = document.createElement("style");
        try { style.textContent = await (await inline(link.href, 1_000_000)).text(); } catch (_) {}
        link.replaceWith(style);
      }
      for (const picture of [...clone.querySelectorAll("img[src]")]) {
        if (picture.src.startsWith("data:")) continue;
        try { picture.setAttribute("src", await dataUrl(await inline(picture.src, 1_000_000))); } catch (_) { picture.removeAttribute("src"); }
        picture.removeAttribute("srcset");
      }
      const html = `<!doctype html>${clone.outerHTML}`;
      if (html.length <= 3_000_000) send({ type: "snapshot", html, width: innerWidth, height: innerHeight });
    } catch (_) {}
  };
  addEventListener("message", (event) => {
    if (event.source !== window.parent || event.origin !== parentOrigin) return;
    const data = event.data || {};
    if (data.daedalus !== "canvas-board") return;
    if (data.type === "mode") { mode = data.mode === "comment" ? "comment" : "interact"; if (mode !== "comment") place(null); document.documentElement.style.cursor = mode === "comment" ? "crosshair" : ""; }
    if (data.type === "snapshot") void snapshot();
    if (data.type === "highlight") { try { place(data.selector ? document.querySelector(String(data.selector)) : null); } catch (_) { place(null); } }
  });
})();
"""


def inject(html: bytes, token: str, parent: str) -> bytes:
    """The page with the bridge loaded before anything of its own: first in ``<head>`` when there is
    one, else first in the document."""
    tag = f'<script src="/f/{token}/{RESERVED}/bridge.js" data-parent="{parent}"></script>'.encode()
    lowered = html[:4096].lower()
    head = lowered.find(b"<head")
    if head >= 0:
        end = html.find(b">", head)
        if end >= 0:
            return html[: end + 1] + tag + html[end + 1:]
    doctype = lowered.find(b"<!doctype")
    if doctype >= 0:
        end = html.find(b">", doctype)
        if end >= 0:
            return html[: end + 1] + tag + html[end + 1:]
    return tag + html


__all__ = [
    "ADDRESS_HOURS",
    "BRIDGE",
    "DEVICES",
    "MAX_FRAMES",
    "RESERVED",
    "Capability",
    "FrameError",
    "Frames",
    "Report",
    "device_size",
    "frames_for",
    "inject",
    "origin_of",
    "page_and_root",
    "policy",
    "served_file",
]
