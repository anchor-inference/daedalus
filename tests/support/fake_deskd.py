"""A desktop daemon in-process: the socket protocol of ``docs/architecture/computer.md`` over a scripted desktop.

It speaks the real framing, handshake (with the role), and JSON-RPC, so the host's client, agent and
tools are exercised end to end over a unix socket in a temporary run directory. The desktop is a list
of windows on one display; a screenshot is a real JPEG of them painted in their colours, sized by the
contract's rules (captured pixels scaled down, never up, to ``max_edge``). The rules the host meets
are the contract's: the lease names a session or the call is ``1200``; a point names a screenshot the
daemon handed out or it is ``1201``; a point off the picture is ``1202``; parameters are decoded
strictly, so a misspelt one is ``-32602`` as the real daemon's is. ``fail`` makes a method's next call
answer with any refusal a test wants.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import secrets
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PARAMS: dict[str, set[str]] = {
    "info": set(),
    "lease.status": set(),
    "screenshot": {"session", "display", "window", "max_edge", "format", "quality"},
    "act": {"session", "action", "screenshot_id", "x", "y", "button", "count", "mods", "path", "dx", "dy", "text", "keys", "repeat", "ms"},
    "cursor": {"session", "screenshot_id"},
    "apps.list": {"session"},
    "apps.focus": {"session", "app", "window"},
    "lease.revoke": {"session"},
    "stop": set(),
}
"""Each method's parameters; anything else is refused as the daemon's strict decoding refuses it."""


class _Fail(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


@dataclass
class Window:
    id: str
    title: str
    bounds: dict[str, float]
    app: dict[str, Any]
    color: tuple[int, int, int] = (200, 200, 200)
    minimized: bool = False


@dataclass
class Shot:
    id: str
    region: dict[str, float]
    width: int
    height: int
    lease: str


@dataclass
class FakeDeskd:
    run_dir: Path
    protocol: int = 1
    role: str = "host"
    display: dict[str, Any] = field(default_factory=lambda: {"id": "1", "name": "Built-in", "bounds": {"x": 0, "y": 0, "w": 1440, "h": 900}, "primary": True, "pixels_per_unit": 2, "scale": 2})
    windows: list[Window] = field(default_factory=list)
    """Front to back."""
    focused: str = ""
    limits: dict[str, Any] = field(default_factory=lambda: {"actions_per_second": 4, "action_burst": 4, "lease_actions": 200, "max_type_chars": 2000, "max_wait_ms": 10000, "screenshot_ttl_s": 600, "max_edge": 1568})
    lease: dict[str, Any] | None = None
    last_ended: str = ""
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    acts: list[dict[str, Any]] = field(default_factory=list)
    fail: dict[str, tuple[int, str, Any]] = field(default_factory=dict)
    """Method → the error its next call answers with, once."""
    cursor: tuple[float, float] = (500.0, 400.0)
    shots: dict[str, Shot] = field(default_factory=dict)
    token: str = field(default_factory=lambda: secrets.token_hex(32))
    _server: asyncio.AbstractServer | None = None
    _writers: set[asyncio.StreamWriter] = field(default_factory=set)
    _serial: int = 0

    def __post_init__(self) -> None:
        if not self.windows:
            self.windows = [
                Window("10", "notes.txt — Editor", {"x": 100, "y": 100, "w": 800, "h": 600}, {"id": "com.example.editor", "name": "Editor", "pid": 501, "bundle_id": "com.example.editor"}, (255, 0, 0)),
                Window("20", "Inbox — Mail", {"x": 950, "y": 100, "w": 450, "h": 700}, {"id": "com.apple.mail", "name": "Mail", "pid": 502, "bundle_id": "com.apple.mail"}, (0, 0, 255)),
            ]
            self.focused = "10"

    # -- lifecycle ------------------------------------------------------------------------------

    async def start(self) -> FakeDeskd:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        sock = self.run_dir / "deskd.sock"
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(sock), limit=(1 << 20) + 64)
        (self.run_dir / "token").write_text(self.token + "\n")
        (self.run_dir / "endpoint").write_text("unix:deskd.sock")
        return self

    async def stop(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.run_dir / "endpoint").unlink()
        if self._server is not None:
            self._server.close()
        for writer in list(self._writers):
            writer.close()
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)
        self._server = None

    # -- scripting --------------------------------------------------------------------------------

    def grant(self, session: str, scope: dict[str, Any] | None = None, max_actions: int = 200) -> dict[str, Any]:
        self.lease = {"id": f"lease-{secrets.token_hex(3)}", "session": session, "granted_at": "2026-10-20T10:00:00Z",
                      "expires_at": "2026-10-20T10:30:00Z", "scope": scope or {"kind": "desktop"}, "max_actions": max_actions, "actions": 0}
        self.shots.clear()
        return self.lease

    def end(self, reason: str = "expired") -> None:
        self.lease = None
        self.last_ended = reason

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    # -- the wire ---------------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            channel, payload = await self._read(reader)
            if channel != 0 or payload.strip() != self.token.encode():
                await asyncio.sleep(0.05)
                return
            await self._send(writer, {"jsonrpc": "2.0", "method": "hello", "params": {
                "version": "fake", "protocol": self.protocol, "instance": "fake-instance", "role": self.role,
                "platform": {"os": "linux", "session_type": "fake", "units": "pixels"}}})
            while True:
                channel, payload = await self._read(reader)
                if channel != 0:
                    continue
                message = json.loads(payload)
                method, params = str(message.get("method")), message.get("params") or {}
                self.calls.append((method, params))
                try:
                    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": self._handle(method, params)}
                except _Fail as exc:
                    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
                    if exc.data is not None:
                        error["data"] = exc.data
                    reply = {"jsonrpc": "2.0", "id": message["id"], "error": error}
                await self._send(writer, reply)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _read(self, reader: asyncio.StreamReader) -> tuple[int, bytes]:
        length, channel = struct.unpack(">II", await reader.readexactly(8))
        return channel, await reader.readexactly(length - 4)

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        payload = json.dumps(message).encode()
        writer.write(struct.pack(">II", 4 + len(payload), 0) + payload)
        await writer.drain()

    # -- the methods ------------------------------------------------------------------------------

    def _holder(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.lease is None:
            raise _Fail(1200, "no lease", {"reason": self.last_ended or "none"})
        if self.lease["session"] != params.get("session"):
            raise _Fail(1200, "no lease", {"reason": "other_session"})
        return self.lease

    def _status(self) -> dict[str, Any]:
        if self.lease is None:
            lease: dict[str, Any] = {"active": False}
            if self.last_ended:
                lease["last_ended"] = self.last_ended
        else:
            lease = {"active": True, "lease": self.lease, "remaining_ms": 1_200_000}
        return {"lease": lease, "human": {"paused": False}}

    def _handle(self, method: str, params: dict[str, Any]) -> Any:  # noqa: C901 — one dispatcher, as the daemon's
        if method not in PARAMS:
            raise _Fail(-32601, f"no method {method}")
        unknown = set(params) - PARAMS[method]
        if unknown:
            raise _Fail(-32602, f"params: json: unknown field {sorted(unknown)[0]!r}")
        if method in self.fail:
            code, message, data = self.fail.pop(method)
            raise _Fail(code, message, data)
        if method == "info":
            return {"version": "fake", "protocol": 1, "role": self.role, "platform": {"os": "linux", "session_type": "fake", "units": "pixels"},
                    "displays": [self.display], "permissions": {}, "secure": {"locked": False}, **self._status(), "limits": self.limits}
        if method == "lease.status":
            return self._status()
        if method == "lease.revoke":
            ended = self.lease is not None and self.lease["session"] == params.get("session")
            if ended:
                self.end("revoked")
            return {"revoked": ended}
        if method == "stop":
            self.end("stopped")
            return {"stopped": True}
        lease = self._holder(params)
        if method == "screenshot":
            return self._screenshot(params, lease)
        if method == "apps.list":
            return self._apps()
        if method == "apps.focus":
            target = next((w for w in self.windows if w.id == params.get("window") or w.app["id"] == params.get("app")), None)
            if target is None:
                raise _Fail(1001, "no such window")
            self.windows.remove(target)
            self.windows.insert(0, target)
            self.focused = target.id
            return {"window": self._window_view(target, 0)}
        if method == "cursor":
            out: dict[str, Any] = {"x": self.cursor[0], "y": self.cursor[1]}
            shot = self.shots.get(str(params.get("screenshot_id") or ""))
            if shot is not None:
                region = shot.region
                px = int((self.cursor[0] - region["x"]) * shot.width / region["w"])
                py = int((self.cursor[1] - region["y"]) * shot.height / region["h"])
                if 0 <= px < shot.width and 0 <= py < shot.height:
                    out["in_screenshot"] = [px, py]
            return out
        if method == "act":
            return self._act(params, lease)
        raise _Fail(-32601, f"no method {method}")

    def _window_view(self, window: Window, order: int) -> dict[str, Any]:
        return {"id": window.id, "title": window.title, "bounds": window.bounds, "display": "1", "focused": window.id == self.focused,
                "minimized": window.minimized, "in_scope": True, "order": order}

    def _apps(self) -> dict[str, Any]:
        apps: dict[str, dict[str, Any]] = {}
        front = None
        for order, window in enumerate(self.windows):
            view = self._window_view(window, order)
            app = apps.setdefault(window.app["id"], {**window.app, "reachable": True, "in_scope": True, "windows": []})
            app["windows"].append(view)
            if window.id == self.focused:
                front = view
        out: dict[str, Any] = {"apps": list(apps.values())}
        if front is not None:
            out["foreground"] = front
        return out

    def _screenshot(self, params: dict[str, Any], lease: dict[str, Any]) -> dict[str, Any]:
        from PIL import Image, ImageDraw

        region = dict(self.display["bounds"])
        if params.get("window"):
            window = next((w for w in self.windows if w.id == params["window"]), None)
            if window is None:
                raise _Fail(1001, "no such window")
            region = dict(window.bounds)
        ppu = float(self.display["pixels_per_unit"])
        captured_w, captured_h = region["w"] * ppu, region["h"] * ppu
        max_edge = int(params.get("max_edge") or self.limits["max_edge"])
        scale = min(1.0, max_edge / max(captured_w, captured_h))
        width, height = max(1, round(captured_w * scale)), max(1, round(captured_h * scale))
        image = Image.new("RGB", (width, height), (30, 30, 30))
        draw = ImageDraw.Draw(image)
        for window in reversed(self.windows):
            b = window.bounds
            box = [((b["x"] - region["x"]) * width / region["w"]), ((b["y"] - region["y"]) * height / region["h"]),
                   ((b["x"] + b["w"] - region["x"]) * width / region["w"]), ((b["y"] + b["h"] - region["y"]) * height / region["h"])]
            draw.rectangle(box, fill=window.color)
        buffer = io.BytesIO()
        fmt = str(params.get("format") or "jpeg")
        image.save(buffer, format="PNG" if fmt == "png" else "JPEG", quality=int(params.get("quality") or 80))
        self._serial += 1
        shot = Shot(f"s{self._serial}", region, width, height, lease["id"])
        self.shots[shot.id] = shot
        return {"screenshot_id": shot.id, "format": fmt, "data": base64.b64encode(buffer.getvalue()).decode(), "width": width, "height": height,
                "display": "1", **({"window": params["window"]} if params.get("window") else {}), "region": region,
                "units_per_pixel": [region["w"] / width, region["h"] / height], "pixels_per_unit": ppu, "scale": 2, "units": "pixels"}

    def _point(self, shot: Shot, x: int, y: int) -> tuple[float, float]:
        if not (0 <= x < shot.width and 0 <= y < shot.height):
            raise _Fail(1202, "off the screenshot", {"scope": self.lease["scope"] if self.lease else {}, "point": [x, y]})
        region = shot.region
        return region["x"] + (x + 0.5) * region["w"] / shot.width, region["y"] + (y + 0.5) * region["h"] / shot.height

    def _act(self, params: dict[str, Any], lease: dict[str, Any]) -> dict[str, Any]:
        action = params.get("action")
        points: list[tuple[float, float]] = []
        needs_shot = "x" in params or "path" in params
        if needs_shot:
            shot = self.shots.get(str(params.get("screenshot_id") or ""))
            if shot is None:
                raise _Fail(1201, "stale screenshot", {"screenshot_id": params.get("screenshot_id"), "reason": "unknown"})
            if "path" in params:
                points = [self._point(shot, int(p["x"]), int(p["y"])) for p in params["path"]]
            else:
                points = [self._point(shot, int(params["x"]), int(params["y"]))]
        if action == "type" and len(str(params.get("text") or "")) > self.limits["max_type_chars"]:
            raise _Fail(1003, f"type at most {self.limits['max_type_chars']} characters a call")
        if points:
            self.cursor = points[-1]
        lease["actions"] += 1
        self.acts.append(dict(params))
        target_window = None
        if action in ("type", "key"):
            target_window = next((w for w in self.windows if w.id == self.focused), None)
        else:
            x, y = self.cursor
            target_window = next((w for w in self.windows if w.bounds["x"] <= x < w.bounds["x"] + w.bounds["w"] and w.bounds["y"] <= y < w.bounds["y"] + w.bounds["h"]), None)
        out: dict[str, Any] = {"action": action, "cursor": list(self.cursor), "duration_ms": 1, "actions_left": lease["max_actions"] - lease["actions"]}
        if points:
            out["points"] = [list(p) for p in points]
        if target_window is not None and action != "wait":
            out["target"] = {"window": target_window.id, "app": target_window.app}
        return out


__all__ = ["FakeDeskd", "Window"]
