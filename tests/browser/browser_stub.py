"""A browser service for the browser checks: the REST routes and the live view's WebSocket, scripted.

The app's Browser tab, its corner preview and its phone button speak to the host's browser routes and
to ``/ws/browsers/{group}``, which the host relays from the browser daemon. Neither runs here. This
answers ``/api/browsers*`` the way the host does and serves the socket through
``page.route_web_socket``, as the daemon would: ``hello``, ``tabs`` and ``viewers`` on ATTACH, then
JPEG frames, one at a time, the next only after the client acknowledged the last (newest wins), and
the agent's ``action`` events with the point and box of the element it acts on.

The frames are real pictures: ``render_scenes`` renders the pages of ``browser_pages.py`` in the
check's own Chromium once, before the app is opened, and keeps each as a JPEG with the boxes of its
named elements. An action replayed on "the Add to cart button" therefore lands on the button the
picture shows, and a check can say where the app must have drawn the cursor.

A copy is answered with the group's ``selection``, as the daemon answers it with the page's.

Everything the page sends is recorded per connection (ATTACH, ACK, VIEW, INPUT), and every request
to the REST routes, so a check can say which socket sent which input and what a give-back carried.
"""

from __future__ import annotations

import io
import json
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from browser_pages import SCENES

FRAME, EVENT, ATTACH, ACK, VIEW, INPUT = 0x21, 0x22, 0x30, 0x31, 0x32, 0x33
VIEWPORT = (1280, 800)


@dataclass
class Scene:
    name: str
    url: str
    title: str
    favicon: str
    jpeg: bytes
    thumb: bytes
    size: tuple[int, int]
    thumb_size: tuple[int, int]
    boxes: dict[str, dict[str, float]]
    sizes: dict[tuple[int, int], bytes] = field(default_factory=dict)
    """The same page rendered at other viewport sizes (``render_scenes(sizes=...)``), for a picture of
    a page the app asked to take another shape: a phone driving it at its own width."""


def render_scenes(browser: Any, quality: int = 72, sizes: tuple[tuple[int, int], ...] = ()) -> dict[str, Scene]:
    """Every page of ``browser_pages.SCENES`` as a 1280×800 JPEG, a 320×200 one, and its elements' boxes;
    with ``sizes``, also as a JPEG at each of those viewport sizes."""
    context = browser.new_context(viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]}, device_scale_factor=1)
    page = context.new_page()
    out: dict[str, Scene] = {}
    try:
        for name, (url, title, favicon, html, selectors) in SCENES.items():
            page.set_content(html)
            page.wait_for_timeout(50)
            boxes = {}
            for key, selector in selectors.items():
                b = page.locator(selector).bounding_box()
                if b:
                    boxes[key] = {"x": round(b["x"], 1), "y": round(b["y"], 1), "w": round(b["width"], 1), "h": round(b["height"], 1)}
            jpeg = page.screenshot(type="jpeg", quality=quality)
            thumb, thumb_size = _thumbnail(jpeg)
            out[name] = Scene(name, url, title, favicon, jpeg, thumb, VIEWPORT, thumb_size, boxes)
        for w, h in sizes:
            page.set_viewport_size({"width": w, "height": h})
            for name, (_url, _title, _favicon, html, _selectors) in SCENES.items():
                page.set_content(html)
                page.wait_for_timeout(50)
                out[name].sizes[(w, h)] = page.screenshot(type="jpeg", quality=quality)
    finally:
        context.close()
    return out


def _thumbnail(jpeg: bytes) -> tuple[bytes, tuple[int, int]]:
    """The daemon's thumbnail tier: 320×200 at quality 45. Without Pillow the full frame stands in."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - a harness without Pillow still runs, with bigger thumbnails
        return jpeg, VIEWPORT
    image = Image.open(io.BytesIO(jpeg)).convert("RGB").resize((320, 200), Image.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=45)
    return buf.getvalue(), (320, 200)


def enc_frame(frame_no: int, meta: dict[str, Any], image: bytes) -> bytes:
    body = json.dumps(meta, separators=(",", ":")).encode()
    return bytes([FRAME]) + struct.pack(">IH", frame_no, len(body)) + body + image


def enc_event(body: dict[str, Any]) -> bytes:
    return bytes([EVENT]) + json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def decode(frame: bytes) -> tuple[str, Any]:
    kind, body = frame[0], frame[1:]
    if kind == ACK:
        return "ack", struct.unpack(">I", body)[0]
    names = {ATTACH: "attach", VIEW: "view", INPUT: "input"}
    if kind in names:
        return names[kind], json.loads(body.decode())
    raise ValueError(f"unexpected frame 0x{kind:02x}")


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


@dataclass
class Tab:
    id: str
    scene: str
    active: bool = True
    loading: bool = False


@dataclass
class Group:
    id: str
    owner_kind: str = "session"
    owner_id: str = ""
    owner_label: str = ""
    project_id: str | None = None
    status: str = "running"
    tabs: list[Tab] = field(default_factory=list)
    owner: str = "agent"
    holder: str | None = None
    reason: str = ""
    needs: dict[str, Any] | None = None
    acting: bool = False
    last_activity: float = field(default_factory=time.time)
    created: float = field(default_factory=time.time)
    actions: list[dict[str, Any]] = field(default_factory=list)
    dialog: dict[str, Any] | None = None
    recording: bool = False
    viewport: tuple[int, int] = VIEWPORT
    """The page's size: the default, or a size a scene was rendered at that the app asked for."""
    frames: list[dict[str, Any]] = field(default_factory=list)
    """Keyframes while recording: one when it starts and one after every action, each the picture of
    the scene on screen then (``scene`` names it; the REST answer leaves it out)."""
    selection: str = ""
    """What the page has selected, which a copy (INPUT ``copy``) answers with."""
    selection_withheld: bool = False
    """The focus is on a password field: a copy answers ``withheld`` and no text, as the daemon does."""
    workflow: dict[str, Any] | None = None
    """The recording of the operator's steps on now (``/api/browsers/{group}/workflow``)."""
    workflows: list[dict[str, Any]] = field(default_factory=list)
    """The finished ones the host keeps, newest first."""


@dataclass
class Client:
    n: int
    group: Group
    ws: Any
    read_only: bool
    label: str
    frames: list[tuple[str, Any]] = field(default_factory=list)
    attached: bool = False
    tier: str = "live"
    tab: str | None = None
    frame_no: int = 0
    inflight: int | None = None
    waiting: bytes | None = None
    waiting_no: int = 0
    closed: bool = False
    sent_frames: int = 0

    @property
    def id(self) -> str:
        return f"c{self.n}"

    def of(self, kind: str) -> list[Any]:
        return [f for k, f in self.frames if k == kind]


class BrowserStub:
    """The browser groups of an invented agent, their REST routes, and their live views."""

    def __init__(self, scenes: dict[str, Scene]) -> None:
        self.scenes = scenes
        self.groups: dict[str, Group] = {}
        self.clients: list[Client] = []
        self.requests: list[tuple[str, str, Any]] = []
        self.tickets: list[dict[str, Any]] = []
        self.lock = threading.RLock()
        self.counter = 0
        self.action_counter = 0
        self.available = True
        self.running: list[dict[str, Any]] = []
        """The browsers Settings lists as running (``/api/browsers/running``)."""
        self.profiles: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []
        """The site notes agents proposed (``/api/browsers/notes``)."""
        self.envs: list[dict[str, Any]] = [{
            "env": "container", "configured": True, "available": True, "reason": "", "detail": "", "version": "src-4f1c2a9e0b7d",
            "chromium": {"version": "Chrome/151.0.7922.34", "kind": "bundled", "error": ""}, "sandbox": "ok",
            "limits": {"max_browsers": 2}, "counts": {"browsers": 1}, "image_version": "", "update_available": False,
        }]

    # -- the groups -------------------------------------------------------------------------

    def add(self, id_: str, *, scene: str = "shop", owner_kind: str = "session", owner_id: str = "", owner_label: str = "", project_id: str | None = None, **fields: Any) -> Group:
        group = Group(id=id_, owner_kind=owner_kind, owner_id=owner_id, owner_label=owner_label, project_id=project_id, **fields)
        if not group.tabs:
            group.tabs = [Tab(id="t1", scene=scene)]
        self.groups[id_] = group
        return group

    def active(self, group: Group) -> Tab:
        return next((t for t in group.tabs if t.active), group.tabs[0])

    def tab_view(self, tab: Tab) -> dict[str, Any]:
        s = self.scenes[tab.scene]
        return {"id": tab.id, "url": s.url, "title": s.title, "favicon_url": s.favicon, "loading": tab.loading, "active": tab.active}

    def group_view(self, g: Group) -> dict[str, Any]:
        holder = next((c.label for c in self.clients if c.id == g.holder), g.holder)
        last = g.actions[-1] if g.actions else None
        return {
            "id": g.id,
            "owner": {"kind": g.owner_kind, "id": g.owner_id, "label": g.owner_label},
            "session_id": g.owner_id if g.owner_kind == "session" else None,
            "staff_id": g.owner_id if g.owner_kind == "staff" else None,
            "project_id": g.project_id,
            "profile": f"{g.owner_kind}-{g.owner_id}"[:64],
            "env": "container",
            "status": g.status,
            "viewport": {"w": g.viewport[0], "h": g.viewport[1]},
            "tabs": [self.tab_view(t) for t in g.tabs],
            "active_tab": self.active(g).id,
            "control": {"owner": g.owner, "holder": holder if g.owner == "human" else None, "until": None, "reason": g.reason},
            "needs_you": g.needs,
            "acting": g.acting,
            "last_action": {"kind": last["kind"], "element": last["element"], "at": last["at"]} if last else None,
            "created_at": iso(g.created),
            "last_activity_at": iso(g.last_activity),
        }

    # -- what the page is sent --------------------------------------------------------------

    def meta(self, client: Client, tab: Tab) -> tuple[dict[str, Any], bytes]:
        s = self.scenes[tab.scene]
        thumb = client.tier == "thumb"
        (w, h), image = (s.thumb_size, s.thumb) if thumb else (s.size, s.jpeg)
        vw, vh = VIEWPORT
        shaped = s.sizes.get(client.group.viewport)
        if shaped and not thumb:
            (w, h), image = client.group.viewport, shaped
            vw, vh = client.group.viewport
        meta = {"tab": tab.id, "tier": client.tier, "w": w, "h": h, "vw": vw, "vh": vh, "scroll_x": 0, "scroll_y": 0, "offset_top": 0, "page_scale": 1, "ts": int(time.time() * 1000)}
        return meta, image

    def viewed(self, client: Client) -> Tab:
        g = client.group
        return next((t for t in g.tabs if t.id == client.tab), None) or self.active(g)

    def send(self, client: Client, frame: bytes) -> None:
        if client.closed:
            return
        try:
            client.ws.send(frame)
        except Exception:  # noqa: BLE001 - a socket the page closed a moment ago
            client.closed = True

    def push_frame(self, client: Client) -> None:
        """A new picture for one client: sent at once when nothing is in flight, else it waits (newest wins)."""
        if not client.attached or client.closed:
            return
        meta, image = self.meta(client, self.viewed(client))
        client.frame_no += 1
        frame = enc_frame(client.frame_no, meta, image)
        if client.inflight is None:
            client.inflight = client.frame_no
            client.sent_frames += 1
            self.send(client, frame)
        else:
            client.waiting, client.waiting_no = frame, client.frame_no

    def paint(self, group_id: str) -> None:
        """The page repainted: every client of the group gets the new picture inside its window."""
        with self.lock:
            for c in self.live(group_id):
                self.push_frame(c)

    def broadcast(self, group_id: str, body: dict[str, Any] | None = None, per_client: Any = None) -> None:
        with self.lock:
            for c in self.live(group_id):
                if c.attached:
                    self.send(c, enc_event(per_client(c) if per_client else body))

    def control_for(self, client: Client) -> dict[str, Any]:
        g = client.group
        holder = None if g.owner != "human" or not g.holder else ("you" if g.holder == client.id else "other")
        return {"owner": g.owner, "holder": holder, "until": None, "reason": g.reason}

    def live(self, group_id: str) -> list[Client]:
        return [c for c in self.clients if c.group.id == group_id and not c.closed]

    # -- the script -------------------------------------------------------------------------

    def act(self, group_id: str, kind: str, element_key: str, *, name: str, element: str, scene: str | None = None, text_len: int | None = None, keys: str | None = None, text: str | None = None, after: str | None = None) -> dict[str, Any]:
        """The agent acts on ``element_key`` of the page on screen: ``action``, then the picture of
        ``after`` (the page the action led to), then ``action_done``. Returns the event."""
        with self.lock:
            g = self.groups[group_id]
            tab = self.active(g)
            s = self.scenes[scene or tab.scene]
            box = s.boxes[element_key]
            point = {"x": round(box["x"] + box["w"] / 2, 1), "y": round(box["y"] + box["h"] / 2, 1)}
            self.action_counter += 1
            now = time.time()
            event: dict[str, Any] = {"type": "action", "id": f"a{self.action_counter}", "group": g.id, "tab": tab.id, "actor": "agent", "kind": kind, "point": point, "box": box, "name": name, "element": element, "at": int(now * 1000)}
            if text_len is not None:
                event["text_len"] = text_len
            if keys:
                event["keys"] = keys
            g.acting = True
            g.last_activity = now
            row = {"id": event["id"], "at": iso(now), "actor": "agent", "kind": kind, "element": element, "name": name, "tab": tab.id, "point": point, "box": box, "ok": True, "action_id": event["id"]}
            if text is not None:
                row["text"] = text
            if text_len is not None:
                row["text_len"] = text_len
            if keys:
                row["keys"] = keys
            g.actions.append(row)
            self.broadcast(group_id, event)
            if after:
                tab.scene = after
                self.broadcast(group_id, per_client=lambda c: {"type": "tab", **{k: v for k, v in self.tab_view(tab).items() if k != "active"}})
            self.paint(group_id)
            self.broadcast(group_id, {"type": "action_done", "id": event["id"], "ok": True, "effects": {"navigated": bool(after)}})
            self.keyframe(g, "action", event["id"])
            return event

    def keyframe(self, g: Group, kind: str, action_id: str = "") -> None:
        """A recording's picture of what is on screen now, as the daemon takes one after an action."""
        if not g.recording:
            return
        tab = self.active(g)
        s = self.scenes[tab.scene]
        g.frames.append({"no": len(g.frames) + 1, "at": int(time.time() * 1000), "tab": tab.id, "url": s.url, "kind": kind, "action_id": action_id, "w": s.size[0], "h": s.size[1], "bytes": len(s.jpeg), "scene": tab.scene})

    def navigate(self, group_id: str, scene: str) -> None:
        with self.lock:
            g = self.groups[group_id]
            tab = self.active(g)
            tab.scene = scene
            now = time.time()
            self.action_counter += 1
            g.actions.append({"id": f"a{self.action_counter}", "at": iso(now), "actor": "agent", "kind": "navigate", "element": "", "name": "", "tab": tab.id, "url": self.scenes[scene].url, "ok": True})
            g.last_activity = now
            self.broadcast(group_id, per_client=lambda c: {"type": "tab", **{k: v for k, v in self.tab_view(tab).items() if k != "active"}})
            self.paint(group_id)

    def set_control(self, group_id: str, owner: str, holder: str | None = None, reason: str = "") -> None:
        with self.lock:
            g = self.groups[group_id]
            g.owner, g.holder, g.reason = owner, holder if owner == "human" else None, reason
            if owner == "human":
                g.needs = None
            g.last_activity = time.time()
            self.broadcast(group_id, per_client=lambda c: {"type": "control", "group": g.id, **self.control_for(c)})
            if owner != "human" and g.workflow:
                # The daemon ends a recording when the browser is no longer the person's.
                self.stop_workflow(group_id, "control")

    # -- the operator's recorded steps ---------------------------------------------------------

    def workflow_view(self, g: Group, w: dict[str, Any]) -> dict[str, Any]:
        return {"id": w["id"], "group_id": g.id, "env": "container", "project_id": g.project_id, "session_id": g.owner_id if g.owner_kind == "session" else None,
                "staff_id": g.owner_id if g.owner_kind == "staff" else None, "state": w["state"], "values": w["values"], "reason": w.get("reason", ""),
                "start_url": w["start_url"], "start_title": w["start_title"], "started_at": iso(w["started"]), "stopped_at": iso(w["stopped"]) if w.get("stopped") else None,
                "steps": list(w["steps"]), "note_id": w.get("note_id", "")}

    def record_step(self, group_id: str, **step: Any) -> dict[str, Any]:
        """The operator took a step: the daemon records it and tells the live views, as it does."""
        with self.lock:
            g = self.groups[group_id]
            w = g.workflow
            assert w is not None, "no recording is on"
            tab = self.active(g)
            full = {"n": len(w["steps"]) + 1, "at": int(time.time() * 1000), "tab": tab.id, "url": self.scenes[tab.scene].url, **step}
            w["steps"].append(full)
            self.broadcast(group_id, {"type": "workflow", "state": "step", "id": w["id"], "step": full, "steps": len(w["steps"]), "recording": True})
            return full

    def stop_workflow(self, group_id: str, reason: str) -> dict[str, Any]:
        with self.lock:
            g = self.groups[group_id]
            w = g.workflow
            assert w is not None
            w.update(state="stopped", reason=reason, stopped=time.time())
            g.workflow = None
            g.workflows.insert(0, w)
            self.broadcast(group_id, {"type": "workflow", "state": "stopped", "id": w["id"], "recording": False, "steps": len(w["steps"]), "reason": reason, "values": w["values"]})
            return self.workflow_view(g, w)

    def draft(self, workflow_id: str, goal: str) -> tuple[int, Any]:
        """A procedure from the steps, as the host writes one without a model."""
        g, w = next(((g, w) for g in self.groups.values() for w in g.workflows if w["id"] == workflow_id), (None, None))
        if g is None or w is None:
            return 404, {"detail": "no such recording"}
        lines = [f"{n}. {s['action']} {json.dumps((s.get('element') or {}).get('name', ''))}" for n, s in enumerate(w["steps"], start=1) if s["action"] not in ("arrive", "scroll")]
        now = time.time()
        note = {"id": f"n{len(self.notes) + 1}", "project_id": g.project_id or "", "project": "", "host": "shop.example", "status": "proposed", "by": "operator",
                "proposed_at": now, "approved_at": 0, "kind": "procedure", "title": goal or "Recorded steps on shop.example", "source": workflow_id,
                "text": "Recorded by the operator in the agent's browser on shop.example.\n\n" + "\n".join(lines)}
        self.notes.append(note)
        w["note_id"] = note["id"]
        return 200, {"note": note, "drafted_by": "steps", "why": ""}

    def needs_you(self, group_id: str, reason: str, what: str, by: str = "agent") -> None:
        """A request for the operator: the agent's handoff, or (``by="daemon"``) one the daemon raised
        itself, which may come with no sentence of its own."""
        with self.lock:
            g = self.groups[group_id]
            url = self.scenes[self.active(g).scene].url
            g.needs = {"reason": reason, "what": what, "url": url, "at": iso(time.time()), "by": by}
            g.owner, g.reason = "paused", reason
            g.last_activity = time.time()
            self.broadcast(group_id, {"type": "needs_you", "reason": reason, "what": what, "url": url, "by": by})
            self.broadcast(group_id, per_client=lambda c: {"type": "control", "group": g.id, **self.control_for(c)})

    def open_dialog(self, group_id: str, message: str, kind: str = "confirm") -> None:
        with self.lock:
            g = self.groups[group_id]
            g.dialog = {"type": "dialog", "state": "opened", "tab_id": self.active(g).id, "dialog_type": kind, "message": message}
            self.broadcast(group_id, g.dialog)

    def drop(self, group_id: str, code: int = 1012) -> None:
        """Every socket of the group goes away, as a host restart does it."""
        with self.lock:
            for c in self.live(group_id):
                c.closed = True
                try:
                    c.ws.close(code=code, reason="restart")
                except Exception:  # noqa: BLE001 - the page may have closed it first
                    pass

    def inputs(self, group_id: str, client: Client | None = None) -> list[dict[str, Any]]:
        return [f for c in self.clients if c.group.id == group_id and (client is None or c is client) for kind, f in c.frames if kind == "input"]

    # -- the socket ---------------------------------------------------------------------------

    def socket(self, ws: Any) -> None:
        """``page.route_web_socket("**/ws/browsers/**", stub.socket)``."""
        parts = urlsplit(ws.url)
        group_id = parts.path.rstrip("/").rsplit("/", 1)[-1]
        ticket = parse_qs(parts.query).get("ticket", [""])[0]
        with self.lock:
            g = self.groups.get(group_id)
            if g is None or g.status in ("closed", "lost"):
                ws.close(code=4404, reason="no such browser")
                return
            issued = next((t for t in self.tickets if t["ticket"] == ticket), None)
            self.counter += 1
            client = Client(n=self.counter, group=g, ws=ws, read_only=bool(issued and issued["read_only"]), label=f"viewer {self.counter}")
            self.clients.append(client)
        ws.on_message(lambda message: self.on_message(client, message))

    def on_message(self, client: Client, message: bytes | str) -> None:
        if isinstance(message, str):
            message = message.encode("latin-1")
        kind, body = decode(bytes(message))
        with self.lock:
            client.frames.append((kind, body))
            g = client.group
            if kind == "attach":
                client.tier = body.get("tier", "live")
                client.tab = body.get("tab")
                client.attached = True
                tab = self.viewed(client)
                self.send(client, enc_event({"type": "hello", "client_id": client.id, "read_only": client.read_only, "tier": client.tier, "group": {"id": g.id, "profile": f"{g.owner_kind}-{g.owner_id}", "viewport": {"w": VIEWPORT[0], "h": VIEWPORT[1]}}, "tab_id": tab.id, "control": self.control_for(client), "fps_cap": 15}))
                self.send(client, enc_event({"type": "tabs", "tabs": [self.tab_view(t) for t in g.tabs], "active": self.active(g).id}))
                others = [{"id": c.id, "kind": "viewer" if c.read_only else "human", "label": c.label} for c in self.live(g.id) if c is not client and c.attached]
                self.send(client, enc_event({"type": "viewers", "count": len(others) + 1, "others": others}))
                if g.needs:
                    self.send(client, enc_event({"type": "needs_you", "reason": g.needs["reason"], "what": g.needs["what"], "url": g.needs["url"]}))
                if g.dialog:
                    self.send(client, enc_event(g.dialog))
                self.push_frame(client)
            elif kind == "ack":
                if client.inflight == body:
                    client.inflight = None
                    if client.waiting is not None:
                        frame, no = client.waiting, client.waiting_no
                        client.waiting = None
                        client.inflight = no
                        client.sent_frames += 1
                        self.send(client, frame)
            elif kind == "view":
                client.tier = body.get("tier", client.tier)
                if body.get("tab"):
                    client.tab = body["tab"]
                self.push_frame(client)
            elif kind == "input":
                if client.read_only or g.owner != "human" or g.holder != client.id:
                    self.send(client, enc_event({"type": "error", "code": "not_holder", "message": "this client does not hold control"}))
                    return
                g.last_activity = time.time()
                if body.get("t") == "copy":
                    withheld = g.selection_withheld
                    self.send(client, enc_event({"type": "copied", "id": body.get("id", ""), "text": "" if withheld else g.selection, "truncated": False, "withheld": withheld}))
                if body.get("t") == "nav" and body.get("action") == "url":
                    target = next((n for n, s in self.scenes.items() if s.url == body.get("url")), None)
                    if target:
                        self.active(g).scene = target
                        tab = self.active(g)
                        self.broadcast(g.id, per_client=lambda c: {"type": "tab", **{k: v for k, v in self.tab_view(tab).items() if k != "active"}})
                        self.paint(g.id)

    # -- REST ---------------------------------------------------------------------------------

    def answer(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        segments = path.strip("/").split("/")  # api, browsers, [group], [action]
        if len(segments) >= 3 and segments[2] in ("running", "profiles", "recordings", "load", "notes", "workflows"):
            return self.answer_settings(method, segments[2:], body)
        if len(segments) == 2 and method == "GET":
            session = query.get("session", [None])[0]
            staff = query.get("staff", [None])[0]
            rows = [self.group_view(g) for g in self.groups.values()
                    if (session and g.owner_kind == "session" and g.owner_id == session) or (staff and g.owner_kind == "staff" and g.owner_id == staff)]
            if query.get("status"):
                rows = [self.group_view(g) for g in self.groups.values() if g.status != "closed"]
            return 200, {"available": self.available, "reason": "", "groups": rows, "envs": self.envs, "capacity": {"open": len(rows), "cap": 2, "queued": []}}
        g = self.groups.get(segments[2]) if len(segments) >= 3 else None
        if g is None:
            return 404, {"detail": "no such browser"}
        action = segments[3] if len(segments) > 3 else ""
        body = body or {}
        if action == "" and method == "GET":
            return 200, self.group_view(g)
        if action == "ticket" and method == "POST":
            ticket = {"ticket": f"bt-{g.id}-{len(self.tickets) + 1}", "tier": body.get("tier", "live"), "read_only": bool(body.get("read_only"))}
            self.tickets.append(ticket)
            return 200, {"ticket": ticket["ticket"], "expires_in": 30}
        if action == "viewport" and method == "POST":
            # The picture asks the page to become the pane. The harness's frames stay the scene's
            # size: the assertion is that the app asked, and for what.
            w, h = int(body.get("w") or 0), int(body.get("h") or 0)
            if not (320 <= w <= 3840 and 320 <= h <= 3840):
                return 400, {"detail": "viewport sides must be 320-3840"}
            # A size some scene was rendered at (or the default again) is taken, and the picture follows.
            if (w, h) == VIEWPORT or any((w, h) in sc.sizes for sc in self.scenes.values()):
                g.viewport = (w, h)
                self.paint(g.id)
            return 200, {"viewport": {"w": w, "h": h}}
        if action == "control" and method == "POST":
            owner = body.get("owner")
            if owner == "human":
                client = next((c for c in self.live(g.id) if c.id == body.get("client_id")), None)
                if client is None or client.read_only:
                    return 409, {"detail": "that client is not attached to this browser"}
                self.set_control(g.id, "human", client.id)
            elif owner in ("agent", "paused"):
                self.set_control(g.id, owner, reason=body.get("reason", ""))
                if owner == "agent":
                    g.needs = None
            else:
                return 422, {"detail": "owner must be agent, human or paused"}
            return 200, {"owner": g.owner, "holder": g.holder, "until": None, "reason": g.reason}
        if action == "actions" and method == "GET":
            return 200, {"actions": list(reversed(g.actions))}
        if action == "dialog" and method == "POST":
            g.dialog = None
            self.broadcast(g.id, {"type": "dialog", "state": "closed", "tab_id": self.active(g).id})
            return 200, {}
        if action == "recording" and method == "GET":
            return 200, {"recording": {"frames": g.recording, "human": False}, "frames": [{k: v for k, v in f.items() if k != "scene"} for f in g.frames]}
        if action == "recording" and method == "POST":
            was = g.recording
            g.recording = bool(body.get("frames"))
            if g.recording and not was:
                self.keyframe(g, "start")
            return 200, {"group_id": g.id, "frames": g.recording, "human": bool(body.get("human"))}
        if action == "recording" and method == "DELETE":
            g.frames = []
            return 200, {"ok": True}
        if action == "workflow" and method == "GET" and len(segments) == 4:
            live = self.workflow_view(g, g.workflow) if g.workflow else None
            return 200, {"workflow": live, "recent": [self.workflow_view(g, w) for w in g.workflows]}
        if action == "workflow" and method == "POST" and len(segments) == 4:
            if g.owner != "human":
                return 409, {"detail": "take the browser first: a recording is of a person's own steps", "code": "forbidden"}
            if not g.workflow:
                self.counter += 1
                tab = self.active(g)
                g.workflow = {"id": f"w{self.counter}", "state": "recording", "values": body.get("values", "slots"), "started": time.time(), "steps": [],
                              "start_url": self.scenes[tab.scene].url, "start_title": self.scenes[tab.scene].title}
                self.broadcast(g.id, {"type": "workflow", "state": "started", "id": g.workflow["id"], "recording": True, "steps": 0, "values": g.workflow["values"]})
            return 200, {"workflow": self.workflow_view(g, g.workflow)}
        if action == "workflow" and method == "POST" and segments[-1] == "stop":
            if not g.workflow:
                return 404, {"detail": "not recording", "code": "not_found"}
            return 200, {"workflow": self.stop_workflow(g.id, "operator")}
        if action == "workflow" and method == "POST" and segments[-1] == "mark":
            if not g.workflow:
                return 404, {"detail": "not recording", "code": "not_found"}
            text = g.selection or self.scenes[self.active(g).scene].title
            return 200, {"step": self.record_step(g.id, action="expect", text=text)}
        if action == "close" and method == "POST":
            g.status = "closed"
            self.drop(g.id, 4404)
            return 200, {"tabs": len(g.tabs)}
        return 404, {"detail": "no such route"}

    def answer_settings(self, method: str, parts: list[str], body: Any) -> tuple[int, Any]:
        """Settings → Browser's routes: the running browsers, the profiles, the recordings and the load."""
        what = parts[0]
        if what == "running" and method == "GET":
            return 200, {"browsers": self.running}
        if what == "running" and method == "POST" and len(parts) == 4 and parts[3] == "close":
            self.running = [b for b in self.running if b["id"] != parts[2]]
            return 200, {"ok": True}
        if what == "profiles" and method == "GET":
            return 200, {"profiles": self.profiles}
        if what == "profiles" and len(parts) >= 3:
            name = parts[-2] if parts[-1] == "clear" else parts[-1]
            if method == "DELETE":
                self.profiles = [p for p in self.profiles if p["id"] != name]
            elif method == "POST" and parts[-1] == "clear":
                for p in self.profiles:
                    if p["id"] == name:
                        p["size_bytes"] = 0
            return 200, {"ok": True}
        if what == "recordings" and method == "GET":
            groups = [{"group_id": g.id, "frames": len(g.frames), "bytes": sum(f["bytes"] for f in g.frames), "first_at": g.frames[0]["at"], "last_at": g.frames[-1]["at"]} for g in self.groups.values() if g.frames]
            return 200, {"envs": [{"env": "container", "groups": groups, "bytes": sum(x["bytes"] for x in groups), "max_bytes": 500 << 20, "retention_ms": 7 * 86_400_000}]}
        if what == "load" and method == "GET":
            return 200, browser_load(running=len(self.running))
        if what == "workflows" and len(parts) >= 2:
            found = next(((g, w) for g in self.groups.values() for w in g.workflows if w["id"] == parts[1]), None)
            if found is None:
                return 404, {"detail": "no such recording"}
            g, w = found
            if method == "GET" and len(parts) == 2:
                return 200, {"workflow": self.workflow_view(g, w)}
            if method == "POST" and parts[-1] == "draft":
                return self.draft(w["id"], str((body or {}).get("goal") or ""))
            if method == "DELETE":
                g.workflows = [x for x in g.workflows if x is not w]
                return 200, {"ok": True}
        if what == "notes" and method == "GET" and len(parts) == 1:
            return 200, {"notes": sorted(self.notes, key=lambda n: n["status"] != "proposed")}
        if what == "notes" and len(parts) >= 2:
            note = next((n for n in self.notes if n["id"] == parts[1]), None)
            if note is None:
                return 404, {"detail": "no such note"}
            if method == "POST" and parts[-1] == "approve":
                note.update(status="active", approved_at=note["proposed_at"] + 60)
                return 200, {"note": note}
            if method == "PATCH":
                if not str((body or {}).get("text") or "").strip():
                    return 400, {"detail": "a procedure needs its steps"}
                note["text"] = body["text"]
                if note.get("kind") == "procedure" and body.get("title") is not None:
                    note["title"] = body["title"]
                return 200, {"note": note}
            if method == "DELETE":
                self.notes = [n for n in self.notes if n["id"] != parts[1]]
                return 200, {"ok": True}
        return 404, {"detail": "no such route"}

    def route(self, route: Any) -> None:
        """``page.route("**/api/browsers**", stub.route)``: registered after the general stub, so it wins."""
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path[parts.path.index("/api/"):]
        body = None
        if request.method in ("POST", "PATCH"):
            try:
                body = request.post_data_json
            except Exception:  # noqa: BLE001
                body = None
        with self.lock:
            self.requests.append((request.method, path, body))
            segments = path.strip("/").split("/")
            if len(segments) == 5 and segments[3] == "frames" and request.method == "GET":
                # A keyframe is a picture, not JSON: the scene it was taken of.
                g = self.groups.get(segments[2])
                frame = next((f for f in (g.frames if g else []) if str(f["no"]) == segments[4]), None)
                if frame is None:
                    route.fulfill(status=404, content_type="application/json", body=json.dumps({"detail": "no such keyframe"}))
                else:
                    route.fulfill(status=200, content_type="image/jpeg", body=self.scenes[frame["scene"]].jpeg)
                return
            status, answer = self.answer(request.method, path, parse_qs(parts.query), body)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(answer))

    def install(self, page: Any) -> None:
        page.route("**/api/browsers**", self.route)
        page.route_web_socket("**/ws/browsers/**", self.socket)

    def posted(self, suffix: str) -> list[Any]:
        return [b for m, p, b in self.requests if m == "POST" and p.endswith(suffix)]


GIB = 1 << 30
MIB = 1 << 20


def browser_load(*, running: int = 1, cap: int = 2, total: int = 62 * GIB, available: int = 38 * GIB, each: int = 260 * MIB) -> dict[str, Any]:
    """``GET /api/browsers/load`` for the harness's 62 GB, 16-CPU machine: browsers of about 260 MB."""
    daemon = 12 * MIB
    used = running * each + daemon
    return {
        "cap": cap, "running": running, "queued": [], "memory_basis": "cgroup",
        "used": {"rss_bytes": used, "daemon_rss_bytes": daemon, "cpu_percent": 4.0, "cpus": 16, "mem_total_bytes": total, "mem_available_bytes": available, "machine_cpu_percent": 14.0},
        "likely": {"rss_bytes": each, "cpu_percent": 12.0, "samples": 30, "basis": "measured"},
        "projection": {"cap": cap, "sessions": max(cap, running), "terminals_rss_bytes": used, "machine_used_bytes": total - available, "mem_total_bytes": total, "mem_percent": 40.0, "cpu_percent": 15.0, "level": "ok", "cpu_level": "ok"},
        "envs": [{"env": "container", "supported": True, "browsers": running, "rss_bytes": running * each, "cpu_percent": 4.0, "mem_total_bytes": total, "mem_available_bytes": available}],
        "thresholds": {"warn": 70.0, "bad": 90.0},
    }


def open_page(context: Any, bs: BrowserStub, general: Any, url: str, wait: str = ".chat-scroll .timeline") -> Any:
    """A page at ``url`` with every API answered: the browsers by ``bs``, the rest by ``general``."""
    page = context.new_page()
    page.route("**/api/**", general)
    bs.install(page)
    page.goto(url)
    page.wait_for_selector(wait, timeout=20000)
    return page


def cursor_expected(page: Any, root: str, point: dict[str, float]) -> tuple[float, float]:
    """Where the app must draw the agent's cursor for a page ``point``: the picture's own rectangle
    (``data-rect`` on the canvas) scaled from the 1280 px viewport."""
    rect = page.locator(f"{root} .bv-canvas").get_attribute("data-rect")
    x, y, w, h = (float(v) for v in rect.split(","))
    return x + point["x"] * w / VIEWPORT[0], y + point["y"] * h / VIEWPORT[1]


def wait_frames(page: Any, root: str, at_least: int = 1, timeout: int = 10000) -> int:
    page.wait_for_function(
        "([sel, n]) => { const c = document.querySelector(sel); return c && Number(c.dataset.frames || 0) >= n; }",
        arg=[f"{root} .bv-canvas", at_least], timeout=timeout,
    )
    return int(page.locator(f"{root} .bv-canvas").get_attribute("data-frames") or 0)


__all__ = ["open_page", "cursor_expected", "wait_frames", "BrowserStub", "Client", "Group", "Scene", "Tab", "VIEWPORT", "decode", "enc_event", "enc_frame", "render_scenes"]
