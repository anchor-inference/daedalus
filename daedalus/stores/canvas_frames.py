"""Live pages on a canvas: the frame's signed address, the files it may read, and the walls around it.

A frame is a file node of a canvas whose file is an HTML page of the agent's workspace. It runs a page
the agent wrote, so it is held by two walls that do not depend on each other:

- it is served only by the services door, another origin than the app's (``api_services_door``); on
  the app's own origin the route does not exist. The door does not strip cookies on ``/f/``, and need
  not: the route reads none and sets none, and the sandbox below means the page never has one;
- every answer carries a CSP ``sandbox`` without ``allow-same-origin``, and the board's iframe has the
  same sandbox, so the page has an opaque origin: no cookie, no storage of anything else, no popups,
  no dialogs, no downloads, no navigation of the window around it.

The address carries a capability, an HMAC over the canvas, the frame, the frame's secret, an expiry,
the app's origin (the only page allowed to frame it), the origins the operator allowed for this one
load and the open board those were allowed on. It is checked against the canvas as it is now: a frame
removed from the board, shown again by the agent, or retired by the board gets a new random secret,
and its old addresses answer nothing, even if a frame of the same id is put back later. The key is one
for all canvases (``canvas_frame_key`` in the database) and signs nothing but frame addresses.

What the page may load is the content security policy built for it: its own address prefix on the
door (not the whole door, which also serves shared services and other frames) and, from elsewhere,
only origins the operator allowed. A page that tries anything else is blocked by the browser, and its
bridge script tells the board which origin was refused, so the operator can allow it "this time",
"always on this canvas" or "always on every canvas". "This time" is signed into the address with the
id of the board that allowed it, and the door honours it only while that board is open and says so
every minute: a page that kept its own address cannot come back to it after the board is closed.

Its files are those under one folder, the root, by default the page's own folder, never the top of the
workspace: a page can read every file under its root and send it to an origin it was allowed, so the
root stays as tight as the page allows. Every file is opened from the filesystem's root down, one
component at a time, refusing a symbolic link at every step, the root's own path included, so swapping
a folder for a link after the page was shown reaches nothing. No dot paths, read only.

The bridge proves each document it speaks for: the door gives a fresh nonce to every document the
board's iframe loads (and to nothing a script fetches), the bridge takes it before the page's own
scripts run, and the board accepts a frame's words only once the host has confirmed that nonce, once.
A page that navigated its frame elsewhere cannot speak for the frame it left.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
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
BOARD_SECONDS = 180
"""How long an open board's "this time" stays good after its last heartbeat (it beats every minute)."""
NONCE_SECONDS = 120
NONCES_PER_FRAME = 16
SELECTOR = re.compile(
    r"^(?:#[A-Za-z][\w-]*|[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][\w-]*){0,2}(?::nth-of-type\(\d{1,4}\))?)"
    r"(?: > (?:#[A-Za-z][\w-]*|[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][\w-]*){0,2}(?::nth-of-type\(\d{1,4}\))?)){0,5}$"
)
"""Exactly what the bridge's own selector builder writes. A pick whose selector is anything else did not
come from the bridge answering a click on the board, and is refused rather than quoted."""
TAG = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,39}$")


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
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username is not None or not re.fullmatch(r"[a-z0-9.\-]+|[0-9a-f:.]+", host):
        raise FrameError(f"{value!r} is not an https origin")
    # An IPv6 literal keeps its brackets in an origin; without them the port reads as part of it.
    named = f"[{host}]" if ":" in host else host
    return f"https://{named}" + (f":{port}" if port and port != 443 else "")


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


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_root(root: Path) -> int:
    """A descriptor of ``root``, reached from ``/`` one component at a time with ``O_NOFOLLOW``.

    The stored root is the real path the page was shown from. Checking it with ``resolve()`` on each
    request followed whatever now stood there: a folder renamed away and a link to ``/`` put in its
    place served the host's own files. Walking down without following anything opens exactly that
    path or nothing, and every later step is relative to this descriptor, so a swap between the check
    and the read cannot redirect it either."""
    if not root.is_absolute():
        raise OSError(errno.EINVAL, "the root is not absolute")
    descriptor = os.open("/", _DIRECTORY)
    try:
        for part in root.parts[1:]:
            if part in (".", ".."):
                raise OSError(errno.EINVAL, "a dot component")
            following = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_served(root: Path, path: str) -> tuple[bytes, str] | None:
    """The bytes and the name of the file ``path`` names under ``root``, or None.

    No dot segment, no symbolic link anywhere from ``/`` to the file, the file itself regular, a folder
    answering its ``index.html``, nothing over :data:`MAX_FILE_BYTES`. A file with more than one hard
    link is refused too: a link made inside the root to a file elsewhere would otherwise be the same
    escape a symbolic link is."""
    parts = [part for part in path.split("/") if part]
    if any(part.startswith(".") or "\\" in part or "\x00" in part for part in parts):
        return None
    try:
        directory = _open_root(root)
    except OSError:
        return None
    opened: list[int] = [directory]
    try:
        for part in parts[:-1]:
            directory = os.open(part, _DIRECTORY, dir_fd=directory)
            opened.append(directory)
        name = parts[-1] if parts else "index.html"
        handle = os.open(name, _FILE, dir_fd=directory)
        opened.append(handle)
        info = os.fstat(handle)
        if stat.S_ISDIR(info.st_mode):
            handle = os.open("index.html", _FILE, dir_fd=handle)
            opened.append(handle)
            info, name = os.fstat(handle), "index.html"
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1 or info.st_size > MAX_FILE_BYTES:
            return None
        chunks, left = [], info.st_size + 1
        while left > 0:
            chunk = os.read(handle, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
        return (data, name) if len(data) <= MAX_FILE_BYTES else None
    except OSError:
        return None
    finally:
        for descriptor in opened:
            os.close(descriptor)


def policy(app_origin: str, own: str, allowed: list[str]) -> str:
    """The content security policy of a frame's every answer.

    ``own`` is the frame's own address prefix on the door (``https://door/f/<token>/``), not ``'self'``:
    the door's origin also serves every shared service and every other frame, and ``'self'`` would let
    this page read them. The allowed origins are added to the fetch directives and nothing else. Inline
    and evaluated script stay possible: the pages are the agent's own drafts, often a single file, and
    the wall is the origin and the sandbox, not the script source. No ``allow-modals`` (a page could
    draw a password prompt that looks like the browser's) and no ``allow-downloads`` (nothing a frame
    shows needs to drop a file on the operator's disk).
    """
    extra = " ".join(allowed)
    return "; ".join([
        "sandbox allow-scripts allow-forms",
        f"default-src {own} data: blob: 'unsafe-inline' 'unsafe-eval' {extra}".rstrip(),
        f"connect-src {own} data: blob: {extra}".rstrip(),
        "form-action 'none'",
        f"base-uri {own}",
        "object-src 'none'",
        f"frame-ancestors {app_origin}",
    ])


@dataclass(frozen=True, slots=True)
class Capability:
    canvas_id: str
    node_id: str
    secret: str
    """The frame's secret when the address was made; a new one retires the address."""
    expires: int
    app_origin: str
    once: tuple[str, ...]
    """Origins allowed for this one address only."""
    board: str = ""
    """The open board that allowed ``once``; those origins count only while it beats."""

    def payload(self) -> bytes:
        return json.dumps([self.canvas_id, self.node_id, self.secret, self.expires, self.app_origin, list(self.once), self.board], separators=(",", ":")).encode()


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
        self.boards: dict[str, float] = {}
        self.nonces: dict[tuple[str, str], dict[str, float]] = {}

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
            canvas_id, node_id, secret, expires, app, once, board = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if expires < (time.time() if now is None else now):
            return None
        return Capability(str(canvas_id), str(node_id), str(secret), int(expires), str(app), tuple(str(o) for o in once), str(board))

    # -- open boards and the documents they load -------------------------------------------------------

    def beat(self, board: str, now: float | None = None) -> None:
        """An open board says it is still open. Its "this time" origins live as long as it keeps saying so."""
        at = time.time() if now is None else now
        self.boards[board] = at
        for name, seen in list(self.boards.items()):
            if at - seen > BOARD_SECONDS:
                del self.boards[name]

    def board_open(self, board: str, now: float | None = None) -> bool:
        seen = self.boards.get(board)
        return bool(board) and seen is not None and (time.time() if now is None else now) - seen <= BOARD_SECONDS

    def issue_nonce(self, canvas_id: str, node_id: str, now: float | None = None) -> str:
        at = time.time() if now is None else now
        issued = {key: until for key, until in self.nonces.get((canvas_id, node_id), {}).items() if until > at}
        nonce = secrets.token_urlsafe(18)
        issued[nonce] = at + NONCE_SECONDS
        while len(issued) > NONCES_PER_FRAME:
            del issued[next(iter(issued))]
        self.nonces[(canvas_id, node_id)] = issued
        return nonce

    def consume_nonce(self, canvas_id: str, node_id: str, nonce: str, now: float | None = None) -> bool:
        """Whether the door gave ``nonce`` to a document of this frame lately; true once only."""
        until = self.nonces.get((canvas_id, node_id), {}).pop(nonce, None)
        return until is not None and until > (time.time() if now is None else now)

    # -- rows ------------------------------------------------------------------------------------------

    async def get(self, canvas_id: str, node_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT canvas_id,node_id,root,entry,generation,secret,updated_at FROM canvas_frames WHERE canvas_id=? AND node_id=?", (canvas_id, node_id))
        return dict(row) if row else None

    async def put(self, canvas_id: str, node_id: str, root: Path, entry: str) -> int:
        """Record the frame's files; showing it again is a new generation and a new secret, which retires
        every address handed out before (the board fetches a new one when it sees the canvas change).
        The secret, not the generation, is what an address names: a frame taken off the board loses its
        row, and a generation counted again from one would have revived its old addresses."""
        count = await self.db.fetchone("SELECT COUNT(*) AS n FROM canvas_frames WHERE canvas_id=? AND node_id!=?", (canvas_id, node_id))
        if count is not None and count["n"] >= MAX_FRAMES:
            raise FrameError(f"a canvas holds at most {MAX_FRAMES} live frames; remove one first")
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO canvas_frames(canvas_id,node_id,root,entry,generation,secret,created_at,updated_at) VALUES(?,?,?,?,1,?,?,?)"
                " ON CONFLICT(canvas_id,node_id) DO UPDATE SET root=excluded.root,entry=excluded.entry,generation=generation+1,secret=excluded.secret,updated_at=excluded.updated_at",
                (canvas_id, node_id, str(root), entry, secrets.token_urlsafe(18), at, at),
            )
            row = await (await conn.execute("SELECT generation FROM canvas_frames WHERE canvas_id=? AND node_id=?", (canvas_id, node_id))).fetchone()
        self.reports.pop((canvas_id, node_id), None)
        return int(row["generation"])

    async def revoke(self, canvas_id: str, node_id: str) -> None:
        """Retire the frame's addresses without moving it: the page navigated somewhere it should not."""
        await self.db.execute("UPDATE canvas_frames SET secret=? WHERE canvas_id=? AND node_id=?", (secrets.token_urlsafe(18), canvas_id, node_id))
        self.nonces.pop((canvas_id, node_id), None)

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
  // what it logged as an error, which origin the browser refused it, which element lies under a point
  // the operator clicked on the board in comment mode, and a still copy of the page for a shared view.
  // Everything it sends is the page's own words, so the board treats it all as untrusted text.
  //
  // The door gave this document a nonce in the script tag. The bridge takes it and removes the tag
  // before any of the page's scripts run, and sends it with every message: the board believes words
  // from the frame only under a nonce the host confirms, once, so a page that navigated its frame
  // elsewhere cannot speak for it, and the page's own scripts cannot read the nonce to hand it on.
  const me = document.currentScript;
  const parentOrigin = me && me.dataset.parent;
  const nonce = (me && me.dataset.nonce) || "";
  if (me) me.remove();
  if (!parentOrigin || window.parent === window) return;
  const post = window.parent.postMessage.bind(window.parent);
  const send = (message) => { try { post({ daedalus: "canvas-frame", nonce, ...message }, parentOrigin); } catch (_) {} };
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

  // Comment mode: the board lays a glass over the frame, so the page never sees the operator's clicks
  // there, and asks what lies under a point. The page cannot pick anything on its own: the board
  // takes a pick only as the answer to a request it made for a click it saw.
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
      let part = /^[A-Za-z][A-Za-z0-9-]*$/.test(node.localName) ? node.localName : "div";
      const classes = [...node.classList].filter((name) => /^[A-Za-z][\w-]*$/.test(name)).slice(0, 2);
      if (classes.length) part += `.${classes.join(".")}`;
      const siblings = node.parentElement ? [...node.parentElement.children].filter((other) => other.localName === node.localName) : [];
      if (siblings.length > 1) part += `:nth-of-type(${Math.min(9999, siblings.indexOf(node) + 1)})`;
      parts.unshift(part);
    }
    return parts.join(" > ");
  };
  const at = (data) => {
    const x = Number(data.x), y = Number(data.y);
    return Number.isFinite(x) && Number.isFinite(y) ? document.elementFromPoint(x, y) : null;
  };
  // A static copy of what the page shows now, for a shared view, where a page never runs. Scripts,
  // frames and event handlers are taken out; stylesheets and pictures are fetched and put inline, so
  // the copy needs nothing from anywhere to be drawn. What anyone typed into the page is left out:
  // the copy is made from the page's markup, never from its fields' live values, and a password
  // field loses even the value its markup gave it.
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
      const clone = document.documentElement.cloneNode(true);
      clone.querySelectorAll("input[type=password i]").forEach((field) => field.removeAttribute("value"));
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
    if (data.type === "mode") { if (data.mode !== "comment") place(null); }
    if (data.type === "snapshot") void snapshot();
    if (data.type === "hover-at") place(at(data));
    if (data.type === "pick-at" && typeof data.id === "string") {
      const element = at(data);
      if (!(element instanceof Element)) return;
      place(element);
      const box = element.getBoundingClientRect();
      send({
        type: "picked",
        id: data.id.slice(0, 64),
        selector: selectorOf(element),
        tag: /^[A-Za-z][A-Za-z0-9-]*$/.test(element.localName) ? element.localName.slice(0, 40) : "",
        text: (element.innerText || element.textContent || "").replace(/\s+/g, " ").trim().slice(0, 300),
        html: element.outerHTML.slice(0, 2000),
        rect: { x: Math.round(box.left), y: Math.round(box.top), width: Math.round(box.width), height: Math.round(box.height) },
      });
    }
    if (data.type === "highlight") { try { place(data.selector ? document.querySelector(String(data.selector)) : null); } catch (_) { place(null); } }
  });
})();
"""


def inject(html: bytes, token: str, parent: str, nonce: str = "") -> bytes:
    """The page with the bridge loaded before anything of its own: right after the doctype when there
    is one, else first in the document, so no script of the page runs before the bridge has taken its
    nonce. DNS prefetching is switched off first, because a prefetch of a name the policy refuses
    still tells that name's server the page asked."""
    tag = (
        '<meta http-equiv="x-dns-prefetch-control" content="off">'
        f'<script src="/f/{token}/{RESERVED}/bridge.js" data-parent="{parent}" data-nonce="{nonce}"></script>'
    ).encode()
    lowered = html[:4096].lower()
    start = len(html) - len(html.lstrip())
    if lowered[start:].startswith(b"<!doctype"):
        end = html.find(b">", start)
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
    "read_served",
]
