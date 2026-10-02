"""Keep one Cursor ACP session behind a terminal and a launch-local control socket.

Cursor's CLI hooks do not run in ACP mode. The bridge uses the documented ACP stream for turn,
tool and approval events, and keeps the operator's terminal useful for reading and typing prompts.
It never edits Cursor's user configuration or copies its login into the launch directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any


class Bridge:
    def __init__(self, config: dict[str, Any], socket_path: str) -> None:
        self.config = config
        self.socket_path = socket_path
        self.process: asyncio.subprocess.Process | None = None
        self.session_id = ""
        self.next_id = 0
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.permissions: dict[str, dict[str, Any]] = {}
        self.clients: set[asyncio.StreamWriter] = set()
        self.busy = False
        self.started = False
        self.acknowledged = False
        self.message_id = ""
        self.prompt = ""
        self.answer = ""
        self.tool_status: dict[str, str] = {}
        self.transcript = str(Path(config["launch_dir"]) / "cursor-turns.jsonl")
        self.stopping = False

    async def emit(self, kind: str, payload: dict[str, Any] | None = None, native_id: str = "") -> None:
        event = {"type": "event", "kind": kind, "payload": payload or {}, "native_id": native_id}
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode()
        for client in tuple(self.clients):
            try:
                client.write(data)
                await client.drain()
            except (ConnectionError, OSError):
                self.clients.discard(client)

    async def write_acp(self, message: dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def call(self, method: str, params: dict[str, Any], timeout: float = 45) -> dict[str, Any]:
        self.next_id += 1
        ident = self.next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.write_acp({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(ident, None)
        if "error" in reply:
            error = reply["error"]
            raise RuntimeError(str(error.get("message") if isinstance(error, dict) else error))
        return reply.get("result") or {}

    async def read_acp(self) -> None:
        assert self.process and self.process.stdout
        while line := await self.process.stdout.readline():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:
                await self.request(message)
            elif message.get("method") == "session/update":
                await self.update(message.get("params") or {})
            elif "id" in message:
                future = self.pending.get(message["id"])
                if future and not future.done():
                    future.set_result(message)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Cursor ACP closed"))
        if not self.stopping:
            await self.emit("turn_failed", {"failure": "Cursor ACP closed unexpectedly"})

    async def request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        ident = message["id"]
        ref = f"cursor-{ident}"
        if method == "session/request_permission":
            options = [option for option in params.get("options") or [] if isinstance(option, dict)]
            self.permissions[ref] = {"id": ident, "method": method, "options": options}
            tool = params.get("toolCall") or {}
            summary = str(tool.get("title") or tool.get("name") or "Cursor asks to run a tool")[:300]
            await self.emit("permission_requested", {"tool": str(tool.get("kind") or "tool"), "summary": summary, "options": ["allow_once", "allow_always", "deny"]}, ref)
            print(f"\nApproval requested: {summary}\nUse the staff approval control or type /allow {ref} or /deny {ref} here.", flush=True)
            return
        if method == "cursor/ask_question":
            self.permissions[ref] = {"id": ident, "method": method, "params": params}
            questions = params.get("questions") or []
            summary = "\n".join(str(item.get("prompt") or "") for item in questions if isinstance(item, dict))[:300]
            options = [str(option.get("label") or "") for item in questions if isinstance(item, dict) for option in item.get("options") or [] if isinstance(option, dict)]
            await self.emit("question_asked", {"tool": "question", "summary": summary, "text": summary, "options": options}, ref)
            print(f"\nCursor asks: {summary}\nAnswer from the staff view, or type /answer {ref} <text> here.", flush=True)
            return
        if method == "cursor/create_plan":
            self.permissions[ref] = {"id": ident, "method": method, "params": params}
            summary = str(params.get("overview") or params.get("plan") or "Cursor proposes a plan")[:300]
            await self.emit("permission_requested", {"tool": "plan", "summary": summary, "options": ["allow_once", "deny"]}, ref)
            print(f"\nCursor proposes a plan: {summary}\nUse the staff approval control or type /allow {ref} or /deny {ref} here.", flush=True)
            return
        await self.write_acp({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "unsupported client request"}})

    async def update(self, params: dict[str, Any]) -> None:
        if params.get("sessionId") not in (None, self.session_id):
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if self.busy and not self.acknowledged and kind in ("agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"):
            self.acknowledged = True
            await self.emit("prompt_acknowledged", {"prompt": self.prompt, "message_id": self.message_id})
        if self.busy and not self.started and self.acknowledged:
            self.started = True
            await self.emit("turn_started")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = str(content.get("text") or "")
            self.answer += text
            print(text, end="", flush=True)
            await self.emit("activity", {"source": "agent_message"})
        elif kind in ("tool_call", "tool_call_update"):
            ref = str(update.get("toolCallId") or "")
            status = str(update.get("status") or "")
            title = str(update.get("title") or "tool")
            previous = self.tool_status.get(ref)
            if ref and previous is None:
                self.tool_status[ref] = status or "pending"
                print(f"\n[{title}]", flush=True)
                await self.emit("tool_started", {"tool": title}, ref)
            if ref and status in ("completed", "failed") and previous not in ("completed", "failed"):
                self.tool_status[ref] = status
                await self.emit("tool_finished", {"tool": title, "ok": status == "completed"}, ref)
        elif kind in ("agent_thought_chunk", "session_info_update"):
            await self.emit("activity", {"source": str(kind)})

    def record(self, role: str, text: str) -> None:
        with open(self.transcript, "a", encoding="utf-8") as file:
            file.write(json.dumps({"role": role, "text": text}, ensure_ascii=False) + "\n")

    async def prompt_turn(self, text: str, message_id: str) -> None:
        self.busy = True
        self.started = False
        self.acknowledged = False
        self.message_id = message_id
        self.prompt = text
        self.answer = ""
        self.tool_status.clear()
        self.record("user", text)
        print("\nCursor Agent · working", flush=True)
        try:
            result = await self.call("session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]}, timeout=3600)
            if not self.acknowledged:
                await self.emit("prompt_acknowledged", {"prompt": text, "message_id": message_id})
            if self.answer:
                self.record("assistant", self.answer)
            reason = str(result.get("stopReason") or "")
            if reason == "cancelled":
                await self.emit("turn_cancelled", {"via": "session/cancel"})
            elif reason in ("end_turn", "max_tokens"):
                await self.emit("turn_completed", {"last_message": self.answer})
            else:
                await self.emit("turn_failed", {"failure": reason or "Cursor ended without a reason"})
        except Exception as exc:
            await self.emit("turn_failed", {"failure": str(exc)[:300]})
            print(f"\nCursor Agent: {exc}", flush=True)
        finally:
            self.busy = False
            for ref in list(self.permissions):
                await self.answer_request(ref, "deny", "")
            print("\ncursor> ", end="", flush=True)

    async def answer_request(self, ref: str, choice: str, said: str) -> bool:
        request = self.permissions.pop(ref, None)
        if request is None:
            return False
        if request["method"] == "session/request_permission":
            wanted = "allow_always" if choice == "allow_always" else "allow_once" if choice.startswith("allow") else "reject_once"
            option = next((x for x in request["options"] if x.get("kind") == wanted), None)
            if option is None:
                option = next((x for x in request["options"] if wanted.replace("_", "-") in str(x.get("optionId") or "")), None)
            if option is None:
                option = next((x for x in request["options"] if "reject" in str(x.get("optionId") or "")), None)
            outcome = {"outcome": "selected", "optionId": option["optionId"]} if option else {"outcome": "cancelled"}
        elif request["method"] == "cursor/create_plan":
            outcome = {"outcome": "accepted" if choice.startswith("allow") else "rejected"}
        else:
            params = request["params"]
            questions = params.get("questions") or []
            answers = []
            for question in questions:
                if not isinstance(question, dict):
                    continue
                selected = [str(option.get("id")) for option in question.get("options") or [] if isinstance(option, dict) and said in (str(option.get("id") or ""), str(option.get("label") or ""))]
                answers.append({"questionId": str(question.get("id") or ""), "selectedOptionIds": selected})
            outcome = {"outcome": "answered", "answers": answers} if answers and all(row["selectedOptionIds"] for row in answers) else {"outcome": "skipped", "reason": "no matching option was selected"}
        await self.write_acp({"jsonrpc": "2.0", "id": request["id"], "result": {"outcome": outcome}})
        await self.emit("request_resolved", {}, ref)
        return True

    async def control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.clients.add(writer)
        hello = {"type": "hello", "session_id": self.session_id, "transcript": self.transcript, "busy": self.busy, "permissions": list(self.permissions)}
        writer.write((json.dumps(hello) + "\n").encode())
        await writer.drain()
        try:
            while line := await reader.readline():
                try:
                    request = json.loads(line)
                    ident = request.get("id")
                    method = request.get("method")
                    if method == "send":
                        if self.busy:
                            result: Any = {"ok": False, "error": "Cursor is busy"}
                        else:
                            self.busy = True
                            asyncio.create_task(self.prompt_turn(str(request.get("text") or ""), str(request.get("message_id") or "")))
                            result = {"ok": True}
                    elif method == "answer":
                        result = {"ok": await self.answer_request(str(request.get("ref") or ""), str(request.get("choice") or "deny"), str(request.get("said") or ""))}
                    elif method == "interrupt":
                        await self.write_acp({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": self.session_id}})
                        result = {"ok": True}
                    elif method == "stop":
                        self.stopping = True
                        result = {"ok": True}
                        asyncio.get_running_loop().call_later(0.2, lambda: self.process and self.process.terminate())
                    else:
                        result = {"ok": False, "error": "unknown method"}
                    writer.write((json.dumps({"type": "response", "id": ident, "result": result}) + "\n").encode())
                    await writer.drain()
                except (ValueError, TypeError, KeyError) as exc:
                    writer.write((json.dumps({"type": "response", "id": None, "result": {"ok": False, "error": str(exc)}}) + "\n").encode())
                    await writer.drain()
        except (ConnectionError, OSError):
            pass  # a viewer or a restarted host can disconnect at any point
        finally:
            self.clients.discard(writer)
            writer.close()

    async def terminal_input(self) -> None:
        reader = asyncio.StreamReader()
        await asyncio.get_running_loop().connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while not self.stopping:
            line = await reader.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("/allow ") or text.startswith("/deny "):
                word, ref = text.split(" ", 1)
                await self.answer_request(ref, "allow_once" if word == "/allow" else "deny", "")
            elif text.startswith("/answer "):
                _, _, rest = text.partition(" ")
                ref, _, said = rest.partition(" ")
                await self.answer_request(ref, "answer", said)
            elif text == "/exit":
                self.stopping = True
                if self.process:
                    self.process.terminate()
                return
            elif text and not self.busy:
                self.busy = True
                asyncio.create_task(self.prompt_turn(text, ""))
            elif text:
                print("Cursor is working; send after this turn finishes.", flush=True)

    async def run(self) -> None:
        argv = ["cursor-agent"]
        if self.config.get("model"):
            argv += ["--model", self.config["model"]]
        if self.config.get("sandbox"):
            argv += ["--sandbox", self.config["sandbox"]]
        argv += ["acp"]
        env = {**os.environ, "CURSOR_CONFIG_DIR": str(Path(self.config["launch_dir"]) / "cursor-config")}
        self.process = await asyncio.create_subprocess_exec(*argv, cwd=self.config["cwd"], env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        reader_task = asyncio.create_task(self.read_acp())
        await self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}, "clientInfo": {"name": "daedalus", "version": "1"}})
        await self.call("authenticate", {"methodId": "cursor_login"})
        servers = self.config.get("mcp_servers") or []
        if self.config.get("resume"):
            result = await self.call("session/load", {"sessionId": self.config["resume"], "cwd": self.config["cwd"], "mcpServers": servers})
            self.session_id = self.config["resume"]
        else:
            result = await self.call("session/new", {"cwd": self.config["cwd"], "mcpServers": servers})
            self.session_id = str(result.get("sessionId") or "")
        if not self.session_id:
            raise RuntimeError("Cursor ACP did not return a session ID")
        mode = self.config.get("mode") or "ask"
        if mode != "agent":
            await self.call("session/set_mode", {"sessionId": self.session_id, "modeId": mode})
        Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)
        server = await asyncio.start_unix_server(self.control, path=self.socket_path)
        os.chmod(self.socket_path, 0o600)
        print(f"Cursor Agent · {mode}\ncursor> ", end="", flush=True)
        input_task = asyncio.create_task(self.terminal_input())
        try:
            async with server:
                await self.process.wait()
        finally:
            self.stopping = True
            input_task.cancel()
            reader_task.cancel()
            await self.emit("session_ended", {"reason": "Cursor ACP exited"})


async def main() -> None:
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    config["launch_dir"] = str(Path(sys.argv[1]).parent)
    bridge = Bridge(config, sys.argv[2])
    try:
        await bridge.run()
    except Exception as exc:
        print(f"Cursor Agent could not start: {exc}", flush=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
