"""A browser daemon in-process: the socket protocol of ``docs/architecture/browser.md``, with scripted
pages instead of Chromium.

It speaks the real framing, handshake, JSON-RPC, event subscription and view channels, so the host's
client, service, gateway and tools are exercised end to end over a unix socket. What a page holds is
whatever the test says: an outline, its elements with their roles, names and the fields that are
secret, the text, a dialog, a download. The control rules (a person driving holds the agent back, a
pause refuses it) and the secret-field and stale-ref refusals are the contract's, so the host is
tested against the behaviour it will meet, not against a mock that agrees with it.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import json
import re
import secrets
import struct
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SECRET_AUTOCOMPLETE = ("current-password", "new-password", "one-time-code")
SENSITIVE_WORDS = {
    "purchase": ("buy", "pay", "place order", "checkout", "purchase", "donate", "book now", "купить", "оплатить", "оформить заказ"),
    "send": ("send", "post", "publish", "share", "reply", "submit", "отправить", "опубликовать"),
    "destroy": ("delete", "remove", "revoke", "удалить"),
    "accept": ("accept", "agree"),
}


def stamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _Fail(Exception):
    def __init__(self, code: int, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class Element:
    """One element a snapshot names by its ref."""

    ref: str
    role: str
    name: str
    tag: str = "button"
    type: str = ""
    autocomplete: str = ""
    href: str = ""
    form_action: str = ""
    value: str = ""
    box: dict[str, float] = field(default_factory=lambda: {"x": 100.0, "y": 200.0, "w": 120.0, "h": 32.0})
    goes_to: str = ""
    """A click on it navigates here (a link, a submit)."""
    opens_dialog: dict[str, Any] | None = None
    downloads: tuple[str, bytes] | None = None
    """A click on it downloads this file."""
    covered_by: str = ""
    """What a page put over it: a click is refused ``1004 {ref, covered_by}``, as the daemon refuses one."""
    checked: bool = False
    alerts: str = ""
    """A click on it shows an alert with this message, which the daemon answers by itself."""
    logs: list[dict[str, Any]] = field(default_factory=list)
    """Console entries a click on it makes the page write (``{level, source, text}``)."""

    @property
    def secret(self) -> bool:
        return self.type == "password" or self.autocomplete in SECRET_AUTOCOMPLETE or self.autocomplete.startswith("cc-")

    def field_kind(self) -> str:
        if self.autocomplete.startswith("cc-"):
            return "payment"
        if self.autocomplete == "one-time-code":
            return "one_time_code"
        return "password"

    def line(self) -> str:
        out = f'- {self.role} "{self.name}" [ref={self.ref}]'
        if self.secret:
            out += " [secret]"
        elif self.value and self.role in ("textbox", "searchbox", "combobox"):
            out += f' value="{self.value}"'
        return out


@dataclass
class Page:
    url: str = "about:blank"
    title: str = ""
    text: str = ""
    elements: dict[str, Element] = field(default_factory=dict)
    heading: str = ""
    logs: list[dict[str, Any]] = field(default_factory=list)
    """Console entries the page writes as it loads."""
    requests: list[dict[str, Any]] = field(default_factory=list)
    """Requests the page makes as it loads (``page.network``'s shape, without id and seq); a
    ``body`` key is its response's body, which ``page.request`` gives."""
    inspected: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Ref or selector → what ``page.inspect`` answers for it."""

    def outline(self) -> str:
        lines = [f'- heading "{self.heading}" [level=1]'] if self.heading else []
        lines += [e.line() for e in self.elements.values()]
        if self.text:
            lines.append(f'- text "{self.text[:200]}"')
        return "\n".join(lines)


@dataclass
class Tab:
    id: str
    group_id: str
    page: Page = field(default_factory=Page)
    history: list[Page] = field(default_factory=list)
    dialog: dict[str, Any] | None = None
    created_at: str = field(default_factory=stamp)
    logs: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    log_seq: int = 0
    request_seq: int = 0
    dialog_seq: int = 0

    def log(self, entry: dict[str, Any]) -> None:
        self.log_seq += 1
        self.logs.append({"seq": self.log_seq, "at": int(time.time() * 1000), "level": "info", "source": "console", **entry})

    def loaded(self, page: Page) -> None:
        """What a page writes and asks for as it loads, kept by the tab as the daemon keeps it."""
        for entry in page.logs:
            self.log(entry)
        for request in page.requests:
            self.request_seq += 1
            self.requests.append({"id": f"r{self.request_seq}", "seq": self.request_seq, "at": int(time.time() * 1000), **request})

    def view(self, active: bool) -> dict[str, Any]:
        return {"id": self.id, "group_id": self.group_id, "url": self.page.url, "title": self.page.title, "favicon_url": "", "loading": False, "active": active, "opener": None, "created_at": self.created_at}


@dataclass
class Group:
    id: str
    browser_id: str
    profile: str
    labels: dict[str, str]
    viewport: dict[str, int]
    tabs: list[Tab] = field(default_factory=list)
    active: str = ""
    control: dict[str, Any] = field(default_factory=lambda: {"owner": "agent", "holder": None, "until": None, "reason": ""})
    created_at: str = field(default_factory=stamp)

    def view(self) -> dict[str, Any]:
        return {"id": self.id, "browser_id": self.browser_id, "profile": self.profile, "viewport": self.viewport, "tabs": [t.id for t in self.tabs],
                "active_tab": self.active, "control": dict(self.control), "labels": self.labels, "created_at": self.created_at, "last_activity_at": stamp()}


@dataclass
class Browser:
    id: str
    profile: str
    pid: int = field(default_factory=lambda: 2000 + secrets.randbelow(30000))
    started_at: str = field(default_factory=stamp)

    def view(self, groups: list[Group]) -> dict[str, Any]:
        mine = [g for g in groups if g.browser_id == self.id]
        return {"id": self.id, "profile": self.profile, "pid": self.pid, "status": "running", "started_at": self.started_at,
                "groups": [g.id for g in mine], "tabs": sum(len(g.tabs) for g in mine), "labels": {}}


@dataclass
class ViewChannel:
    """A live view the host opened: the frames it sent, in order, and whether it closed it."""

    id: int
    group_id: str
    client: dict[str, Any]
    client_id: str
    writer: asyncio.StreamWriter
    received: asyncio.Queue[bytes] = field(default_factory=asyncio.Queue)
    closed_by_host: asyncio.Event = field(default_factory=asyncio.Event)
    closed_by_daemon: bool = False

    async def frame(self, timeout: float = 5.0) -> bytes:
        return await asyncio.wait_for(self.received.get(), timeout)


class FakeBrowserd:
    def __init__(self, run_dir: Path, *, env: str = "container", protocol: int = 1, max_browsers: int = 2) -> None:
        self.run_dir = run_dir
        self.env = env
        self.protocol = protocol
        self.max_browsers = max_browsers
        self.instance = secrets.token_hex(8)
        self.token = secrets.token_hex(32)
        self.browsers: dict[str, Browser] = {}
        self.groups: dict[str, Group] = {}
        self.profiles: dict[str, dict[str, Any]] = {}
        self.downloads: dict[str, dict[str, Any]] = {}
        self.download_bytes: dict[str, bytes] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.pages: dict[str, Page] = {}
        """URL → the page a navigation there shows; an unknown URL is a blank page with that URL."""
        self.human_typed: set[str] = set()
        self.events: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, tuple[int, str, dict[str, Any] | None]] = {}
        """Method → the error its next call answers with, once."""
        self.screenshot = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
        self.machine: dict[str, Any] = {"mem_total_bytes": 16 << 30, "mem_available_bytes": 8 << 30, "cpus": 8, "cpu_percent": 10.0}
        self._server: asyncio.AbstractServer | None = None
        self._subscribers: list[tuple[asyncio.StreamWriter, asyncio.Queue[dict[str, Any] | None]]] = []
        self._writers: set[asyncio.StreamWriter] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self.channels: dict[int, ViewChannel] = {}
        self.attached: asyncio.Queue[ViewChannel] = asyncio.Queue()
        self._next = {"browser": 0, "tab": 0, "channel": 0, "action": 0, "download": 0, "upload": 0}
        self._control_changed = asyncio.Event()
        self.wall: dict[str, Any] | None = None
        """The rules ``net.configure`` last set; ``None`` until the host sends them."""
        self.walled: dict[str, tuple[str, str]] = {}
        """Host → (decision, reason) the network wall gives it: ``deny`` or ``ask``. Others pass."""
        self.grants: set[tuple[str, str, int]] = set()
        """(browser, host, port) the host opened with ``net.grant``."""
        self.limits: dict[str, Any] = {}
        """What ``limits.set`` last set."""
        self.recording: dict[str, dict[str, Any]] = {}
        """Group → its recording switch."""
        self.frames: dict[str, list[dict[str, Any]]] = {}
        """Group → its keyframes, each with its ``data``: one after every action while recording."""
        self.workflows: dict[str, dict[str, Any]] = {}
        """Group → its recording of a person's steps: the one on, or the last it made."""
        self.legacy = False
        """A daemon older than the host: no ``page.find``, no point, no scroll to a text or sideways, and
        unknown parameters refused as the real daemon's strict decoding refuses them."""

    # -- lifecycle ------------------------------------------------------------------------

    async def start(self) -> FakeBrowserd:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        sock = self.run_dir / "browserd.sock"
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(sock), limit=(1 << 20) + 64)
        (self.run_dir / "token").write_text(self.token + "\n")
        (self.run_dir / "endpoint").write_text("unix:browserd.sock")
        return self

    async def stop(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.run_dir / "endpoint").unlink()
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
        self._server = None
        self._subscribers.clear()

    async def restart(self) -> None:
        """A new life: new instance, new token, no browsers, an empty event log."""
        await self.stop()
        self.instance = secrets.token_hex(8)
        self.token = secrets.token_hex(32)
        self.browsers.clear()
        self.groups.clear()
        self.events.clear()
        await self.start()

    # -- scripting --------------------------------------------------------------------------

    def emit(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        event = {"seq": len(self.events) + 1, "at": stamp(), "type": kind, "data": data}
        self.events.append(event)
        for _, queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def page(self, url: str, **kwargs: Any) -> Page:
        """A page a navigation to ``url`` shows."""
        page = self.pages[url] = Page(url=url, **kwargs)
        return page

    def tab_of(self, group_id: str) -> Tab:
        group = self.groups[group_id]
        return next(t for t in group.tabs if t.id == group.active)

    def show(self, group_id: str, page: Page) -> None:
        """Put a page on the group's active tab, as if it had been navigated there."""
        tab = self.tab_of(group_id)
        tab.history.append(tab.page)
        tab.page = page
        self.emit("tab.updated", {"group_id": group_id, "tab_id": tab.id, "url": page.url, "title": page.title, "favicon_url": "", "loading": False})

    def crash(self, browser_id: str, reason: str = "crashed") -> None:
        browser = self.browsers.pop(browser_id)
        groups = [g.id for g in self.groups.values() if g.browser_id == browser_id]
        for gid in groups:
            del self.groups[gid]
        self.emit("browser.exited", {"browser_id": browser_id, "profile": browser.profile, "code": -1, "crashed": reason == "crashed", "reason": reason, "groups": groups})

    def needs_you(self, group_id: str, reason: str, what: str) -> None:
        tab = self.tab_of(group_id)
        self.emit("needs_you", {"group_id": group_id, "tab_id": tab.id, "reason": reason, "what": what, "url": tab.page.url, "by": "daemon"})

    def add_download(self, group_id: str, name: str, data: bytes) -> dict[str, Any]:
        self._next["download"] += 1
        did = f"d{self._next['download']}"
        download = {"id": did, "group_id": group_id, "tab_id": self.groups[group_id].active, "name": name, "url": f"https://example.test/{name}", "mime": "application/octet-stream",
                    "size": len(data), "state": "completed", "sha256": hashlib.sha256(data).hexdigest(), "started_at": stamp(), "finished_at": stamp()}
        self.downloads[did] = download
        self.download_bytes[did] = data
        self.emit("download.done", {"group_id": group_id, "download": dict(download)})
        return download

    def block_navigation(self, group_id: str, url: str, reason: str = "egress_allow") -> None:
        """A page's own navigation the allowlist stopped, as the daemon publishes it."""
        tab = self.tab_of(group_id)
        host = url.split("/")[2] if "://" in url else url
        self.emit("navigation.blocked", {"group_id": group_id, "tab_id": tab.id, "url": url, "from": tab.page.url, "by": "page", "host": host, "port": 443, "decision": "ask", "reason": reason})

    def keyframe(self, group_id: str, kind: str, action_id: str = "") -> None:
        if not self.recording.get(group_id, {}).get("frames"):
            return
        frames = self.frames.setdefault(group_id, [])
        tab = self.tab_of(group_id) if group_id in self.groups else None
        frames.append({"no": len(frames) + 1, "at": int(time.time() * 1000), "tab": tab.id if tab else "", "url": tab.page.url if tab else "", "kind": kind, "action_id": action_id, "w": 1280, "h": 800, "bytes": len(self.screenshot), "data": self.screenshot})

    def auto_dialog(self, group_id: str, kind: str, message: str) -> dict[str, Any]:
        """A dialog the daemon answered by itself while the agent held the page, published as it is."""
        tab = self.tab_of(group_id)
        tab.dialog_seq += 1
        answered = {"seq": tab.dialog_seq, "type": kind, "message": message, "url": tab.page.url, "at": int(time.time() * 1000)}
        tab.log({"level": "info", "source": "dialog", "text": f"{kind} (accepted by the browser): {message}"})
        self.emit("dialog.auto", {"group_id": group_id, "tab_id": tab.id, "accepted": True, **answered})
        return answered

    def set_control(self, group_id: str, owner: str, holder: str | None = None, reason: str = "") -> None:
        group = self.groups[group_id]
        group.control = {"owner": owner, "holder": holder, "until": int(time.time() * 1000) + 1_800_000 if owner == "human" else None, "reason": reason}
        self.emit("control", {"group_id": group_id, **group.control})
        if owner != "human" and self.workflows.get(group_id, {}).get("state") == "recording":
            self.stop_workflow(group_id, "control")
        self._control_changed.set()
        self._control_changed = asyncio.Event()

    def record_step(self, group_id: str, **step: Any) -> dict[str, Any]:
        """A person's step, as the daemon records it while they drive and publishes it."""
        workflow = self.workflows[group_id]
        full = {"n": len(workflow["steps"]) + 1, "at": int(time.time() * 1000), "tab": self.groups[group_id].active, "url": self.tab_of(group_id).page.url, **step}
        workflow["steps"].append(full)
        self.emit("workflow.step", {"group_id": group_id, "id": workflow["id"], "step": full, "steps": len(workflow["steps"])})
        return full

    def stop_workflow(self, group_id: str, reason: str) -> dict[str, Any]:
        workflow = self.workflows[group_id]
        workflow.update(state="stopped", reason=reason, stopped_at=int(time.time() * 1000))
        self.emit("workflow.stopped", {"group_id": group_id, "workflow": copy.deepcopy(workflow)})
        return copy.deepcopy(workflow)

    # -- the protocol -----------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        pump: asyncio.Task[None] | None = None
        try:
            channel, payload = await self._read(reader)
            if channel != 0 or payload.strip() != self.token.encode():
                await asyncio.sleep(0.05)
                return
            await self._send(writer, {"jsonrpc": "2.0", "method": "hello", "params": {"version": "fake", "protocol": self.protocol, "instance": self.instance, "env": self.env}})
            while True:
                channel, payload = await self._read(reader)
                if channel != 0:
                    await self._channel_frame(writer, channel, payload)
                    continue
                message = json.loads(payload)
                method, params = message.get("method"), message.get("params") or {}
                self.calls.append((method, params))
                if method == "events.subscribe":
                    after = int(params.get("after_seq") or 0)
                    resync = after > len(self.events)
                    start = 0 if resync else after
                    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
                    for event in self.events[start:]:
                        queue.put_nowait(event)
                    self._subscribers.append((writer, queue))
                    await self._send(writer, {"jsonrpc": "2.0", "id": message["id"], "result": {"instance": self.instance, "from_seq": start + 1, "resync": resync}})
                    pump = asyncio.create_task(self._pump(writer, queue))
                    self._tasks.add(pump)
                    continue
                # Every call runs on its own, as the daemon's do: an agent's call held back by a
                # person driving must not hold the operator's give-back behind it.
                call = asyncio.create_task(self._answer(writer, message["id"], method, params))
                self._tasks.add(call)
                call.add_done_callback(self._tasks.discard)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if pump is not None:
                pump.cancel()
            self._subscribers = [(w, q) for w, q in self._subscribers if w is not writer]
            self._writers.discard(writer)
            writer.close()
            if task is not None:
                self._tasks.discard(task)

    async def _answer(self, writer: asyncio.StreamWriter, call_id: int, method: str, params: dict[str, Any]) -> None:
        try:
            if method == "view.attach" and method not in self.fail:
                result = self._attach(writer, params)
            else:
                result = await self._handle(method, params)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": call_id, "result": result}
        except _Fail as exc:
            error: dict[str, Any] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            reply = {"jsonrpc": "2.0", "id": call_id, "error": error}
        with contextlib.suppress(ConnectionError, RuntimeError):
            await self._send(writer, reply)
        if method == "view.attach" and "result" in reply:
            # Only once the host has the reply: a test that closes the channel at once must not have
            # its close reach the host before the channel is known there, as the real daemon never does.
            self.attached.put_nowait(self.channels[int(reply["result"]["channel"])])

    def _attach(self, writer: asyncio.StreamWriter, params: dict[str, Any]) -> dict[str, Any]:
        gid = str(params.get("group_id") or "")
        if gid not in self.groups:
            raise _Fail(1001, "no such group")
        self._next["channel"] += 1
        cid = self._next["channel"]
        channel = ViewChannel(cid, gid, dict(params.get("client") or {}), f"v{cid}", writer)
        self.channels[cid] = channel
        return {"channel": cid, "client_id": channel.client_id}

    async def _channel_frame(self, writer: asyncio.StreamWriter, channel_id: int, payload: bytes) -> None:
        channel = self.channels.get(channel_id)
        if channel is None or channel.writer is not writer:
            return
        if payload:
            channel.received.put_nowait(payload)
            return
        channel.closed_by_host.set()
        if not channel.closed_by_daemon:
            channel.closed_by_daemon = True
            await self._frame(writer, channel_id, b"")

    async def send_channel(self, channel_id: int, payload: bytes) -> None:
        await self._frame(self.channels[channel_id].writer, channel_id, payload)

    async def close_channel(self, channel_id: int) -> None:
        channel = self.channels[channel_id]
        if not channel.closed_by_daemon:
            channel.closed_by_daemon = True
            await self._frame(channel.writer, channel_id, b"")

    async def _frame(self, writer: asyncio.StreamWriter, channel_id: int, payload: bytes) -> None:
        writer.write(struct.pack(">II", 4 + len(payload), channel_id) + payload)
        await writer.drain()

    async def _pump(self, writer: asyncio.StreamWriter, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        with contextlib.suppress(ConnectionError, asyncio.CancelledError):
            while (event := await queue.get()) is not None:
                await self._send(writer, {"jsonrpc": "2.0", "method": "event", "params": event})

    async def _read(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        length, channel = struct.unpack(">II", await reader.readexactly(8))
        return channel, await reader.readexactly(length - 4)

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        payload = json.dumps(message).encode()
        writer.write(struct.pack(">II", 4 + len(payload), 0) + payload)
        await writer.drain()

    # -- the methods -------------------------------------------------------------------------

    def _group(self, gid: str) -> Group:
        group = self.groups.get(gid)
        if group is None:
            raise _Fail(1001, f"no such group {gid}")
        return group

    def _tab(self, params: dict[str, Any]) -> tuple[Group, Tab]:
        tid = str(params.get("tab_id") or "")
        for group in self.groups.values():
            for tab in group.tabs:
                if tab.id == tid:
                    return group, tab
        raise _Fail(1104, f"no tab {tid}", {"tab_id": tid})

    async def _held_back(self, group: Group, params: dict[str, Any]) -> None:
        """The contract's control rule for an agent's call: a person driving holds it back up to
        ``wait_ms``, a pause refuses it at once. An operator's call is never held."""
        origin = params.get("origin") or {}
        if origin.get("actor") == "operator":
            return
        deadline = time.monotonic() + min(int(origin.get("wait_ms", 20_000)), 60_000) / 1000
        while group.control["owner"] == "human":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Fail(1101, "the operator is driving this browser", {"owner": "human", "holder": group.control["holder"], "until": group.control["until"]})
            changed = self._control_changed
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), remaining)
        if group.control["owner"] == "paused":
            raise _Fail(1106, "the operator paused the agent in this browser", {"reason": group.control["reason"]})

    def _page_call(self, tab: Tab) -> None:
        if tab.dialog is not None:
            raise _Fail(1107, "a dialog is open", {"dialog": {"type": tab.dialog.get("type"), "message": tab.dialog.get("message")}})

    def _new_tab(self, group: Group, url: str) -> Tab:
        self._next["tab"] += 1
        tab = Tab(f"t{self._next['tab']}", group.id)
        tab.page = self._page_at(url)
        group.tabs.append(tab)
        group.active = tab.id
        self.emit("tab.created", {"group_id": group.id, "tab": tab.view(True)})
        return tab

    def _through_wall(self, group: Group, url: str) -> None:
        """The wall's check of a navigation, as the daemon makes it: ``1102`` with the verdict."""
        from urllib.parse import urlsplit  # noqa: PLC0415 — only navigations need it

        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if parts.scheme == "https" else 80)
        verdict = self.walled.get(host)
        if verdict is None or (group.browser_id, host, port) in self.grants:
            if host:
                self.emit("egress", {"browser_id": group.browser_id, "group_id": group.id, "host": host, "port": port, "decision": "allow", "at": stamp()})
            return
        decision, reason = verdict
        self.emit("egress", {"browser_id": group.browser_id, "group_id": group.id, "host": host, "port": port, "decision": decision, "reason": reason, "at": stamp()})
        raise _Fail(1102, f"the network wall refused {host}:{port}: {reason}", {"host": host, "port": port, "decision": decision, "reason": reason})

    def _page_at(self, url: str) -> Page:
        known = self.pages.get(url)
        if known is not None:
            return Page(url=known.url, title=known.title, text=known.text, elements=dict(known.elements), heading=known.heading, logs=list(known.logs),
                        requests=[dict(r) for r in known.requests], inspected=dict(known.inspected))
        return Page(url=url or "about:blank")

    async def _handle(self, method: str, params: dict[str, Any]) -> Any:  # noqa: C901 — one dispatcher, as the daemon's
        if method in self.fail:
            code, text, data = self.fail.pop(method)
            raise _Fail(code, text, data)
        if method == "daemon.info":
            return {"version": "fake", "protocol": self.protocol, "instance": self.instance, "env": self.env, "os": "linux", "arch": "amd64", "pid": 1, "started_at": stamp(), "uptime_s": 1,
                    "chromium": {"path": "/fake/chrome", "version": "151.0.0.0", "kind": "bundled"}, "capabilities": {"sandbox": "ok", "headed": False, "screencast": True},
                    "limits": {"max_browsers": self.max_browsers, "max_tabs_per_group": 8}, "counts": {"browsers": len(self.browsers), "groups": len(self.groups), "tabs": sum(len(g.tabs) for g in self.groups.values()), "viewers": len(self.channels)},
                    "machine": self.machine}
        if method == "events.unsubscribe":
            return {}
        if method == "browser.open":
            return self._open(params)
        if method == "browser.list":
            return {"browsers": [b.view(list(self.groups.values())) for b in self.browsers.values()]}
        if method == "browser.close":
            bid = str(params.get("browser_id"))
            if bid not in self.browsers:
                raise _Fail(1001, "no such browser")
            groups = [g for g in self.groups if self.groups[g].browser_id == bid]
            self.crash(bid, "closed")
            return {"groups": groups}
        if method == "group.list":
            return {"groups": [g.view() for g in self.groups.values() if not params.get("browser_id") or g.browser_id == params["browser_id"]]}
        if method == "group.resize":
            group = self._group(str(params.get("group_id")))
            viewport = params.get("viewport") or {}
            w, h = int(viewport.get("w") or 0), int(viewport.get("h") or 0)
            if not (320 <= w <= 3840 and 320 <= h <= 3840):
                raise _Fail(-32602, "viewport sides must be 320-3840")
            group.viewport = {"w": w, "h": h}
            return {"viewport": {"w": w, "h": h}}
        if method == "group.close":
            group = self._group(str(params.get("group_id")))
            del self.groups[group.id]
            self.emit("group.closed", {"group_id": group.id, "browser_id": group.browser_id, "profile": group.profile, "labels": group.labels})
            if not any(g.browser_id == group.browser_id for g in self.groups.values()) and group.browser_id in self.browsers:
                self.crash(group.browser_id, "closed")
            return {"tabs": len(group.tabs)}
        if method == "profile.list":
            return {"profiles": [{"id": p, "size_bytes": 2 << 20, "last_used_at": stamp(), "running": any(b.profile == p for b in self.browsers.values())} for p in self.profiles]}
        if method in ("profile.clear", "profile.delete"):
            profile = str(params.get("profile"))
            if profile not in self.profiles:
                raise _Fail(1001, "no such profile")
            if any(b.profile == profile for b in self.browsers.values()):
                raise _Fail(1004, "the profile's browser is running")
            if method == "profile.delete":
                del self.profiles[profile]
            return {}
        if method == "tab.list":
            group = self._group(str(params.get("group_id")))
            return {"tabs": [t.view(t.id == group.active) for t in group.tabs], "active_tab": group.active}
        if method == "tab.new":
            group = self._group(str(params.get("group_id")))
            await self._held_back(group, params)
            if len(group.tabs) >= 8:
                raise _Fail(1003, "a group has at most 8 tabs", {"limit": "tabs", "max": 8})
            self._through_wall(group, str(params.get("url") or "about:blank"))
            return self._new_tab(group, str(params.get("url") or "about:blank")).view(True)
        if method == "tab.select":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            group.active = tab.id
            return tab.view(True)
        if method == "tab.close":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            group.tabs.remove(tab)
            if group.active == tab.id:
                group.active = group.tabs[-1].id if group.tabs else ""
            self.emit("tab.closed", {"group_id": group.id, "tab_id": tab.id})
            return {}
        if method == "page.navigate":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            self._page_call(tab)
            url = str(params.get("url") or "")
            if url.split(":", 1)[0] not in ("http", "https", "about"):
                raise _Fail(1004, f"{url.split(':', 1)[0]}: URLs are not opened", {"reason": "scheme"})
            self._through_wall(group, url)
            tab.history.append(tab.page)
            tab.page = self._page_at(url)
            tab.loaded(tab.page)
            self.emit("tab.updated", {"group_id": group.id, "tab_id": tab.id, "url": tab.page.url, "title": tab.page.title, "favicon_url": "", "loading": False})
            reply: dict[str, Any] = {"url": tab.page.url, "title": tab.page.title, "status": 200}
            errors = sum(1 for e in tab.page.logs if e.get("level") == "error")
            warnings = sum(1 for e in tab.page.logs if e.get("level") == "warning")
            if errors or warnings:
                reply["logged"] = {"errors": errors, "warnings": warnings}
            return reply
        if method in ("page.back", "page.forward", "page.reload"):
            group, tab = self._tab(params)
            await self._held_back(group, params)
            if method == "page.back" and tab.history:
                tab.page = tab.history.pop()
            return {"url": tab.page.url, "title": tab.page.title}
        if method == "page.snapshot":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            self._page_call(tab)
            text = tab.page.outline()
            limit = int(params.get("max_chars") or 40_000)
            return {"url": tab.page.url, "title": tab.page.title, "text": text[:limit], "refs": len(tab.page.elements), "truncated": len(text) > limit, "frames": []}
        if method == "page.text":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            self._page_call(tab)
            limit = int(params.get("max_chars") or 40_000)
            return {"url": tab.page.url, "title": tab.page.title, "text": tab.page.text[:limit], "truncated": len(tab.page.text) > limit}
        if method == "page.screenshot":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            self._page_call(tab)
            masked = [e.ref for e in tab.page.elements.values() if e.secret]
            return {"format": "jpeg", "width": 1280, "height": 800, "data_b64": base64.b64encode(self.screenshot).decode(), "masked": masked}
        if method == "page.act":
            return await self._act(params)
        if method == "page.find" and not self.legacy:
            group, tab = self._tab(params)
            await self._held_back(group, params)
            self._page_call(tab)
            return self._find(tab.page, str(params.get("query") or ""), regex=bool(params.get("regex")), limit=int(params.get("max") or 20))
        if method == "page.wait":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            wanted = str(params.get("for"))
            value = str(params.get("value") or "")
            matched = wanted in ("load", "idle") or (wanted == "text" and value in tab.page.text + tab.page.outline()) or (wanted == "url" and value in tab.page.url)
            return {"matched": wanted if matched else "timeout", "url": tab.page.url}
        if method == "dialog.answer":
            group, tab = self._tab(params)
            if tab.dialog is None:
                raise _Fail(1001, "no dialog is open")
            tab.dialog = None
            self.emit("dialog.closed", {"group_id": group.id, "tab_id": tab.id})
            return {}
        if method == "page.logs":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            self._page_call(tab)
            levels = {"error": 3, "warning": 2, "info": 1, "debug": 0}
            floor = levels.get(str(params.get("level") or "info"))
            if floor is None:
                raise _Fail(-32602, "level is error, warning, info or debug")
            after = int(params.get("after") or 0)
            if after > tab.log_seq:
                after = 0
            limit = int(params.get("limit") or 100)
            matched = [e for e in tab.logs if e["seq"] > after and levels.get(e["level"], 0) >= floor]
            shown = matched[:limit]
            return {"url": tab.page.url, "entries": shown, "last": shown[-1]["seq"] if len(matched) > limit else tab.log_seq, "more": len(matched) - len(shown), "dropped": False}
        if method == "page.network":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            self._page_call(tab)
            after = int(params.get("after") or 0)
            if after > tab.request_seq:
                after = 0
            types = [str(t) for t in params.get("types") or []]
            api = {"xhr", "fetch", "websocket", "eventsource"}

            def match(r: dict[str, Any]) -> bool:
                if types and not any(t == r.get("type") or (t == "api" and r.get("type") in api) for t in types):
                    return False
                if params.get("host") and str(params["host"]) not in str(r.get("url")):
                    return False
                if params.get("contains") and str(params["contains"]).lower() not in str(r.get("url")).lower():
                    return False
                if params.get("method") and str(params["method"]).upper() != r.get("method"):
                    return False
                return not params.get("failed") or bool(r.get("failed")) or int(r.get("status") or 0) >= 400

            matched = [r for r in tab.requests if r["seq"] > after and match(r)]
            limit = int(params.get("limit") or 50)
            shown = matched[-limit:]
            return {"url": tab.page.url, "requests": [{k: v for k, v in r.items() if k not in ("body", "request_headers", "response_headers")} for r in shown],
                    "last": tab.request_seq, "skipped": len(matched) - len(shown), "dropped": False, "total": len(tab.requests)}
        if method == "page.request":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            self._page_call(tab)
            found = next((r for r in tab.requests if r["id"] == params.get("id")), None)
            if found is None:
                raise _Fail(1001, f"no request {params.get('id')} on this tab")
            out: dict[str, Any] = {"request": {k: v for k, v in found.items() if k != "body"}}
            if params.get("body"):
                if "body" in found:
                    limit = int(params.get("max_chars") or 20_000)
                    out["body"] = found["body"][:limit]
                    out["truncated"] = len(found["body"]) > limit
                else:
                    out["body_error"] = "the response is image/png, not text"
            return out
        if method == "page.inspect":
            group, tab = self._tab(params)
            await self._held_back(group, params)
            self._page_call(tab)
            key = str(params.get("ref") or params.get("selector") or "")
            if key not in tab.page.inspected:
                if params.get("selector"):
                    raise _Fail(1001, f"no element of the page matches {key}")
                raise _Fail(1103, f"{key} is not on the page any more", {"ref": key})
            return {"ref": key, **tab.page.inspected[key]}
        if method == "download.list":
            gid = str(params.get("group_id"))
            return {"downloads": [dict(d) for d in self.downloads.values() if d["group_id"] == gid]}
        if method == "download.read":
            did = str(params.get("id"))
            if did not in self.download_bytes:
                raise _Fail(1001, "no such download")
            data = self.download_bytes[did]
            offset = int(params.get("offset") or 0)
            piece = data[offset : offset + int(params.get("max") or 512 << 10)]
            return {"data_b64": base64.b64encode(piece).decode(), "offset": offset, "size": len(data), "eof": offset + len(piece) >= len(data)}
        if method == "download.delete":
            self.downloads.pop(str(params.get("id")), None)
            return {}
        if method == "upload.put":
            upload_id = str(params.get("upload_id") or "")
            data = base64.b64decode(params.get("data_b64") or "")
            if not upload_id:
                self._next["upload"] += 1
                upload_id = f"u{self._next['upload']}"
                self.uploads[upload_id] = {"group_id": params.get("group_id"), "name": params.get("name"), "data": b""}
            upload = self.uploads[upload_id]
            if int(params.get("offset") or 0) != len(upload["data"]):
                raise _Fail(-32602, "offset is not the size so far")
            upload["data"] += data
            return {"upload_id": upload_id, "size": len(upload["data"])}
        if method == "net.configure":
            self.wall = dict(params)
            return {}
        if method == "limits.set":
            self.limits = dict(params)
            return {"max_browsers": params.get("max_browsers", self.max_browsers)}
        if method == "record.set":
            group = self._group(str(params.get("group_id")))
            was = self.recording.get(group.id, {}).get("frames")
            self.recording[group.id] = {"group_id": group.id, "frames": bool(params.get("frames")), "human": bool(params.get("human"))}
            if params.get("frames") and not was:
                self.keyframe(group.id, "start")
            return dict(self.recording[group.id])
        if method == "record.list":
            gid = str(params.get("group_id"))
            after = int(params.get("after") or 0)
            listed = [{k: v for k, v in f.items() if k != "data"} for f in self.frames.get(gid, []) if f["no"] > after]
            return {"recording": self.recording.get(gid, {"group_id": gid, "frames": False, "human": False}), "frames": listed[: int(params.get("limit") or 500)]}
        if method == "record.read":
            gid = str(params.get("group_id"))
            found = next((f for f in self.frames.get(gid, []) if f["no"] == int(params.get("no") or 0)), None)
            if found is None:
                raise _Fail(1001, "no such keyframe")
            return {"frame": {k: v for k, v in found.items() if k != "data"}, "data_b64": base64.b64encode(found["data"]).decode()}
        if method == "record.delete":
            self.frames.pop(str(params.get("group_id")), None)
            return {}
        if method == "record.groups":
            groups = [{"group_id": g, "frames": len(f), "bytes": sum(x["bytes"] for x in f), "first_at": f[0]["at"], "last_at": f[-1]["at"]} for g, f in self.frames.items() if f]
            return {"groups": groups, "bytes": sum(g["bytes"] for g in groups), "max_bytes": int(self.limits.get("record_max_bytes") or 500 << 20), "retention_ms": int(self.limits.get("record_retention_ms") or 7 * 86_400_000)}
        if method in ("net.grant", "net.revoke"):
            group = self._group(str(params.get("group_id")))
            key = (group.browser_id, str(params.get("host")), int(params.get("port") or 0))
            if method == "net.grant":
                self.grants.add(key)
            else:
                self.grants.discard(key)
            return {}
        if method == "workflow.start":
            group = self._group(str(params.get("group_id")))
            if group.control["owner"] != "human":
                raise _Fail(1004, "take the browser first: a recording is of a person's own steps")
            if params.get("values", "slots") not in ("slots", "literal"):
                raise _Fail(-32602, "values must be slots or literal")
            current = self.workflows.get(group.id)
            if current and current["state"] == "recording":
                return copy.deepcopy(current)
            tab = self.tab_of(group.id)
            self.workflows[group.id] = workflow = {"id": f"w{secrets.token_hex(4)}", "group_id": group.id, "values": params.get("values") or "slots", "state": "recording",
                                                   "started_at": int(time.time() * 1000), "start_url": tab.page.url, "start_title": tab.page.title, "steps": []}
            self.emit("workflow.started", {"group_id": group.id, "workflow": {**copy.deepcopy(workflow), "steps": None}})
            return copy.deepcopy(workflow)
        if method == "workflow.stop":
            gid = str(params.get("group_id"))
            if self.workflows.get(gid, {}).get("state") != "recording":
                raise _Fail(1001, f"group {gid!r} is not recording")
            return self.stop_workflow(gid, "operator")
        if method == "workflow.get":
            found = self.workflows.get(str(params.get("group_id")))
            return {"workflow": copy.deepcopy(found) if found else None}
        if method == "workflow.mark":
            gid = str(params.get("group_id"))
            if self.workflows.get(gid, {}).get("state") != "recording":
                raise _Fail(1001, f"group {gid!r} is not recording")
            return {"step": self.record_step(gid, action="expect", title=self.tab_of(gid).page.heading or self.tab_of(gid).page.title)}
        if method == "control.set":
            group = self._group(str(params.get("group_id")))
            owner = str(params.get("owner"))
            holder = str(params.get("client_id") or "") or None
            self.set_control(group.id, owner, holder, str(params.get("reason") or ""))
            return dict(group.control)
        if method == "view.detach":
            channel = self.channels.get(int(params.get("channel") or 0))
            if channel is not None:
                await self.close_channel(channel.id)
            return {}
        if method == "browser.stats":
            return {"at": stamp(), "supported": True, "memory_basis": "cgroup", "machine": self.machine, "daemon": {"pid": 1, "rss_bytes": 20 << 20, "cpu_percent": 0.2},
                    "browsers": [{"id": b.id, "pid": b.pid, "processes": 6, "rss_bytes": 250 << 20, "cpu_percent": 5.0, "tabs": sum(len(g.tabs) for g in self.groups.values() if g.browser_id == b.id)} for b in self.browsers.values()]}
        raise _Fail(-32601, f"method not found: {method}")

    def _open(self, params: dict[str, Any]) -> dict[str, Any]:
        gid = str(params.get("group_id"))
        profile = str(params.get("profile"))
        if gid in self.groups:
            group = self.groups[gid]
            return {"group": group.view(), "tab": self.tab_of(gid).view(True), "created": False}
        browser = next((b for b in self.browsers.values() if b.profile == profile), None)
        if browser is None:
            if len(self.browsers) >= self.max_browsers:
                raise _Fail(1003, f"{len(self.browsers)} browsers already run", {"limit": "browsers", "max": self.max_browsers})
            self._next["browser"] += 1
            browser = Browser(f"b{self._next['browser']:08x}", profile)
            self.browsers[browser.id] = browser
            self.profiles.setdefault(profile, {})
            self.emit("browser.started", {"browser_id": browser.id, "profile": profile, "pid": browser.pid, "chromium_version": "151.0.0.0"})
        group = Group(gid, browser.id, profile, dict(params.get("labels") or {}), dict(params.get("viewport") or {"w": 1280, "h": 800}))
        self.groups[gid] = group
        self.emit("group.opened", {"group_id": gid, "browser_id": browser.id, "profile": profile, "labels": group.labels})
        tab = self._new_tab(group, str(params.get("url") or "about:blank"))
        return {"group": group.view(), "tab": tab.view(True), "created": True}

    def _sensitive(self, group: Group, tab: Tab, element: Element, action: str, keys: str = "") -> dict[str, Any]:
        kinds: list[str] = []
        words: list[str] = []
        name = element.name.lower()
        fields = [e for e in tab.page.elements.values() if e.secret]
        # A submit, or Enter, in a form with a secret field sends it.
        if fields and ((action == "click" and element.role == "button") or (action == "press" and "Enter" in keys)):
            kinds.append("credentials")
        for kind, table in SENSITIVE_WORDS.items():
            hit = [w for w in table if w in name]
            if hit and action in ("click", "double_click", "press"):
                kinds.append(kind)
                words += hit
        if action == "upload" or element.type == "file":
            kinds.append("upload")
        page_origin = "/".join(tab.page.url.split("/")[:3]) if "://" in tab.page.url else tab.page.url
        if element.form_action and "/".join(element.form_action.split("/")[:3]) != page_origin:
            kinds.append("cross_origin_post")
        # Every field of the form, as the daemon lists them, not only the secret ones.
        listed = [{"type": e.type or ("textarea" if e.tag == "textarea" else "text"), "name": e.name, "autocomplete": e.autocomplete} for e in tab.page.elements.values() if e.tag in ("input", "textarea", "select")]
        return {"kinds": kinds, "evidence": {"name": element.name, "role": element.role, "words": words, "page_origin": page_origin, "fields": listed}}

    @staticmethod
    def _find(page: Page, query: str, *, regex: bool, limit: int) -> dict[str, Any]:
        """``page.find``: the page's text and its elements' names searched, each hit with its ref."""
        pattern = re.compile(query if regex else re.escape(query), re.IGNORECASE)
        matches: list[dict[str, Any]] = []
        for element in page.elements.values():
            if not element.secret and pattern.search(element.name):
                matches.append({"text": element.name, "in": {"ref": element.ref, "role": element.role, "name": element.name}})
        for line in page.text.splitlines():
            if pattern.search(line):
                near = [{"ref": e.ref, "role": e.role, "name": e.name} for e in list(page.elements.values())[:2]]
                matches.append({"text": line.strip()[:200], "near": near})
        shown = matches[:limit]
        lines = []
        for m in shown:
            where = f" [in {m['in']['role']} \"{m['in']['name']}\" ref={m['in']['ref']}]" if m.get("in") else "".join(f" [near {n['role']} \"{n['name']}\" ref={n['ref']}]" for n in m.get("near") or [])
            lines.append(f"- \"{m['text']}\"{where}")
        return {"url": page.url, "matches": shown, "total": len(matches), "text": "\n".join(lines)}

    async def _act(self, params: dict[str, Any]) -> dict[str, Any]:
        group, tab = self._tab(params)
        await self._held_back(group, params)
        self._page_call(tab)
        action = str(params.get("action"))
        if self.legacy:
            unknown = sorted(set(params) & {"x", "y", "allow_point"})
            if unknown:
                raise _Fail(-32602, f'params: json: unknown field "{unknown[0]}"')
            if action == "scroll" and not params.get("ref") and params.get("direction") not in ("up", "down"):
                raise _Fail(-32602, "scroll needs a ref, or direction up or down")
        ref = str(params.get("ref") or "")
        pointed = "x" in params
        if pointed:
            if not params.get("allow_point"):
                raise _Fail(1004, "acting at a point is off for this session; act on a ref from a snapshot")
            x, y = float(params["x"]), float(params["y"])
            hit = [e for e in tab.page.elements.values() if e.box["x"] <= x < e.box["x"] + e.box["w"] and e.box["y"] <= y < e.box["y"] + e.box["h"]]
            if not hit:
                raise _Fail(1001, "nothing that can be acted on is at that point")
            ref = hit[-1].ref
        element = tab.page.elements.get(ref) if ref else None
        if ref and element is None:
            raise _Fail(1103, f"{ref} is not on the page any more", {"ref": ref})
        box = dict(element.box) if element is not None else {"x": 0.0, "y": 0.0, "w": 1280.0, "h": 800.0}
        point = {"x": box["x"] + box["w"] / 2, "y": box["y"] + box["h"] / 2}
        sensitive = self._sensitive(group, tab, element, action, str(params.get("keys") or "")) if element is not None else {"kinds": [], "evidence": {}}
        described = {"role": element.role, "name": element.name, "tag": element.tag, "type": element.type, "autocomplete": element.autocomplete, "href": element.href, "form_action": element.form_action,
                     "secret": element.secret, "secret_kind": element.field_kind() if element.secret else "", "disabled": False, "checked": element.checked, "file": element.type == "file", "select": element.tag == "select"} if element is not None else {}
        if params.get("dry_run"):
            return {"action_id": "", "ok": True, "effects": {}, "element": described, "point": point, "box": box, "sensitive": sensitive, **({"ref": ref} if pointed else {})}
        keys = str(params.get("keys") or "")
        typing = action in ("type", "select") or (action == "press" and keys not in ("Enter", "Tab"))
        handed_over = action == "type" and bool(params.get("operator_secret"))
        if element is not None and typing and not handed_over and (element.secret or element.ref in self.human_typed):
            self.emit("needs_you", {"group_id": group.id, "tab_id": tab.id, "reason": "field_forbidden", "what": f"type into {element.name}", "url": tab.page.url, "by": "daemon"})
            raise _Fail(1105, "this is a secret field", {"ref": ref, "field": element.field_kind()})
        if element is not None and element.covered_by and action in ("click", "double_click", "right_click", "check", "uncheck"):
            raise _Fail(1004, f"{ref} is covered by {element.covered_by}", {"ref": ref, "covered_by": element.covered_by})
        self._next["action"] += 1
        action_id = f"a{self._next['action']}"
        text = str(params.get("text") or "")
        event = {"action_id": action_id, "group_id": group.id, "tab_id": tab.id, "actor": (params.get("origin") or {}).get("actor", "agent"), "kind": action, "point": point, "box": box,
                 "name": element.name if element else "", "element": params.get("element") or "", "at": stamp()}
        if action == "type":
            event["text_len"] = len(text)
        if params.get("keys"):
            event["keys"] = params["keys"]
        self.emit("action", event)
        effects: dict[str, Any] = {}
        before = tab.page.outline()
        if action == "type" and element is not None:
            element.value = text
        if action in ("check", "uncheck") and element is not None:
            if element.checked == (action == "check"):
                effects["unchanged"] = True
            element.checked = action == "check"
        if action == "upload":
            names = [self.uploads[u]["name"] for u in params.get("upload_ids") or [] if u in self.uploads]
            effects["uploaded"] = names
        if action == "scroll":
            wanted = str(params.get("text") or "").strip()
            if wanted:
                found = next((e.ref for e in tab.page.elements.values() if wanted.lower() in e.name.lower()), "")
                if not found and wanted.lower() not in tab.page.text.lower():
                    raise _Fail(1001, f"no visible text on the page holds {wanted!r}")
                effects["found"] = found or "e0"
            elif params.get("direction") and ref:
                effects["scrolled"] = {"by": 0 if tab.page.heading == "End" else 480, "at_end": tab.page.heading == "End", "pane": ref}
            effects["scroll"] = {"top": 800, "height": 4000, "view": 800, "above": 1.0, "below": 3.0}
        if action in ("click", "double_click") and element is not None:
            if element.opens_dialog is not None:
                tab.dialog = dict(element.opens_dialog)
                effects["dialog"] = {"type": tab.dialog.get("type"), "message": tab.dialog.get("message")}
                self.emit("dialog.opened", {"group_id": group.id, "tab_id": tab.id, **effects["dialog"]})
            if element.goes_to:
                tab.history.append(tab.page)
                tab.page = self._page_at(element.goes_to)
                effects.update({"navigated": True, "url": tab.page.url})
                self.emit("tab.updated", {"group_id": group.id, "tab_id": tab.id, "url": tab.page.url, "title": tab.page.title, "favicon_url": "", "loading": False})
            if element.downloads is not None:
                download = self.add_download(group.id, *element.downloads)
                effects["download"] = {"id": download["id"], "name": download["name"], "size": download["size"]}
            if element.alerts:
                effects["dialogs_auto"] = [self.auto_dialog(group.id, "alert", element.alerts)]
            if element.logs:
                for entry in element.logs:
                    tab.log(entry)
                errors = sum(1 for e in element.logs if e.get("level") == "error")
                warnings = sum(1 for e in element.logs if e.get("level") == "warning")
                if errors or warnings:
                    effects["logged"] = {"errors": errors, "warnings": warnings}
        self.emit("action_done", {"action_id": action_id, "group_id": group.id, "tab_id": tab.id, "ok": True, "effects": effects})
        self.keyframe(group.id, "action", action_id)
        reply: dict[str, Any] = {"action_id": action_id, "ok": True, "effects": effects, "point": point, "box": box, "element": described, "sensitive": sensitive}
        if pointed:
            reply["ref"] = ref
        if not effects.get("navigated"):
            # What the action changed in the outline, as lines that came and went; none after a navigation.
            old_lines, new_lines = before.splitlines(), tab.page.outline().splitlines()
            changed = [f"- {line}" for line in old_lines if line not in new_lines] + [f"+ {line}" for line in new_lines if line not in old_lines]
            if changed:
                reply["diff"] = "\n".join(changed)[:2000]
        return reply


__all__ = ["Element", "FakeBrowserd", "Page", "ViewChannel"]
