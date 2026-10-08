"""A fake Codex: ``codex app-server`` on a unix socket, and the TUI that attaches to it with ``--remote``.

The adapter runs the app server as a companion terminal and the TUI as the staff member's terminal;
both are imitated, with what the real 0.155.1 did (``recorded/codex``) where it was measured.

The app server (``codex app-server --listen unix://<path> [-c key=value]…``):

- The socket speaks WebSocket (an HTTP upgrade, then one JSON-RPC message per text frame, without
  the ``"jsonrpc"`` member); a connection that does not ask for the upgrade is closed, as the real
  server closed one that sent a bare JSON line.
- Requests: ``initialize`` (then the client's ``initialized`` notification), ``thread/start``
  (``developerInstructions`` is logged as ``developer_instructions``), ``thread/resume``,
  ``thread/inject_items``, ``thread/loaded/list``, ``skills/extraRoots/set`` (the skills found are
  logged as ``skills``), ``turn/start`` (``input``, ``clientUserMessageId``), ``turn/steer``
  (``expectedTurnId`` required; "no active turn to steer" when none runs), ``turn/interrupt``,
  ``thread/turns/list`` (newest first unless ``sortDirection`` is ``asc``), ``thread/items/list``,
  ``config/read``.
- A thread has no rollout until something is written to it (a turn, an injected item), and a thread
  without one cannot be resumed: "no rollout found for thread id …" (measured, for the TUI too).
- ``thread/started`` and ``thread/status/changed`` go to every client; everything about a thread's
  turns and items only to the clients subscribed to it (the one that started it and those that
  resumed it) — measured: an unsubscribed client saw only the first two.
- Notifications to subscribers: ``turn/started``, ``item/started`` / ``item/completed``
  (``userMessage`` with the ``clientId`` the client chose, ``agentMessage``, ``commandExecution``,
  ``mcpToolCall``), ``turn/completed`` (``completed``, ``interrupted``, ``failed`` with the error),
  ``error``, ``thread/tokenUsage/updated``, ``turn/diff/updated``, ``serverRequest/resolved``.
- Server requests for decisions: ``item/commandExecution/requestApproval`` (``accept``,
  ``acceptForSession``, ``decline``, ``cancel``) and ``item/tool/requestUserInput``. Which clients
  receive them is ``FAKE_CODEX_APPROVALS``: ``all`` (the default; every subscribed client, the first
  answer wins and the others are told ``serverRequest/resolved``) or ``owner`` (only the client that
  started the thread). The real routing could not be measured: the account was at its usage limit.
- The ``fail_at_start`` fault makes the app server print a configuration error and exit with 1.
- ``FAKE_CODEX_SOCKET=link`` binds the socket in a private directory and leaves a link at the path
  it was told to listen on, as Codex 0.157 and later do.
- Configuration overrides with ``-c`` are TOML values under dotted keys, as Codex parses them.
  ``mcp_servers.<name>={command=…,args=[…],env={…},env_vars=[…],tool_timeout_sec=N}`` starts an MCP
  server whose tools the script's ``report:`` / ``askorch:`` call; it inherits only a few basic
  variables plus those ``env_vars`` names, as Codex filters an MCP server's environment.
- A ``silent`` turn sends its end only to the TUI (the client that says it is ``codex-tui``): the other
  clients see a turn that never ends, the case only a screen reconcile finds.

The TUI (``codex --remote unix://<path> [-c …] resume <threadId>``, or ``codex --remote
unix://<path> [prompt]`` to start the thread itself): an available update shows the update prompt
unless its own ``-c check_for_update_on_startup=false`` says not to (a remote TUI asks no trust
question), then the thread is drawn and followed. Enter while idle is ``turn/start``, while busy
``turn/steer``; Esc is ``turn/interrupt``; approval requests that reach it are dialogs; ``/exit``
leaves the thread running in the server and exits 0.

Commands: ``--version`` (``codex-cli X``), ``login status``, ``debug models`` (slugs, some hidden),
``update``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tests.support.fake_cli.agent import FAILURE_TEXT  # noqa: E402
from tests.support.fake_cli.tui import (  # noqa: E402
    Args,
    Dialog,
    Faults,
    Log,
    Look,
    McpClient,
    Tui,
    ask_arguments,
    exit_with,
    installed_version,
    latest_version,
    logged_in,
    new_id,
    now_iso,
    pause,
    report_arguments,
    scaled,
    script_of,
    set_version,
    settle,
    usage_error,
)
from tests.support.fake_cli.websocket import Closed, Socket  # noqa: E402

DEFAULT_VERSION = "0.155.1"
DEFAULT_MODEL = "gpt-6-astra"
BASE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER")
SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
APPROVALS = ("on-request", "never")
GLOBAL_FLAGS = {"--remote": 1, "-c": 1, "--model": 1, "--sandbox": 1, "--ask-for-approval": 1, "--profile": 1, "--cd": 1, "--version": 0, "--listen": 1}
ALIASES = {"-m": "--model", "-s": "--sandbox", "-a": "--ask-for-approval", "-p": "--profile", "-C": "--cd", "-V": "--version", "--config": "-c"}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path(os.environ.get("HOME") or "/tmp") / ".codex")


def parse_overrides(values: list[str]) -> dict[str, Any]:
    """``-c key=value``: the value is TOML, or a plain string when it does not parse."""
    merged: dict[str, Any] = {}
    for item in values:
        key, eq, value = item.partition("=")
        if not eq:
            usage_error("codex", f"invalid override '{item}': expected key=value")
        try:
            parsed = tomllib.loads(f"{key.strip()} = {value.strip()}")
        except tomllib.TOMLDecodeError:
            parsed = tomllib.loads(f"{key.strip()} = {json.dumps(value.strip())}")
        deep_merge(merged, parsed)
    return merged


def deep_merge(into: dict[str, Any], other: dict[str, Any]) -> None:
    for key, value in other.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            deep_merge(into[key], value)
        else:
            into[key] = value


def file_config() -> dict[str, Any]:
    with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
        return tomllib.loads((codex_home() / "config.toml").read_text(encoding="utf-8"))
    return {}


def rpc_error(ident: Any, code: int, message: str) -> dict[str, Any]:
    return {"id": ident, "error": {"code": code, "message": message}}


# -- the app server ------------------------------------------------------------------------------------


class Conn:
    def __init__(self, server: AppServer, socket: Socket, number: int) -> None:
        self.server = server
        self.socket = socket
        self.number = number
        self.name = ""
        self.pending: dict[Any, asyncio.Future[Any]] = {}

    async def send(self, message: dict[str, Any]) -> None:
        with contextlib.suppress(ConnectionError, RuntimeError):
            await self.socket.send(json.dumps(message))


class Thread:
    def __init__(self, ident: str, cwd: str, owner: Conn, params: dict[str, Any]) -> None:
        self.id = ident
        self.cwd = cwd
        self.owner = owner
        self.subscribers: set[Conn] = {owner}
        self.model = str(params.get("model") or DEFAULT_MODEL)
        self.params = params
        self.turns: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.active: dict[str, Any] | None = None
        self.task: asyncio.Task[None] | None = None
        self.steers: list[tuple[str, str]] = []
        self.silent = False
        self.created_at = int(time.time())
        self.rollout = codex_home() / "sessions" / time.strftime("%Y/%m/%d") / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{ident}.jsonl"
        self.written = False
        """The rollout exists only once something was written to the thread (measured): until then the
        thread cannot be resumed, by the TUI or anyone."""
        self.status: dict[str, Any] = {"type": "idle"}

    def view(self) -> dict[str, Any]:
        first = next((i for i in self.items if i["type"] == "userMessage"), None)
        preview = first["content"][0]["text"][:80] if first else ""
        return {"id": self.id, "preview": preview, "modelProvider": "openai", "model": self.model, "ephemeral": False, "createdAt": self.created_at, "cwd": self.cwd, "path": str(self.rollout), "status": self.status, "turns": []}


class AppServer:
    def __init__(self, path: str, config: dict[str, Any], log: Log) -> None:
        self.path = path
        self.config = config
        self.log = log
        self.threads: dict[str, Thread] = {}
        self.conns: list[Conn] = []
        self.next_request = 0
        self.mcp: dict[str, McpClient] = {}
        self.skill_roots: list[str] = []
        self.approvals = os.environ.get("FAKE_CODEX_APPROVALS", "all")
        self.faults = Faults.from_env()
        self.turns_done = 0

    def say(self, text: str) -> None:
        sys.stdout.write(text + "\r\n")
        sys.stdout.flush()

    async def serve(self) -> int:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        for name, spec in (self.config.get("mcp_servers") or {}).items():
            # Codex starts an MCP server with a filtered environment: a variable reaches it only when the
            # server's entry names it in ``env_vars`` or sets it in ``env``.
            base = {k: os.environ[k] for k in (*BASE_ENV, *(str(v) for v in spec.get("env_vars") or [])) if k in os.environ}
            client = McpClient(name, str(spec.get("command", "")), [str(a) for a in spec.get("args") or []], {str(k): str(v) for k, v in (spec.get("env") or {}).items()}, base_env=base)
            if await client.start():
                self.mcp[name] = client
                self.say(f"mcp server {name}: {len(client.tools)} tools")
            else:
                self.say(f"mcp server {name} failed: {client.error}")
        bound = self.path
        if os.environ.get("FAKE_CODEX_SOCKET") == "link":
            # As Codex from 0.157 on does (measured on 0.160.0): the socket is bound in a private
            # directory of its own and the path it was told to listen on becomes a link to it.
            bound = os.path.join(tempfile.mkdtemp(prefix="codex-daemon-"), "app.sock")
        server = await asyncio.start_unix_server(self.connection, path=bound, limit=1 << 22)
        if bound != self.path:
            os.symlink(bound, self.path)
        self.say(f"codex app-server listening on unix://{self.path}")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (15, 1, 2):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        server.close()
        if bound != self.path:
            shutil.rmtree(os.path.dirname(bound), ignore_errors=True)
        for client in self.mcp.values():
            await client.close()
        return 0

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        socket = await Socket.accept(reader, writer)
        if socket is None:
            self.log("app_server_refused", reason="no websocket upgrade")
            return
        conn = Conn(self, socket, len(self.conns) + 1)
        self.conns.append(conn)
        self.log("app_server_connection", number=conn.number)
        try:
            while (line := await socket.receive()) is not None:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    await conn.send(rpc_error(None, -32700, "parse error"))
                    continue
                if "method" in message and "id" in message:
                    asyncio.ensure_future(self.request(conn, message))
                elif "method" in message:
                    pass  # ``initialized`` and other client notifications need no answer
                elif "id" in message:
                    future = conn.pending.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message.get("result") if "result" in message else {"error": message.get("error")})
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.conns.remove(conn)
            for thread in self.threads.values():
                thread.subscribers.discard(conn)
            for future in conn.pending.values():
                if not future.done():
                    future.set_result(None)
            socket.close()

    async def request(self, conn: Conn, message: dict[str, Any]) -> None:
        ident, method, params = message["id"], message["method"], message.get("params") or {}
        try:
            result = await self.handle(conn, method, params)
        except RpcFailure as exc:
            await conn.send(rpc_error(ident, exc.code, exc.message))
            return
        await conn.send({"id": ident, "result": result})

    def thread_of(self, params: dict[str, Any]) -> Thread:
        thread = self.threads.get(str(params.get("threadId")))
        if thread is None:
            raise RpcFailure(-32600, f"thread not found: {params.get('threadId')}")
        return thread

    async def broadcast(self, method: str, params: dict[str, Any]) -> None:
        for conn in list(self.conns):
            await conn.send({"method": method, "params": params})

    async def handle(self, conn: Conn, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            conn.name = str((params.get("clientInfo") or {}).get("name") or "")
            return {"userAgent": f"{conn.name}/{installed_version('codex', DEFAULT_VERSION)} (fake)", "codexHome": str(codex_home()), "platformFamily": "unix", "platformOs": "linux"}
        if method == "skills/extraRoots/set":
            self.skill_roots = [str(r) for r in params.get("extraRoots") or []]
            names = sorted(p.parent.name for root in self.skill_roots for p in Path(root).glob("*/SKILL.md"))
            self.log("skills", names=names, roots=self.skill_roots)
            return {}
        if method == "config/read":
            return {"config": self.effective_config()}
        if method == "thread/start":
            thread = Thread(new_id(), str(params.get("cwd") or os.getcwd()), conn, params)
            self.threads[thread.id] = thread
            if params.get("developerInstructions"):
                self.log("developer_instructions", text=str(params["developerInstructions"]))
            # Every client hears that a thread started; only its subscribers hear what happens in it.
            await self.broadcast("thread/started", {"thread": thread.view()})
            return {"thread": thread.view(), "model": thread.model, "cwd": thread.cwd, "approvalPolicy": params.get("approvalPolicy", "on-request"), "sandbox": params.get("sandbox", "workspace-write")}
        if method == "thread/resume":
            thread = self.threads.get(str(params.get("threadId")))
            if thread is None or not thread.written:
                raise RpcFailure(-32600, f"no rollout found for thread id {params.get('threadId')}")
            thread.subscribers.add(conn)
            if params.get("developerInstructions"):
                self.log("developer_instructions", text=str(params["developerInstructions"]))
            turns = [] if params.get("excludeTurns") else thread.turns
            return {"thread": {**thread.view(), "turns": turns}, "model": thread.model, "cwd": thread.cwd}
        if method == "thread/inject_items":
            thread = self.thread_of(params)
            for item in params.get("items") or []:
                self.rollout(thread, "response_item", item)
            return {}
        if method == "thread/loaded/list":
            return {"data": list(self.threads)}
        if method == "turn/start":
            thread = self.thread_of(params)
            if thread.task is not None and not thread.task.done():
                raise RpcFailure(-32600, "a turn is already running on this thread")
            text = text_of(params.get("input"))
            turn: dict[str, Any] = {"id": new_id(), "status": "inProgress", "items": [], "error": None}
            thread.active = turn
            thread.turns.append(turn)
            thread.task = asyncio.ensure_future(self.run_turn(thread, turn, text, str(params.get("clientUserMessageId") or "")))
            return {"turn": {"id": turn["id"], "status": "inProgress", "items": [], "error": None}}
        if method == "turn/steer":
            thread = self.thread_of(params)
            if not params.get("expectedTurnId"):
                raise RpcFailure(-32602, "expectedTurnId is required")
            if thread.active is None:
                raise RpcFailure(-32600, "no active turn to steer")
            if thread.active["id"] != params["expectedTurnId"]:
                raise RpcFailure(-32600, "precondition failed: expectedTurnId does not match the active turn")
            thread.steers.append((text_of(params.get("input")), str(params.get("clientUserMessageId") or "")))
            return {"turnId": thread.active["id"]}
        if method == "turn/interrupt":
            thread = self.thread_of(params)
            if thread.active is None or thread.active["id"] != params.get("turnId"):
                raise RpcFailure(-32600, "no active turn to interrupt")
            assert thread.task is not None
            thread.task.cancel()
            return {}
        if method == "thread/turns/list":
            thread = self.thread_of(params)
            turns = [{"id": t["id"], "status": t["status"], "error": t["error"]} for t in thread.turns]
            if params.get("sortDirection", "desc") == "desc":
                turns.reverse()
            return page(turns, params)
        if method == "thread/items/list":
            thread = self.thread_of(params)
            items = thread.items if not params.get("turnId") else [i for i in thread.items if i.get("turnId") == params["turnId"]]
            return page(items, params)
        raise RpcFailure(-32601, f"unknown method: {method}")

    def effective_config(self) -> dict[str, Any]:
        merged = file_config()
        deep_merge(merged, self.config)
        return merged

    async def notify(self, thread: Thread, method: str, params: dict[str, Any], *, tui_only: bool = False) -> None:
        for conn in list(thread.subscribers):
            if tui_only and conn.name != "codex-tui":
                continue
            await conn.send({"method": method, "params": params})

    async def ask_clients(self, thread: Thread, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """A server request; the first answer wins, and the others are told it was resolved."""
        targets = [c for c in thread.subscribers if self.approvals == "all" or c is thread.owner]
        self.next_request += 1
        ident = self.next_request
        futures = []
        for conn in targets:
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            conn.pending[ident] = future
            futures.append((conn, future))
            await conn.send({"id": ident, "method": method, "params": params})
        self.log("server_request", method=method, id=ident, clients=[c.name for c in targets])
        if not futures:
            return {}
        try:
            done, _ = await asyncio.wait([f for _, f in futures], return_when=asyncio.FIRST_COMPLETED)
            answer = next(iter(done)).result() or {}
        finally:
            for conn, future in futures:
                conn.pending.pop(ident, None)
                if not future.done():
                    future.cancel()
        winner = next((c for c, f in futures if f.done() and not f.cancelled()), None)
        self.log("server_request_answered", id=ident, by=winner.name if winner else "", answer=answer)
        await self.notify(thread, "serverRequest/resolved", {"threadId": thread.id, "requestId": ident})
        return answer if isinstance(answer, dict) else {}

    def rollout(self, thread: Thread, kind: str, payload: dict[str, Any]) -> None:
        if not thread.written:
            thread.written = True
            thread.rollout.parent.mkdir(parents=True, exist_ok=True)
            with thread.rollout.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": now_iso(), "type": "session_meta", "payload": {"id": thread.id, "cwd": thread.cwd, "cli_version": installed_version("codex", DEFAULT_VERSION)}}) + "\n")
        with thread.rollout.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": now_iso(), "type": kind, "payload": payload}) + "\n")

    async def item(self, thread: Thread, turn: dict[str, Any], item: dict[str, Any], *, started: bool = True, completed: bool = True) -> None:
        item.setdefault("id", new_id())
        record = {**item, "turnId": turn["id"]}
        if started:
            await self.notify(thread, "item/started", {"threadId": thread.id, "turnId": turn["id"], "item": item})
        if completed:
            thread.items.append(record)
            turn["items"].append(item["id"])
            await self.notify(thread, "item/completed", {"threadId": thread.id, "turnId": turn["id"], "item": item})
            if item["type"] in ("userMessage", "agentMessage"):
                role = "user" if item["type"] == "userMessage" else "assistant"
                text = item["content"][0]["text"] if role == "user" else item["text"]
                self.rollout(thread, "response_item", {"type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]})

    async def status(self, thread: Thread, status: dict[str, Any], *, tui_only: bool = False) -> None:
        thread.status = status
        if tui_only:
            await self.notify(thread, "thread/status/changed", {"threadId": thread.id, "status": status}, tui_only=True)
        else:
            await self.broadcast("thread/status/changed", {"threadId": thread.id, "status": status})

    async def user_message(self, thread: Thread, turn: dict[str, Any], text: str, client_id: str) -> None:
        item: dict[str, Any] = {"type": "userMessage", "id": new_id(), "content": [{"type": "text", "text": text}]}
        if client_id:
            item["clientId"] = client_id
        await self.item(thread, turn, item)

    async def run_turn(self, thread: Thread, turn: dict[str, Any], text: str, client_id: str) -> None:
        steps = script_of(text)
        # A silent turn, or the missing-end fault, ends only for the TUI: every other client is left
        # with a turn that never finishes.
        silent = any(s.kind == "silent" for s in steps) or self.faults.no_stop_hook
        await self.status(thread, {"type": "active", "activeFlags": []})
        await self.notify(thread, "turn/started", {"threadId": thread.id, "turn": {"id": turn["id"], "status": "inProgress"}})
        await self.user_message(thread, turn, text, client_id)
        outcome, error = "completed", None
        try:
            for step in steps:
                await self.step(thread, turn, step.kind, step.arg)
                await self.take_steers(thread, turn)
        except asyncio.CancelledError:
            outcome = "interrupted"
        except TurnFailure as failure:
            outcome = "failed"
            error = {"message": FAILURE_TEXT.get(failure.kind, failure.kind), "codexErrorInfo": failure.kind}
            await self.notify(thread, "error", {"threadId": thread.id, "turnId": turn["id"], "error": error, "willRetry": False})
        turn["status"], turn["error"] = outcome, error
        thread.active = None
        thread.steers.clear()
        usage = {"inputTokens": 1200, "cachedInputTokens": 800, "outputTokens": 90, "reasoningOutputTokens": 10, "totalTokens": 1290}
        self.rollout(thread, "event_msg", {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 90}}})
        self.rollout(thread, "event_msg", {"type": "task_complete", "turn_id": turn["id"], "last_agent_message": None})
        await self.notify(thread, "thread/tokenUsage/updated", {"threadId": thread.id, "turnId": turn["id"], "tokenUsage": {"total": usage, "last": usage}}, tui_only=silent)
        await self.notify(thread, "turn/completed", {"threadId": thread.id, "turn": {"id": turn["id"], "status": outcome, "error": error}}, tui_only=silent)
        await self.status(thread, {"type": "systemError"} if error and error["codexErrorInfo"] == "system" else {"type": "idle"}, tui_only=silent)
        self.turns_done += 1
        if self.faults.exit_after and self.turns_done >= self.faults.exit_after:
            self.log("exit", code=3, crash=True)
            os._exit(3)

    async def take_steers(self, thread: Thread, turn: dict[str, Any]) -> None:
        """Steered messages join the running turn between tool calls, each with its own ``clientId``."""
        while thread.steers:
            steer, steer_id = thread.steers.pop(0)
            await self.user_message(thread, turn, steer, steer_id)
            for extra in script_of(steer):
                await self.step(thread, turn, extra.kind, extra.arg)

    async def step(self, thread: Thread, turn: dict[str, Any], kind: str, arg: str) -> None:
        item: dict[str, Any]
        if kind == "echo":
            await pause(0.3)
            await self.item(thread, turn, {"type": "agentMessage", "text": arg})
        elif kind == "perm":
            item = {"type": "commandExecution", "id": new_id(), "command": arg, "cwd": thread.cwd, "status": "inProgress", "aggregatedOutput": None, "exitCode": None}
            await self.item(thread, turn, item, completed=False)
            if thread.params.get("approvalPolicy") == "never" or thread.params.get("sandbox") == "danger-full-access":
                decision = "accept"
            else:
                await self.status(thread, {"type": "active", "activeFlags": ["waitingOnApproval"]})
                answer = await self.ask_clients(thread, "item/commandExecution/requestApproval", {"threadId": thread.id, "turnId": turn["id"], "itemId": item["id"], "command": arg, "cwd": thread.cwd, "reason": f"run {arg}"})
                decision = str(answer.get("decision") or "decline")
                await self.status(thread, {"type": "active", "activeFlags": []})
            if decision == "cancel":
                raise asyncio.CancelledError
            if decision in ("accept", "acceptForSession"):
                await pause(0.2)
                item.update(status="completed", aggregatedOutput=f"(the fake did not run {arg})", exitCode=0)
                await self.item(thread, turn, item, started=False)
                await self.notify(thread, "turn/diff/updated", {"threadId": thread.id, "turnId": turn["id"], "diff": f"--- a/fake\n+++ b/fake\n@@\n+{arg}\n"})
                await self.item(thread, turn, {"type": "agentMessage", "text": f"Ran {arg}."})
            else:
                item.update(status="declined")
                await self.item(thread, turn, item, started=False)
                await self.item(thread, turn, {"type": "agentMessage", "text": f"Understood, I did not run {arg}."})
        elif kind == "ask":
            spec = ask_arguments(arg)
            await self.status(thread, {"type": "active", "activeFlags": ["waitingOnUserInput"]})
            question = {"id": "q1", "header": "Question", "question": spec["question"], "options": [{"label": o, "description": ""} for o in spec.get("options", [])]}
            answer = await self.ask_clients(thread, "item/tool/requestUserInput", {"threadId": thread.id, "turnId": turn["id"], "itemId": new_id(), "questions": [question]})
            await self.status(thread, {"type": "active", "activeFlags": []})
            chosen = ((answer.get("answers") or {}).get("q1") or {}).get("answers") or ["(no answer)"]
            await self.item(thread, turn, {"type": "agentMessage", "text": f"You chose: {chosen[0]}"})
        elif kind == "fail":
            await pause(0.2)
            raise TurnFailure(arg or "unknown")
        elif kind == "slow":
            loop = asyncio.get_running_loop()
            end, n = loop.time() + scaled(float(arg or 1)), 0
            while loop.time() < end:
                n += 1
                item = {"type": "commandExecution", "id": new_id(), "command": f"sed -n 1,40p file{n}.txt", "cwd": thread.cwd, "status": "inProgress", "aggregatedOutput": None, "exitCode": None}
                await self.item(thread, turn, item, completed=False)
                await pause(0.5)
                item.update(status="completed", aggregatedOutput=f"line {n}", exitCode=0)
                await self.item(thread, turn, item, started=False)
                await self.take_steers(thread, turn)
            await self.item(thread, turn, {"type": "agentMessage", "text": f"Worked through {n} files."})
        elif kind == "silent":
            await pause(3)
        elif kind in ("report", "askorch"):
            name = "Report" if kind == "report" else "AskOrchestrator"
            arguments = report_arguments(arg) if kind == "report" else ask_arguments(arg)
            client = self.mcp.get("daedalus_team")
            item = {"type": "mcpToolCall", "id": new_id(), "server": "daedalus_team", "tool": name, "arguments": arguments, "status": "inProgress", "result": None}
            await self.item(thread, turn, item, completed=False)
            timeout = float(((self.config.get("mcp_servers") or {}).get("daedalus_team") or {}).get("tool_timeout_sec") or 60)
            if client is None:
                result = "error: no MCP server daedalus_team"
            else:
                try:
                    result = await client.call(name, arguments, timeout)
                except (TimeoutError, ConnectionError, RuntimeError) as exc:
                    result = f"error: {exc or 'timed out'}"
            item.update(status="completed", result={"content": [{"type": "text", "text": result}]})
            await self.item(thread, turn, item, started=False)
            await self.item(thread, turn, {"type": "agentMessage", "text": f"{name}: {result}"})


class RpcFailure(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TurnFailure(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


def text_of(items: Any) -> str:
    return "".join(str(i.get("text", "")) for i in items or [] if isinstance(i, dict) and i.get("type") == "text")


def page(items: list[Any], params: dict[str, Any]) -> dict[str, Any]:
    start = int(params.get("cursor") or 0)
    limit = int(params.get("limit") or 50)
    chunk = items[start : start + limit]
    return {"data": chunk, "nextCursor": str(start + limit) if start + limit < len(items) else None}


# -- the TUI ------------------------------------------------------------------------------------------


class CodexTui:
    look = Look("codex", ">_ OpenAI Codex (fake)", prompt="› ", idle_hint="⏎ send   ⌃J newline   ⌃C quit", busy_hint="esc to interrupt",
                busy_word="Working", alt_screen=False, collapse_chars=1000, collapse_lines=0, burst_guard_ms=0)

    def __init__(self, socket_path: str, args: Args, log: Log) -> None:
        self.path = socket_path
        self.args = args
        self.log = log
        self.faults = Faults.from_env()
        self.tui = Tui(self.look, faults=self.faults, log=log)
        self.tui.submit = self.submit
        self.tui.busy_enter = self.steer
        self.tui.escape = self.interrupt
        self.tui.quit = self.quit
        self.socket: Socket | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.thread_id = ""
        self.turn_id = ""
        self.open_requests: dict[Any, asyncio.Future[Any]] = {}
        self.done = asyncio.Event()
        self.code = 0

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        assert self.socket is not None
        self.next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        await self.socket.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        return await future

    async def main(self) -> int:
        self.tui.start()
        try:
            try:
                self.socket = await Socket.connect(self.path)
            except (OSError, Closed) as exc:
                self.tui.say(f"■ Failed to connect to the app server at unix://{self.path}: {exc}")
                await asyncio.sleep(0.2)
                return 1
            asyncio.ensure_future(self.read())
            await self.call("initialize", {"clientInfo": {"name": "codex-tui", "version": installed_version("codex", DEFAULT_VERSION)}})
            await self.socket.send('{"method": "initialized"}')
            # The TUI's own settings (``-c`` on its command line over its config file) decide its
            # update prompt; the server's do not (measured: the prompt came with the server switched
            # off). A remote TUI asks no trust question.
            config = file_config()
            deep_merge(config, parse_overrides(self.args.all("-c")))
            cwd = self.args.get("--cd") or os.getcwd()
            current, latest = installed_version("codex", DEFAULT_VERSION), latest_version("codex", DEFAULT_VERSION)
            if current != latest and config.get("check_for_update_on_startup") is not False:
                await self.ask("update", f"✨ Update available! {current} -> {latest}", [], ["Update now (runs `npm install -g @openai/codex`)", "Skip", "Skip until next version"])
            positional = self.args.positional
            if positional[:1] == ["resume"]:
                if len(positional) < 2:
                    usage_error("codex", "resume needs a thread id with --remote")
                resumed = await self.call("thread/resume", {"threadId": positional[1]})
                if "error" in (resumed or {}):
                    self.tui.say(f"Error: Failed to resume session: thread/resume failed during TUI bootstrap: thread/resume failed: {resumed['error'].get('message')}")
                    await asyncio.sleep(0.2)
                    return 1
                self.thread_id = positional[1]
                self.tui.say(f"resumed thread {self.thread_id}")
                prompt = None
            else:
                started = await self.call("thread/start", {"cwd": cwd, "model": self.args.get("--model") or None})
                self.thread_id = started["thread"]["id"]
                prompt = positional[0] if positional else None
            if self.faults.slow_ready_ms:
                await asyncio.sleep(self.faults.slow_ready_ms / 1000)
            self.tui.ready = True
            self.tui.render()
            self.log("ready", thread=self.thread_id)
            if prompt:
                await self.submit(prompt)
            await self.done.wait()
            return self.code
        finally:
            self.tui.restore()

    async def ask(self, kind: str, title: str, body: list[str], options: list[str]) -> int:
        future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.tui.open_dialog(Dialog(kind, title, body, options, on_choose=lambda i: settle(future, i), on_escape=lambda: settle(future, len(options) - 1)))
        return await future

    async def read(self) -> None:
        assert self.socket is not None
        while (line := await self.socket.receive()) is not None:
            with contextlib.suppress(json.JSONDecodeError):
                message = json.loads(line)
                if "method" in message and "id" in message:
                    asyncio.ensure_future(self.server_request(message))
                elif "method" in message:
                    self.notification(message["method"], message.get("params") or {})
                else:
                    future = self.pending.pop(message.get("id"), None)
                    if future is not None and not future.done():
                        future.set_result(message.get("result") if "result" in message else {"error": message.get("error")})
        self.tui.say("■ The app server went away.")
        await self.quit(1)

    def notification(self, method: str, params: dict[str, Any]) -> None:
        if params.get("threadId") not in (None, self.thread_id):
            return
        if method == "turn/started":
            self.turn_id = params["turn"]["id"]
            self.tui.busy = True
            self.tui.status = f"• {self.look.busy_word} ({self.look.busy_hint})"
        elif method == "turn/completed":
            status = params["turn"]["status"]
            self.turn_id = ""
            self.tui.busy = False
            self.tui.status = ""
            if status == "interrupted":
                self.tui.say("■ Conversation interrupted - tell the model what to do differently.")
            elif status == "failed":
                self.tui.say(f"■ {(params['turn'].get('error') or {}).get('message', 'the turn failed')}")
        elif method == "serverRequest/resolved":
            future = self.open_requests.pop(params.get("requestId"), None)
            if future is not None and not future.done():
                future.cancel()
                if self.tui.dialog is not None and self.tui.dialog.kind in ("permission", "question"):
                    self.tui.close_dialog()
        elif method == "item/completed":
            item = params["item"]
            if item["type"] == "userMessage":
                self.tui.say(f"› {item['content'][0]['text']}")
            elif item["type"] == "agentMessage":
                self.tui.say(f"• {item['text']}")
            elif item["type"] == "commandExecution":
                self.tui.say(f"• Ran {item['command']}", f"  └ {item.get('aggregatedOutput') or item.get('status')}")
            elif item["type"] == "mcpToolCall":
                self.tui.say(f"• Called {item['server']}.{item['tool']}")
        self.tui.render()

    async def server_request(self, message: dict[str, Any]) -> None:
        """A decision the server asks this client for, as a dialog. When another client answers
        first, ``serverRequest/resolved`` closes the dialog and nothing is sent."""
        assert self.socket is not None
        method, params = message["method"], message.get("params") or {}
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.open_requests[message["id"]] = future
        if method == "item/commandExecution/requestApproval":
            meanings = ["accept", "acceptForSession", "decline"]
            self.tui.open_dialog(Dialog("permission", "Would you like to run the following command?", [f"  $ {params.get('command')}"], ["Yes, proceed", "Yes, and don't ask again for this command", "No, and tell Codex what to do differently"],
                                        on_choose=lambda i: settle(future, {"decision": meanings[i]}), on_escape=lambda: settle(future, {"decision": "decline"})))
        elif method == "item/tool/requestUserInput":
            question = (params.get("questions") or [{}])[0]
            labels = [o["label"] for o in question.get("options") or []] or ["(free text)"]
            key = question.get("id", "q1")
            self.tui.open_dialog(Dialog("question", str(question.get("question")), [], labels, on_choose=lambda i: settle(future, {"answers": {key: {"answers": [labels[i]]}}}),
                                        on_escape=lambda: settle(future, {"answers": {key: {"answers": ["(no answer)"]}}})))
        else:
            self.open_requests.pop(message["id"], None)
            await self.socket.send(json.dumps(rpc_error(message["id"], -32601, "unsupported")))
            return
        try:
            result = await future
        except asyncio.CancelledError:
            return
        finally:
            self.open_requests.pop(message["id"], None)
        with contextlib.suppress(ConnectionError):
            await self.socket.send(json.dumps({"id": message["id"], "result": result}))

    async def submit(self, text: str) -> None:
        answer = await self.call("turn/start", {"threadId": self.thread_id, "input": [{"type": "text", "text": text}]})
        if "error" in (answer or {}):
            self.tui.say(f"■ {answer['error'].get('message')}")

    async def steer(self, text: str) -> None:
        answer = await self.call("turn/steer", {"threadId": self.thread_id, "input": [{"type": "text", "text": text}], "expectedTurnId": self.turn_id})
        if "error" in (answer or {}):
            await self.submit(text)

    async def interrupt(self) -> None:
        if self.tui.dialog is None and self.turn_id:
            self.log("interrupt", key="esc")
            await self.call("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})

    async def quit(self, code: int = 0) -> None:
        if code == 0:
            # What the real TUI says on /exit: the thread goes on in the app server.
            self.tui.say("Disconnected from this task. Any running work continues.", f"Reconnect: codex --remote unix://{self.path} resume {self.thread_id}")
        self.log("exit", code=code)
        self.code = code
        self.tui.exiting = True
        self.done.set()


# -- commands -----------------------------------------------------------------------------------------


def main() -> None:
    argv = sys.argv[1:]
    log = Log("codex")
    if argv[:1] in (["--version"], ["-V"]):
        print(f"codex-cli {installed_version('codex', DEFAULT_VERSION)}")
        raise SystemExit(0)
    if argv[:2] == ["login", "status"]:
        print("Logged in using ChatGPT" if logged_in("codex") else "Not logged in")
        raise SystemExit(0 if logged_in("codex") else 1)
    if argv[:2] == ["debug", "models"]:
        # The shape 0.155.1 printed (recorded): slugs, and Codex's own models hidden.
        levels = [{"effort": e, "description": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]
        models = [("gpt-6-astra", "list"), ("gpt-6-sol", "list"), ("gpt-6-luna", "list"), ("gpt-reserve", "hide"), ("gpt-5.5", "list"), ("codex-auto-review", "hide")]
        print(json.dumps({"models": [{"slug": slug, "display_name": slug.upper(), "default_reasoning_level": "medium", "supported_reasoning_levels": levels, "visibility": visibility} for slug, visibility in models]}))
        raise SystemExit(0)
    if argv[:1] == ["update"]:
        current, latest = installed_version("codex", DEFAULT_VERSION), latest_version("codex", DEFAULT_VERSION)
        if current != latest:
            set_version("codex", latest)
        print(f"Updated Codex CLI from {current} to {latest}" if current != latest else f"Codex CLI is up to date ({current})")
        raise SystemExit(0)
    if argv[:1] == ["app-server"]:
        args = Args("codex", argv[1:], flags=GLOBAL_FLAGS, aliases=ALIASES)
        listen = args.get("--listen")
        if not listen.startswith("unix://"):
            usage_error("codex", "the fake app server listens on unix://<path> only")
        if Faults.from_env().fail_at_start:
            print("Error: failed to load configuration: unknown field `model_catalog_json`", flush=True)
            raise SystemExit(1)
        server = AppServer(listen[len("unix://") :], parse_overrides(args.all("-c")), log)
        exit_with(server.serve)
        return
    args = Args("codex", argv, flags=GLOBAL_FLAGS, aliases=ALIASES)
    for name, allowed in (("--sandbox", SANDBOXES), ("--ask-for-approval", APPROVALS)):
        if args.get(name) and args.get(name) not in allowed:
            usage_error("codex", f"invalid value '{args.get(name)}' for '{name}'")
    remote = args.get("--remote")
    if not remote.startswith("unix://"):
        usage_error("codex", "the fake TUI runs only against an app server: --remote unix://<path>")
    tui = CodexTui(remote[len("unix://") :], args, log)
    exit_with(tui.main)


if __name__ == "__main__":
    main()
