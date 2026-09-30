"""A terminal service for the browser checks: the REST routes and the WebSocket, scripted.

The app's terminal speaks a binary protocol to the host, which relays it from the terminal daemon.
Neither runs here. This answers ``/api/terminals*`` the way the host does and serves
``/ws/terminals/{id}`` through ``page.route_web_socket``, holding each terminal's output stream as the
daemon would: numbered by byte offset, sent only inside the window the client acknowledges, resumed
from a tail when the client still holds the screen and replaced by a snapshot when it does not.

Everything the page sends is recorded per connection, so a check can say exactly which socket sent
which RESIZE, whether an ATTACH asked for a tail, and how far unacknowledged output ever ran ahead.

Frames are encoded with the host's own codec when it can be imported (``daedalus/terminals/wire.py``);
otherwise with the forty lines below, which follow the same table.
"""

from __future__ import annotations

import json
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

OUTPUT, SNAPSHOT, EVENT, INPUT, RESIZE, ACK, ATTACH = 0x01, 0x02, 0x03, 0x10, 0x11, 0x12, 0x13

try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from daedalus.terminals.wire import BrowserFrame, decode_browser, encode_browser  # type: ignore[import-not-found]

    def enc_output(seq: int, data: bytes) -> bytes:
        return encode_browser(BrowserFrame("output", seq=seq, data=data))

    def enc_snapshot(cols: int, rows: int, seq: int, data: bytes) -> bytes:
        return encode_browser(BrowserFrame("snapshot", seq=seq, cols=cols, rows=rows, data=data))

    def enc_event(body: dict[str, Any]) -> bytes:
        return encode_browser(BrowserFrame("event", json=body))

    def decode(frame: bytes) -> tuple[str, dict[str, Any]]:
        f = decode_browser(frame)
        return f.kind, {"seq": f.seq, "data": f.data, "cols": f.cols, "rows": f.rows, "px_w": f.px_w, "px_h": f.px_h, "json": f.json}

    CODEC = "host"
except Exception:  # noqa: BLE001 - any failure to import the host means the local codec
    def enc_output(seq: int, data: bytes) -> bytes:
        return bytes([OUTPUT]) + struct.pack(">Q", seq) + data

    def enc_snapshot(cols: int, rows: int, seq: int, data: bytes) -> bytes:
        return bytes([SNAPSHOT]) + struct.pack(">HHQ", cols, rows, seq) + data

    def enc_event(body: dict[str, Any]) -> bytes:
        return bytes([EVENT]) + json.dumps(body, separators=(",", ":")).encode()

    def decode(frame: bytes) -> tuple[str, dict[str, Any]]:
        kind, body = frame[0], frame[1:]
        out: dict[str, Any] = {"seq": 0, "data": b"", "cols": 0, "rows": 0, "px_w": 0, "px_h": 0, "json": None}
        if kind == INPUT:
            return "input", {**out, "data": body}
        if kind == RESIZE:
            cols, rows, pw, ph = struct.unpack(">HHHH", body)
            return "resize", {**out, "cols": cols, "rows": rows, "px_w": pw, "px_h": ph}
        if kind == ACK:
            return "ack", {**out, "seq": struct.unpack(">Q", body)[0]}
        if kind == ATTACH:
            return "attach", {**out, "json": json.loads(body.decode())}
        raise ValueError(f"unexpected frame 0x{kind:02x}")

    CODEC = "local"

ENVS: list[dict[str, Any]] = [
    {"env": "container", "available": True, "reason": "", "detail": "", "version": "0.1.0", "sandbox": "ok", "shell": "/bin/bash", "home": "/root", "port_range": "8120-8139", "public_host": "", "preview_poll_ms": 3000, "running": 0},
    {"env": "host", "available": True, "reason": "", "detail": "", "version": "0.1.0", "sandbox": "bwrap cannot create namespaces here: setting up uid map: Permission denied", "shell": "/bin/bash", "home": "/home/operator", "port_range": "", "public_host": "", "preview_poll_ms": 3000, "running": 0},
]

WINDOW = 256 * 1024
ACK_BYTES = 64 * 1024
BATCH = 64 * 1024


@dataclass
class Term:
    id: str
    env: str = "container"
    title: str = "bash"
    cwd: str = "/home/operator/work/bakery"
    owner_kind: str = "session"
    owner_id: str = ""
    status: str = "running"
    exit_code: int | None = None
    cols: int = 80
    rows: int = 24
    stream: bytearray = field(default_factory=bytearray)
    # The stream offset of the last resize: a client whose screen predates it gets a snapshot.
    last_resize_seq: int = 0
    # What a snapshot draws (the emulator's screen, in the real daemon). None: the stream's tail.
    snapshot: bytes | None = None
    busy: bool = False
    size_owner: str = "host"
    # What the Terminals screen shows of it without connecting: the owner's name, the project, the last
    # rows as styled runs (the daemon's encoding: colour 0 default, 1..256 palette + 1), and when.
    owner_label: str = ""
    project_id: str | None = None
    preview: list[list[dict[str, Any]]] = field(default_factory=list)
    exit_signal: str | None = None
    created_at: str = "2026-09-24T09:00:00Z"
    exited_at: str | None = None
    last_input_at: str | None = None
    clients: int = 1
    activity: dict[str, Any] | None = None
    created_by: str = "operator"
    sandbox: bool = False
    # Shell integration, as the daemon keeps it: the commands the shell marked, with absolute rows
    # (counted from the terminal's start by line feeds: the stub's output is line-oriented), and the
    # row of the prompt being typed at. None: the shell has not marked a prompt.
    commands: list[dict[str, Any]] = field(default_factory=list)
    prompt_row: int | None = None
    row: int = 0
    # Who holds the keyboard, as the daemon arbitrates it: a person typing takes it for a while.
    keyboard: str = "auto"

    def view(self, preview: bool = False) -> dict[str, Any]:
        running = self.status == "running"
        return {
            "id": self.id, "env": self.env, "title": self.title,
            "owner": {"kind": self.owner_kind, "id": self.owner_id or None, "label": self.owner_label}, "project_id": self.project_id,
            "profile": "shell", "sandbox": self.sandbox, "cwd": self.cwd, "status": self.status,
            "exit_code": self.exit_code, "exit_signal": self.exit_signal, "created_at": self.created_at, "created_by": self.created_by,
            "exited_at": self.exited_at, "last_output_at": None, "last_input_at": self.last_input_at, "cols": self.cols, "rows": self.rows,
            "live": {"clients": self.clients, "busy": self.busy, "keyboard": {"owner": self.keyboard, "until": None}, "size_owner": self.size_owner, "alt_screen": False} if running else None,
            "last_command": None, "preview": self.preview if preview else [], "activity": self.activity,
        }


@dataclass
class Client:
    n: int
    term: Term
    ws: Any
    frames: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    sent: int = 0
    acked: int = 0
    attached: bool = False
    closed: bool = False
    max_unacked: int = 0
    # Stop the stream after this offset until told otherwise (to let a check read a stable screen).
    hold: int | None = None

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [f for k, f in self.frames if k == kind]


class TerminalStub:
    """The terminals of one invented session, their REST routes, and their sockets."""

    def __init__(self, session_id: str, *, envs: list[dict[str, Any]] | None = None, window: int = WINDOW) -> None:
        self.session_id = session_id
        self.envs = envs if envs is not None else ENVS
        self.window = window
        self.terms: dict[str, Term] = {}
        self.clients: list[Client] = []
        self.requests: list[tuple[str, str, Any]] = []
        # Every frame from the page, across all connections, in the order it arrived.
        self.log: list[tuple[str, int, str, dict[str, Any]]] = []
        self.counter = 0
        self.lock = threading.RLock()
        # The machine-wide cap: an operator's create past it is answered 409 `over_cap` until it is
        # sent again with `confirm: true`, exactly as the host does.
        self.cap = 20
        # What `GET /api/terminals/load` answers; a check replaces it to paint the bar warn or bad.
        self.load = load_answer()
        # What a sandboxed create or restart reports as left read-only, as the host would.
        self.skipped: list[dict[str, str]] = []
        # Seconds between an attach's hello and the snapshot behind it. A loaded machine spaces them
        # like this, and a size the page sends in that gap is confirmed only after the snapshot drawn
        # at the old size. A check sets it so the frames come in that order on every run; whether the
        # snapshot is still being parsed when the confirmation lands is left to xterm.js's timing,
        # which is why the exact order is pinned by `connection.test.ts` as well.
        self.snapshot_delay = 0.0

    # -- the terminals ------------------------------------------------------------------------

    def add(self, id_: str, **fields: Any) -> Term:
        term = Term(id=id_, owner_id=fields.pop("owner_id", self.session_id), **fields)
        self.terms[id_] = term
        return term

    def emit(self, id_: str, data: bytes | str) -> None:
        """The program in terminal ``id_`` printed ``data``: every client gets it inside its window."""
        with self.lock:
            term = self.terms[id_]
            chunk = data.encode() if isinstance(data, str) else data
            term.stream += chunk
            term.row += chunk.count(b"\n")
            for client in self.live(id_):
                self.pump(client)

    # -- shell integration --------------------------------------------------------------------

    def mark(self, id_: str, letter: str, event: dict[str, Any], *, early: bool = False) -> None:
        """Print a shell mark and tell the attached clients, as the daemon does: the event carries the
        offset just after the mark. ``early`` sends the event ahead of the bytes, which the daemon does
        whenever output is still batched for a client."""
        with self.lock:
            term = self.terms[id_]
            data = f"\x1b]133;{letter}\x07".encode()
            body = {"type": "command", **event, "seq": len(term.stream) + len(data), "at": "2026-09-24T09:00:00Z"}
            if early:
                self.broadcast(id_, body)
            self.emit(id_, data)
            if not early:
                self.broadcast(id_, body)

    def broadcast(self, id_: str, body: dict[str, Any]) -> None:
        for client in self.live(id_):
            if client.attached:
                self.send(client, enc_event(body))

    def hold_keyboard(self, id_: str, owner: str = "human") -> None:
        """What the daemon tells every client when a person starts typing (or lets go)."""
        self.terms[id_].keyboard = owner
        self.broadcast(id_, {"type": "keyboard", "owner": owner, "until": None})

    def shell_prompt(self, id_: str, text: str = "$ ", *, early: bool = False) -> None:
        """A prompt: its start marked, then its text."""
        term = self.terms[id_]
        term.prompt_row = term.row
        self.mark(id_, "A", {"phase": "prompt", "abs_row": term.row}, early=early)
        self.emit(id_, text)

    def shell_command(self, id_: str, command: str, output: list[str], exit_code: int | None = 0, *, finish: bool = True, early: bool = False) -> dict[str, Any]:
        """The operator typed ``command`` at the prompt; it prints ``output`` and, with ``finish``, ends."""
        term = self.terms[id_]
        self.emit(id_, f"{command}\r\n")
        record = {"n": len(term.commands) + 1, "command": command, "cwd": term.cwd, "exit_code": None, "started_at": "2026-09-24T09:00:00Z",
                  "finished_at": None, "duration_ms": None, "prompt_row": term.prompt_row, "output_row": term.row, "end_row": None, "lines": []}
        term.commands.append(record)
        term.busy = True
        self.mark(id_, "C", {"phase": "start", "n": record["n"], "command": command, "abs_row": record["output_row"], "prompt_row": record["prompt_row"]}, early=early)
        self.print_lines(id_, record, output)
        if finish:
            self.finish_command(id_, exit_code, early=early)
        return record

    def print_lines(self, id_: str, record: dict[str, Any], lines: list[str]) -> None:
        record["lines"].extend(lines)
        if lines:
            self.emit(id_, "".join(f"{line}\r\n" for line in lines))

    def finish_command(self, id_: str, exit_code: int | None = 0, *, early: bool = False) -> None:
        term = self.terms[id_]
        record = term.commands[-1]
        record.update(exit_code=exit_code, end_row=term.row, finished_at="2026-09-24T09:00:01Z", duration_ms=1000)
        term.busy = False
        self.mark(id_, "D", {"phase": "end", "n": record["n"], "exit_code": exit_code, "command": record["command"], "abs_row": record["output_row"],
                             "prompt_row": record["prompt_row"], "end_row": record["end_row"], "duration_ms": 1000}, early=early)

    def marks_event(self, term: Term, first: int) -> dict[str, Any]:
        """What follows a snapshot: every command whose rows reach its first row, and the current prompt."""
        listed = [{"n": c["n"], "command": c["command"], "exit_code": c["exit_code"], "prompt_row": c["prompt_row"], "output_row": c["output_row"],
                   "end_row": c["end_row"], "running": c["end_row"] is None} for c in term.commands if c["end_row"] is None or c["end_row"] >= first]
        return {"type": "marks", "list": listed, "first_abs_row": first, "prompt_row": term.prompt_row}

    def command_records(self, term: Term, last: int, output: bool) -> list[dict[str, Any]]:
        out = []
        for c in term.commands[-last:]:
            record = {k: v for k, v in c.items() if k != "lines"}
            if output:
                record["output"] = "\n".join(c["lines"])[-64 * 1024:]
            out.append(record)
        return out

    def resize_by_other(self, id_: str, cols: int, rows: int) -> None:
        """Another screen took the size: the PTY is resized and every client hears who owns it."""
        with self.lock:
            term = self.terms[id_]
            term.cols, term.rows, term.size_owner = cols, rows, "other"
            term.last_resize_seq = len(term.stream)
            for client in self.live(id_):
                self.send(client, enc_event({"type": "size", "cols": cols, "rows": rows, "owner": "other"}))

    def exit(self, id_: str, code: int = 0) -> None:
        with self.lock:
            term = self.terms[id_]
            term.status, term.exit_code, term.busy = "exited", code, False
            for client in self.live(id_):
                self.send(client, enc_event({"type": "exit", "code": code, "signal": None}))

    def drop(self, id_: str, code: int = 4000) -> None:
        """Every socket of the terminal goes away, as a network change or a host restart does it."""
        with self.lock:
            for client in self.live(id_):
                client.closed = True
                try:
                    client.ws.close(code=code, reason="dropped")
                except Exception:  # noqa: BLE001 - the page may have closed it first
                    pass

    def live(self, id_: str) -> list[Client]:
        return [c for c in self.clients if c.term.id == id_ and not c.closed]

    def of(self, id_: str) -> list[Client]:
        return [c for c in self.clients if c.term.id == id_]

    def resizes(self, id_: str | None = None) -> list[tuple[str, int, int, int]]:
        """Every RESIZE the page sent, in the order it arrived: (terminal, connection number, cols, rows)."""
        return [(t, n, f["cols"], f["rows"]) for t, n, kind, f in self.log if kind == "resize" and (id_ is None or t == id_)]

    def inputs(self, id_: str) -> bytes:
        return b"".join(f["data"] for c in self.of(id_) for kind, f in c.frames if kind == "input")

    # -- the socket ---------------------------------------------------------------------------

    def socket(self, ws: Any) -> None:
        """``page.route_web_socket("**/ws/terminals/**", stub.socket)``."""
        path = urlsplit(ws.url).path
        id_ = path.rstrip("/").rsplit("/", 1)[-1]
        with self.lock:
            term = self.terms.get(id_)
            if term is None:
                ws.close(code=4404, reason="no terminal")
                return
            self.counter += 1
            client = Client(n=self.counter, term=term, ws=ws)
            self.clients.append(client)
        ws.on_message(lambda message: self.on_message(client, message))
        # No close handler: with one installed, a socket the page closes itself (a restarted
        # terminal's old instance, say) reaches Playwright 1.62 as a close event without a code, whose
        # KeyError then fails every later call of the check. A socket the page closed is found by
        # the next send failing, which marks the client closed.

    def on_message(self, client: Client, message: bytes | str) -> None:
        if isinstance(message, str):
            message = message.encode("latin-1")
        kind, f = decode(bytes(message))
        with self.lock:
            client.frames.append((kind, f))
            self.log.append((client.term.id, client.n, kind, f))
            term = client.term
            if kind == "attach":
                self.attach(client, f["json"] or {})
            elif kind == "ack":
                client.acked = max(client.acked, f["seq"])
                self.pump(client)
            elif kind == "resize":
                term.cols, term.rows, term.size_owner = f["cols"], f["rows"], "human"
                term.last_resize_seq = len(term.stream)
                for other in self.live(term.id):
                    self.send(other, enc_event({"type": "size", "cols": term.cols, "rows": term.rows, "owner": "you" if other is client else "other"}))

    def hello(self, client: Client) -> bytes:
        term = client.term
        return enc_event({
            "type": "hello", "client_id": f"c{client.n}", "read_only": False, "ack_bytes": ACK_BYTES, "window_bytes": self.window,
            "terminal": {"id": term.id, "title": term.title, "cwd": term.cwd, "status": term.status, "cols": term.cols, "rows": term.rows},
            "size": {"cols": term.cols, "rows": term.rows, "owner": term.size_owner if term.size_owner != "human" else "other"},
            "keyboard": {"owner": term.keyboard, "until": None},
            "modes": {"alt_screen": False, "mouse": False, "bracketed_paste": False, "app_cursor": False},
        })

    def attach(self, client: Client, request: dict[str, Any]) -> None:
        term = client.term
        self.send(client, self.hello(client))
        if self.snapshot_delay:
            time.sleep(self.snapshot_delay)
        last = int(request.get("lastSeq") or 0)
        tail = bool(request.get("haveState")) and 0 <= last <= len(term.stream) and term.last_resize_seq <= last
        client.attached = True
        if tail:
            client.sent = client.acked = last
        else:
            screen = term.snapshot if term.snapshot is not None else bytes(term.stream[-4096:])
            # The snapshot's first line is the row its first byte is on: a cut in the middle of a line
            # leaves that line's end as the first row, numbered by the line feeds before it.
            first = 0 if term.snapshot is not None else bytes(term.stream[:-4096]).count(b"\n")
            self.send(client, enc_event({"type": "resync", "reason": "attach", "first_abs_row": first}))
            self.send(client, enc_snapshot(term.cols, term.rows, len(term.stream), screen))
            if term.commands or term.prompt_row is not None:
                self.send(client, enc_event(self.marks_event(term, first)))
            client.sent = client.acked = len(term.stream)
        if term.status != "running":
            self.send(client, enc_event({"type": "exit", "code": term.exit_code, "signal": None}))
        self.pump(client)

    def pump(self, client: Client) -> None:
        """Send what the window allows: never more than ``window`` bytes past the last ACK."""
        term = client.term
        if not client.attached or client.closed:
            return
        end = len(term.stream) if client.hold is None else min(client.hold, len(term.stream))
        while client.sent < end:
            room = self.window - (client.sent - client.acked)
            if room <= 0:
                return
            size = min(BATCH, room, end - client.sent)
            self.send(client, enc_output(client.sent, bytes(term.stream[client.sent:client.sent + size])))
            client.sent += size
            client.max_unacked = max(client.max_unacked, client.sent - client.acked)

    def send(self, client: Client, frame: bytes) -> None:
        if client.closed:
            return
        try:
            client.ws.send(frame)
        except Exception:  # noqa: BLE001 - a socket the page closed a moment ago
            client.closed = True

    # -- REST ---------------------------------------------------------------------------------

    def running(self) -> int:
        return sum(1 for t in self.terms.values() if t.status == "running")

    def listing(self, query: dict[str, list[str]]) -> dict[str, Any]:
        owner = query.get("owner_id", [None])[0]
        kind = query.get("owner_kind", [None])[0]
        project = query.get("project_id", [None])[0]
        preview = int(query.get("preview", ["0"])[0] or 0) > 0
        rows = [t.view(preview) for t in self.terms.values() if (owner is None or t.owner_id == owner) and (kind is None or t.owner_kind == kind) and (project is None or t.project_id == project)]
        return {"envs": self.envs, "terminals": rows, "capacity": {"running": self.running(), "cap": self.cap, "queued": 0}}

    def route(self, route: Any) -> None:
        """``page.route("**/api/terminals**", stub.route)``: registered after the general stub, so it wins."""
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path[parts.path.index("/api/"):]
        query = parse_qs(parts.query)
        body = None
        if request.method in ("POST", "PATCH"):
            try:
                body = request.post_data_json
            except Exception:  # noqa: BLE001
                body = None
        with self.lock:
            self.requests.append((request.method, path, body))
            status, answer = self.answer(request.method, path, query, body)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(answer))

    def answer(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        segments = path.strip("/").split("/")  # api, terminals, [id], [action]
        if len(segments) == 2:
            if method == "GET":
                return 200, self.listing(query)
            if method == "POST":
                body = body or {}
                if self.running() >= self.cap and not body.get("confirm"):
                    return 409, {"code": "over_cap", "running": self.running(), "cap": self.cap, "detail": "the machine runs as many terminals as the cap allows"}
                self.counter += 1
                id_ = f"new{self.counter:09d}"[:12]
                boxed = bool(body.get("sandbox"))
                term = self.add(
                    id_, env=body.get("env", "container"), title="bash", owner_id=body.get("owner_id") or "",
                    owner_kind=body.get("owner_kind", "session"), project_id=body.get("project_id"), sandbox=boxed,
                    **({"cwd": body["cwd"]} if body.get("cwd") else {}),
                )
                return 201, {**term.view(), **({"sandbox_skipped": self.skipped} if boxed else {})}
        if len(segments) == 3 and segments[2] == "load" and method == "GET":
            return 200, {**self.load, "cap": self.cap, "running": self.running()}
        term = self.terms.get(segments[2]) if len(segments) >= 3 else None
        if term is None:
            return 404, {"detail": "no such terminal"}
        action = segments[3] if len(segments) > 3 else ""
        if action == "" and method == "GET":
            return 200, term.view()
        if action == "" and method == "PATCH":
            # A rename: the operator's title, or back to the program's with an empty one.
            if "title" in (body or {}):
                term.title = str(body["title"]).strip()[:200] or "bash"
            return 200, term.view()
        if action == "" and method == "DELETE":
            if term.status == "running":
                return 409, {"detail": "the terminal is running"}
            del self.terms[term.id]
            return 200, {"ok": True}
        if action == "commands" and method == "GET":
            if not term.commands and term.prompt_row is None:
                return 501, {"detail": "its commands are not recorded (it is not a shell started with its integration)"}
            last = max(1, min(500, int(query.get("last", ["20"])[0] or 20)))
            return 200, {"commands": self.command_records(term, last, query.get("output", ["0"])[0] in ("1", "true"))}
        if action == "keyboard" and method == "POST":
            self.hold_keyboard(term.id, str((body or {}).get("owner") or "auto"))
            return 200, {"owner": term.keyboard, "until": None}
        if action == "ticket":
            return 200, {"ticket": f"tk-{term.id}", "expires_in": 30}
        if action == "kill":
            self.exit(term.id, 129)
            return 200, term.view()
        if action == "restart":
            self.counter += 1
            asked = (body or {}).get("sandbox")
            boxed = term.sandbox if asked is None else bool(asked)
            if term.status == "running":
                self.exit(term.id, 129)
            fresh = self.add(f"rst{self.counter:09d}"[:12], env=term.env, title=term.title, cwd=term.cwd, owner_id=term.owner_id, sandbox=boxed)
            return 200, {**fresh.view(), **({"sandbox_skipped": self.skipped} if boxed else {})}
        return 404, {"detail": "no such route"}

    def install(self, page: Any) -> None:
        page.route("**/api/terminals**", self.route)
        page.route_web_socket("**/ws/terminals/**", self.socket)


def load_answer(*, total_gb: float = 62, used_gb: float = 20, terminals_gb: float = 1.4, likely_mb: int = 500, cpus: int = 8, machine_cpu: float = 20.0) -> dict[str, Any]:
    """``GET /api/terminals/load`` as the host answers it; the page does the arithmetic and the words."""
    gb, mb = 1 << 30, 1 << 20
    return {
        "cap": 20, "running": 0, "queued": [],
        "used": {"rss_bytes": int(terminals_gb * gb), "daemon_rss_bytes": 60 * mb, "cpu_percent": 12.0, "cpus": cpus,
                 "mem_total_bytes": int(total_gb * gb), "mem_available_bytes": int((total_gb - used_gb) * gb), "machine_cpu_percent": machine_cpu},
        "profiles": {"shell": {"rss_bytes": likely_mb * mb, "cpu_percent": 4.0, "samples": 12}},
        "likely": {"rss_bytes": likely_mb * mb, "cpu_percent": 4.0, "samples": 12, "basis": "running"},
        "projection": {"cap": 20, "sessions": 20, "terminals_rss_bytes": 0, "machine_used_bytes": 0, "mem_total_bytes": int(total_gb * gb), "mem_percent": 0.0, "cpu_percent": 0.0, "level": "ok", "cpu_level": "ok"},
        "envs": [], "thresholds": {"warn": 70.0, "bad": 90.0},
    }


def run(text: str, fg: int = 0, **style: Any) -> dict[str, Any]:
    """One styled run of a preview row; ``fg`` is an ANSI colour 1..15, encoded as the daemon does (0 leaves the default)."""
    out: dict[str, Any] = {"t": text}
    if fg:
        out["fg"] = fg + 1
    out.update(style)
    return out


def dock_state(session_id: str, tabs: list[str], *, active: str | None = None, split: str | None = None, open_: bool = True, height: int = 300) -> str:
    """An init script that opens the session's dock with these tabs, as the page would have saved it."""
    state = {"open": open_, "height": height, "tabs": tabs, "active": active or (tabs[0] if tabs else None), "split": split}
    return f"try {{ localStorage.setItem('daedalus.dock.{session_id}', {json.dumps(json.dumps(state))}); }} catch (e) {{}}"


# The page publishes a read-only view of its terminals (text, size, renderer) only when asked.
DEBUG = "try { localStorage.setItem('daedalus.debug.terminals', '1'); } catch (e) {}"


def open_session(context: Any, stub: TerminalStub, general: Any, base: str, session_id: str, *, lang: str = "en", wait: str = ".chat-scroll .timeline") -> Any:
    """A page on the session with every API answered: the terminals by ``stub``, the rest by ``general``."""
    page = context.new_page()
    page.route("**/api/**", general)
    stub.install(page)
    page.goto(f"{base}/agents/{session_id}?token=t&scheme=dark&lang={lang}")
    page.wait_for_selector(wait, timeout=20000)
    return page


def text(page: Any, id_: str) -> list[str]:
    """What terminal ``id_`` holds, scrollback first, through the page's debug hook."""
    return page.evaluate("(id) => window.__terminals ? window.__terminals.text(id) : []", id_)


def wait_live(page: Any, id_: str, timeout: int = 15000) -> None:
    page.wait_for_selector(f".term-view[data-terminal-view='{id_}'][data-state='live']", timeout=timeout, state="attached")


def stub_requests(stub: TerminalStub, method: str, suffix: str) -> list[Any]:
    """The bodies of the requests the page made to a terminal route ending in ``suffix``."""
    return [b for m, p, b in stub.requests if m == method and p.endswith(suffix)]


__all__ = ["CODEC", "DEBUG", "ENVS", "WINDOW", "Term", "TerminalStub", "dock_state", "load_answer", "open_session", "run", "stub_requests", "text", "wait_live"]
