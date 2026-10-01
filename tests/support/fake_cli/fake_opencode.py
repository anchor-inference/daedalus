"""A fake OpenCode (major version 1): the TUI with its HTTP server inside the same process.

What it imitates:

- ``opencode <dir> --port P --hostname 127.0.0.1 [--agent a] [-m provider/model] [-s session]``.
  The server listens on that port inside the TUI's own process (there is no separate ``serve``); a
  port already taken prints the error on screen and exits 1, which the adapter must answer by trying
  the next port. ``OPENCODE_SERVER_PASSWORD`` turns on basic authentication (user ``opencode``);
  without it the server answers anyone on loopback, as the real one did in the probe.
  ``FAKE_CLI_FAULTS=hold_first_connection`` holds the first connection it accepts open and never
  answers it, as the real TUI does with the one accepted in the moment before its server can answer
  (measured: unanswered past 120 s, the next one answered at once).
- Routes: ``GET /global/health``, ``GET /event`` (server-sent events, ``server.connected`` first),
  ``GET|POST /session``, ``GET /session/:id``, ``GET /session/:id/message``,
  ``POST /session/:id/prompt_async``, ``POST /session/:id/abort``, ``POST /tui/append-prompt``,
  ``POST /tui/submit-prompt``, ``POST /tui/clear-prompt``, ``POST /tui/select-session``,
  ``POST /permission/:id/reply`` (``once``, ``always``, ``reject``, with an optional ``message``) and
  ``POST /question/:id/reply``.
- Events: ``session.created``, ``session.status`` (``busy``/``idle``), ``session.idle``,
  ``message.updated`` and ``message.part.updated`` (the user message is the acknowledgement),
  ``permission.asked`` / ``permission.replied``, ``question.asked`` / ``question.replied``,
  ``session.error``. The names beyond ``server.connected`` are the plan's reading, to be checked
  against the real server; they are kept in one table here so they change in one place.
- A message typed while the agent works is queued until the turn ends — there is no steering.
  Esc interrupts only when pressed twice ("esc again to interrupt").
- ``OPENCODE_CONFIG_CONTENT`` is read: ``mcp.daedalus_team`` (``type: local``, ``command`` as a
  list, ``environment``, ``timeout``) is started for the team tools, and a message pointing at a file
  outside the project asks the ``external_directory`` permission unless
  ``permission.external_directory`` allows its path.
- Commands: ``--version``, ``agent list``, ``models``, ``auth list`` (names only), ``export <id>``
  (after exit, from its own store), ``upgrade <version>``; a bare ``upgrade`` installs the newest
  release, which is the next major version — the reason the updater must always name a 1.x target.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fnmatch
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.fake_cli.agent import FakeAgent  # noqa: E402
from tests.support.fake_cli.tui import (  # noqa: E402
    Args,
    Dialog,
    Look,
    McpClient,
    exit_with,
    installed_version,
    logged_in,
    new_id,
    set_version,
    settle,
    usage_error,
)

DEFAULT_VERSION = "1.18.23"
NEXT_MAJOR = "2.0.1"
FLAGS = {"--port": 1, "--hostname": 1, "--agent": 1, "--model": 1, "--session": 1, "--version": 0, "--prompt": 1}
ALIASES = {"-m": "--model", "-s": "--session", "-v": "--version"}
EVENTS = {
    "session_created": "session.created", "session_status": "session.status", "session_idle": "session.idle",
    "message_updated": "message.updated", "part_updated": "message.part.updated", "permission_asked": "permission.asked",
    "permission_replied": "permission.replied", "question_asked": "question.asked", "question_replied": "question.replied",
    "session_error": "session.error",
}


def store_dir() -> Path:
    path = Path(os.environ.get("HOME") or "/tmp") / ".local" / "share" / "opencode" / "fake-sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ms() -> int:
    return int(time.time() * 1000)


class FakeOpenCode(FakeAgent):
    cli = "opencode"
    look = Look("opencode", "opencode (fake)", prompt="┃ ", idle_hint="enter send", busy_hint="esc interrupt", busy_word="Working", alt_screen=True, collapse_chars=2000)

    def __init__(self, args: Args) -> None:
        directory = args.positional[0] if args.positional else os.getcwd()
        super().__init__(str(Path(directory).resolve()))
        self.args = args
        port = args.get("--port")
        if not port.isdigit():
            usage_error("opencode", "the fake needs --port <number>")
        self.port = int(port)
        self.host = args.get("--hostname", "127.0.0.1")
        self.password = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
        self.config = json.loads(os.environ.get("OPENCODE_CONFIG_CONTENT") or "{}")
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.current = ""
        self.subscribers: list[asyncio.StreamWriter] = []
        self.requests: dict[str, asyncio.Future[Any]] = {}
        self.mcp: McpClient | None = None
        self.last_esc = 0.0
        self.current_message: dict[str, Any] | None = None
        self.held_first = False
        self.ids_by_text: dict[str, list[str]] = {}
        """``messageID`` a client chose for a prompt, by its text: the user message takes that id."""
        if args.get("--session"):
            self.current = args.get("--session")
            self.load(self.current)

    # -- storage ------------------------------------------------------------------------------------

    def load(self, session_id: str) -> None:
        path = store_dir() / f"{session_id}.json"
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.sessions[session_id] = saved["info"]
            self.messages[session_id] = saved["messages"]
        else:
            self.new_session(session_id=session_id)

    def save(self, session_id: str) -> None:
        (store_dir() / f"{session_id}.json").write_text(json.dumps({"info": self.sessions[session_id], "messages": self.messages[session_id]}), encoding="utf-8")

    def new_session(self, title: str = "", session_id: str = "") -> dict[str, Any]:
        ident = session_id or "ses_" + new_id().replace("-", "")[:26]
        info = {"id": ident, "title": title or "New session", "directory": self.cwd, "projectID": "fake", "version": installed_version("opencode", DEFAULT_VERSION), "time": {"created": ms(), "updated": ms()}}
        self.sessions[ident] = info
        self.messages[ident] = []
        self.save(ident)
        self.publish("session_created", {"info": info})
        return info

    # -- start ---------------------------------------------------------------------------------------

    async def before_ready(self) -> bool:
        try:
            await asyncio.start_server(self.http, self.host, self.port, limit=1 << 22)
        except OSError as exc:
            # Said on the normal screen, after leaving the alternate one, as a fatal error is.
            self.tui.restore()
            sys.stderr.write(f"Error: Failed to start server on port {self.port}: {exc.strerror or exc}\r\n")
            sys.stderr.flush()
            self.log("port_in_use", port=self.port)
            await self.quit(1)
            return False
        for path in self.config.get("instructions") or []:
            with contextlib.suppress(OSError):
                self.log("system_prompt", text=Path(str(path)).read_text(encoding="utf-8"))
        roots = (self.config.get("skills") or {}).get("paths") or []
        if roots:
            self.log("skills", names=sorted(p.parent.name for root in roots for p in Path(str(root)).glob("**/SKILL.md")))
        spec = (self.config.get("mcp") or {}).get("daedalus_team")
        if isinstance(spec, dict) and spec.get("type") == "local":
            command = [str(c) for c in spec.get("command") or []]
            if command:
                self.mcp = McpClient("daedalus_team", command[0], command[1:], {str(k): str(v) for k, v in (spec.get("environment") or {}).items()})
                if not await self.mcp.start():
                    self.tui.say(f"MCP daedalus_team failed: {self.mcp.error}")
                    self.mcp = None
        return True

    # -- events --------------------------------------------------------------------------------------

    def publish(self, key: str, properties: dict[str, Any]) -> None:
        payload = json.dumps({"type": EVENTS[key] if key in EVENTS else key, "properties": properties})
        frame = f"data: {payload}\n\n".encode()
        for writer in list(self.subscribers):
            try:
                writer.write(frame)
            except (ConnectionError, RuntimeError):
                self.subscribers.remove(writer)

    def status(self, kind: str) -> None:
        if self.current:
            self.publish("session_status", {"sessionID": self.current, "status": {"type": kind}})
            if kind == "idle":
                self.publish("session_idle", {"sessionID": self.current})

    # -- the turn ------------------------------------------------------------------------------------

    async def submit(self, text: str) -> None:
        if not self.current:
            self.current = self.new_session(title=text[:40])["id"]
        await super().submit(text)

    async def inject_pending(self) -> None:
        """No steering: a queued message waits for the end of the turn (the engine starts it then)."""

    async def turn(self, prompt: str) -> None:
        pointer = prompt.partition("Read the message in ")[2].split(" ")[0].rstrip(".")
        if pointer and not pointer.startswith(self.cwd + "/") and not self.external_allowed(pointer):
            tool_id = "call_" + new_id()[:8]
            decision = await self.permission("external_directory", {"filePath": pointer}, pointer, tool_id)
            if not decision.startswith("allow"):
                self.tui.say(f"● Permission to read {pointer} was rejected.")
                self.status("idle")
                return
        await super().turn(prompt)

    def external_allowed(self, path: str) -> bool:
        rules = (self.config.get("permission") or {}).get("external_directory")
        if isinstance(rules, str):
            return rules == "allow"
        if isinstance(rules, dict):
            return any(fnmatch.fnmatch(path, pattern.replace("**", "*")) and verdict == "allow" for pattern, verdict in rules.items())
        return False

    def add_message(self, role: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
        ident = "msg_" + new_id().replace("-", "")[:24]
        if role == "user":
            text = "".join(str(p.get("text") or "") for p in parts)
            chosen = self.ids_by_text.get(text) or []
            if chosen:
                ident = chosen.pop(0)
        info: dict[str, Any] = {"id": ident, "sessionID": self.current, "role": role, "time": {"created": ms()}}
        if role == "assistant":
            info.update(modelID=self.args.get("--model", "anthropic/claude-sonnet-4").split("/")[-1], providerID="anthropic", tokens={"input": 900, "output": 60, "cache": {"read": 400, "write": 0}}, cost=0.001)
        message: dict[str, Any] = {"info": info, "parts": []}
        self.messages[self.current].append(message)
        self.publish("message_updated", {"info": info})
        for part in parts:
            self.add_part(message, part)
        self.save(self.current)
        return message

    def add_part(self, message: dict[str, Any], part: dict[str, Any]) -> None:
        part = {"id": "prt_" + new_id().replace("-", "")[:24], "sessionID": self.current, "messageID": message["info"]["id"], **part}
        message["parts"].append(part)
        self.publish("part_updated", {"part": part})

    async def on_prompt(self, text: str, *, queued: bool) -> None:
        self.add_message("user", [{"type": "text", "text": text}])

    async def on_turn_started(self) -> None:
        self.status("busy")
        self.current_message = self.add_message("assistant", [])

    async def on_assistant(self, text: str) -> None:
        if self.current_message is not None:
            self.add_part(self.current_message, {"type": "text", "text": text})
            self.save(self.current)

    async def on_tool_start(self, name: str, tool_input: dict[str, Any], tool_id: str) -> None:
        if self.current_message is not None:
            self.add_part(self.current_message, {"type": "tool", "tool": name.lower(), "callID": tool_id, "state": {"status": "running", "input": tool_input}})

    async def on_tool_end(self, name: str, tool_input: dict[str, Any], tool_id: str, output: str, ok: bool) -> None:
        if self.current_message is not None:
            self.add_part(self.current_message, {"type": "tool", "tool": name.lower(), "callID": tool_id, "state": {"status": "completed" if ok else "error", "input": tool_input, "output": output}})
            self.save(self.current)

    async def on_turn_completed(self) -> None:
        if self.faults.no_stop_hook:
            self.log("stop_hook_suppressed")
            return
        self.status("idle")

    async def on_turn_failed(self, kind: str) -> None:
        self.publish("session_error", {"sessionID": self.current, "error": {"name": "APIError", "data": {"message": kind}}})
        self.status("idle")

    async def on_turn_cancelled(self) -> None:
        self.publish("session_error", {"sessionID": self.current, "error": {"name": "MessageAbortedError", "data": {"message": "aborted"}}})
        self.status("idle")

    async def escape(self) -> None:
        if not self.busy:
            return
        if time.monotonic() - self.last_esc < 1.0:
            await super().escape()
        else:
            self.tui.notice = "esc again to interrupt"
            self.tui.render()
        self.last_esc = time.monotonic()

    async def abort(self) -> None:
        if self.busy:
            assert self.turn_task is not None
            self.log("interrupt", via="abort")
            self.turn_task.cancel()

    # -- permissions and questions ------------------------------------------------------------------------

    async def permission(self, tool: str, tool_input: dict[str, Any], summary: str, tool_id: str) -> str:
        ident = "per_" + new_id().replace("-", "")[:24]
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.requests[ident] = future
        meanings = ["once", "always", "reject"]
        kind = "bash" if tool == "Bash" else tool
        self.tui.open_dialog(Dialog("permission", f"Permission required: {kind}", [f"  {summary}"], ["Allow once", "Allow always", "Reject"],
                                    on_choose=lambda i: settle(future, {"reply": meanings[i], "via": "tui"}), on_escape=lambda: settle(future, {"reply": "reject", "via": "tui"})))
        # The shape 1.18.23 sent (recorded): the command in the metadata, the call it belongs to.
        metadata = {"command": summary} if kind == "bash" else {"filePath": summary}
        self.publish("permission_asked", {"id": ident, "sessionID": self.current, "permission": kind, "patterns": [summary], "metadata": metadata, "always": [f"{summary.split(' ')[0]} *"], "tool": {"messageID": self.current_message["info"]["id"] if self.current_message else "", "callID": tool_id}})
        try:
            answer = await future
        finally:
            self.requests.pop(ident, None)
            if self.tui.dialog is not None and self.tui.dialog.kind == "permission":
                self.tui.close_dialog()
        self.publish("permission_replied", {"sessionID": self.current, "requestID": ident, "reply": answer["reply"]})
        return {"once": "allow_once", "always": "allow_always"}.get(answer["reply"], "deny")

    async def question(self, question: str, options: list[str], tool_id: str) -> str:
        ident = "que_" + new_id().replace("-", "")[:24]
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.requests[ident] = future
        labels = options or ["(free text)"]
        self.tui.open_dialog(Dialog("question", question, [], labels, on_choose=lambda i: settle(future, {"answers": [[labels[i]]]}), on_escape=lambda: settle(future, {"answers": [["(no answer)"]]})))
        self.publish("question_asked", {"id": ident, "sessionID": self.current, "questions": [{"question": question, "header": "Question", "options": [{"label": o, "description": ""} for o in options]}]})
        try:
            answer = await future
        finally:
            self.requests.pop(ident, None)
            if self.tui.dialog is not None and self.tui.dialog.kind == "question":
                self.tui.close_dialog()
        self.publish("question_replied", {"sessionID": self.current, "requestID": ident, "answers": answer["answers"]})
        return str(answer["answers"][0][0])

    async def team_tool(self, name: str, arguments: dict[str, Any], tool_id: str) -> str:
        if self.mcp is None or name not in self.mcp.tools:
            return f"error: no tool daedalus_team_{name}"
        await self.on_tool_start(f"daedalus_team_{name}", arguments, tool_id)
        spec = (self.config.get("mcp") or {}).get("daedalus_team") or {}
        try:
            result = await self.mcp.call(name, arguments, float(spec.get("timeout") or 60_000) / 1000)
        except (TimeoutError, ConnectionError, RuntimeError) as exc:
            result = f"error: {exc or 'timed out'}"
        await self.on_tool_end(f"daedalus_team_{name}", arguments, tool_id, result, not result.startswith("error"))
        return result

    async def quit(self, code: int = 0) -> None:
        if code == 0 and self.current and not self.done.is_set():
            # What the real TUI leaves on the screen when it exits.
            self.tui.restore()
            sys.stdout.write(f"\r\n  Session   {self.sessions[self.current].get('title')}\r\n  Continue  opencode -s {self.current}\r\n")
            sys.stdout.flush()
        await super().quit(code)

    async def on_session_end(self) -> None:
        if self.mcp is not None:
            await self.mcp.close()
        for writer in self.subscribers:
            with contextlib.suppress(Exception):
                writer.close()

    # -- the HTTP server ---------------------------------------------------------------------------------

    async def http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.faults.hold_first_connection and not self.held_first:
            self.held_first = True
            self.log("connection_held")
            with contextlib.suppress(Exception):
                await reader.read()  # until the client gives up on it
            writer.close()
            return
        try:
            line = (await reader.readline()).decode("latin-1").strip()
            if not line:
                writer.close()
                return
            method, target, _ = (line.split(" ") + ["", ""])[:3]
            headers: dict[str, str] = {}
            while header := (await reader.readline()).decode("latin-1").strip():
                name, _, value = header.partition(":")
                headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            raw = await reader.readexactly(length) if length else b""
        except (ConnectionError, asyncio.IncompleteReadError, ValueError):
            writer.close()
            return
        if self.password:
            expected = "Basic " + base64.b64encode(f"opencode:{self.password}".encode()).decode()
            if headers.get("authorization") != expected:
                await respond(writer, 401, {"error": "unauthorized"})
                return
        path = target.partition("?")[0]
        if method == "GET" and path == "/event":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n")
            writer.write(b'data: {"type": "server.connected", "properties": {}}\n\n')
            self.subscribers.append(writer)
            with contextlib.suppress(Exception):
                await reader.read()  # held open until the client leaves
            if writer in self.subscribers:
                self.subscribers.remove(writer)
            writer.close()
            return
        try:
            body = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            await respond(writer, 400, {"error": "invalid JSON"})
            return
        status, answer = await self.route(method, path, body)
        await respond(writer, status, answer)

    async def route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        parts = [p for p in path.split("/") if p]
        if method == "GET" and path == "/global/health":
            return 200, {"healthy": True, "version": installed_version("opencode", DEFAULT_VERSION)}
        if path == "/session":
            if method == "GET":
                return 200, list(self.sessions.values())
            if method == "POST":
                if body.get("model"):
                    self.log("session_model", model=body["model"])
                return 200, self.new_session(title=str(body.get("title") or ""))
        if len(parts) >= 2 and parts[0] == "session":
            session_id = parts[1]
            if session_id not in self.sessions:
                return 404, {"error": f"session {session_id} not found"}
            if len(parts) == 2 and method == "GET":
                return 200, self.sessions[session_id]
            if parts[2:] == ["message"] and method == "GET":
                return 200, self.messages[session_id]
            if parts[2:] == ["abort"] and method == "POST":
                if session_id == self.current:
                    await self.abort()
                return 200, True
            if parts[2:] == ["prompt_async"] and method == "POST":
                text = "".join(str(p.get("text", "")) for p in body.get("parts") or [] if p.get("type") == "text")
                if body.get("messageID"):
                    if not str(body["messageID"]).startswith("msg"):
                        return 400, {"error": "messageID must start with msg"}
                    self.ids_by_text.setdefault(text, []).append(str(body["messageID"]))
                if body.get("model"):
                    self.log("prompt_model", model=body["model"])
                self.current = session_id
                self.log("submitted", text=text, busy=self.busy, via="prompt_async")
                await self.submit(text)
                return 204, None
        if parts[:1] == ["tui"] and method == "POST":
            action = parts[1] if len(parts) > 1 else ""
            if action == "append-prompt":
                self.tui.parts.append(str(body.get("text", "")))
                self.tui._merge()
                self.tui.render()
                return 200, True
            if action == "submit-prompt":
                self.tui._enter()
                self.tui.render()
                return 200, True
            if action == "clear-prompt":
                self.tui.parts.clear()
                self.tui.render()
                return 200, True
            if action == "select-session":
                session_id = str(body.get("sessionID", ""))
                if session_id not in self.sessions:
                    return 404, {"error": "no such session"}
                self.current = session_id
                self.tui.say(f"session {session_id}")
                return 200, True
        if len(parts) == 3 and parts[0] in ("permission", "question") and parts[2] == "reply" and method == "POST":
            future = self.requests.get(parts[1])
            if future is None or future.done():
                return 404, {"error": "no such pending request"}
            if parts[0] == "permission":
                reply = body.get("reply")
                if reply not in ("once", "always", "reject"):
                    return 400, {"error": "reply is once, always or reject"}
                future.set_result({"reply": reply, "message": body.get("message"), "via": "http"})
            else:
                future.set_result({"answers": body.get("answers") or [["(no answer)"]], "via": "http"})
            if self.tui.dialog is not None:
                self.tui.close_dialog()
            return 200, True
        return 404, {"error": f"no route {method} {path}"}


async def respond(writer: asyncio.StreamWriter, status: int, body: Any) -> None:
    data = b"" if body is None else json.dumps(body).encode()
    reason = {200: "OK", 204: "No Content", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}.get(status, "Status")
    head = f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n"
    with contextlib.suppress(ConnectionError):
        writer.write(head.encode() + data)
        await writer.drain()
    writer.close()


def command(argv: list[str]) -> int | None:
    if argv[:1] in (["--version"], ["-v"]):
        print(installed_version("opencode", DEFAULT_VERSION))
        return 0
    if argv[:2] == ["agent", "list"]:
        print("build (primary)\nplan (primary)\ngeneral (subagent)")
        return 0
    if argv[:1] == ["models"]:
        print("anthropic/claude-sonnet-4\nanthropic/claude-haiku-4\nopenai/gpt-5")
        return 0
    if argv[:2] == ["auth", "list"]:
        if logged_in("opencode"):
            print("Credentials ~/.local/share/opencode/auth.json\n●  Anthropic oauth\n●  OpenAI api\n\n2 credentials")
        else:
            print("Credentials ~/.local/share/opencode/auth.json\n\n0 credentials")
        return 0
    if argv[:1] == ["export"]:
        if len(argv) < 2 or not (store_dir() / f"{argv[1]}.json").exists():
            print(f"Session not found: {argv[1] if len(argv) > 1 else ''}", file=sys.stderr)
            return 1
        print((store_dir() / f"{argv[1]}.json").read_text(encoding="utf-8"))
        return 0
    if argv[:1] == ["upgrade"]:
        target = argv[1] if len(argv) > 1 else os.environ.get("FAKE_OPENCODE_NEWEST") or NEXT_MAJOR
        previous = installed_version("opencode", DEFAULT_VERSION)
        set_version("opencode", target.lstrip("v"))
        print(f"Upgraded opencode from {previous} to {target.lstrip('v')}")
        return 0
    return None


def main() -> None:
    argv = sys.argv[1:]
    code = command(argv)
    if code is not None:
        raise SystemExit(code)
    agent = FakeOpenCode(Args("opencode", argv, flags=FLAGS, aliases=ALIASES))
    exit_with(agent.main)


if __name__ == "__main__":
    main()
