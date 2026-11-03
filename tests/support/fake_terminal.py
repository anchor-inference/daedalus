"""A terminal client's executor for the tests: the relay's other end, over a real loopback WebSocket.

It speaks the relay as ``docs/architecture/cli.md`` describes it: ``device.hello`` first, then it
answers the server's ``exec.run`` and ``fs.*`` requests against a folder of the test, streaming a
command's output as ``exec.output``, and keeps its own journal of what it ran so a request the server
asks about after a reconnect (``device.result``) is answered rather than run again. Knobs let a test
drop the link in the middle of a request, refuse requests the way a guard does, or keep the client
away altogether.

Also here: :func:`relay_server`, the real relay route of the host served by uvicorn on a free
loopback port, so the client connects to exactly what a terminal connects to.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection, connect

from daedalus.extensions import api_remote_relay


class FakeTerminal:
    def __init__(self, url: str, token: str, root: Path, *, fs: tuple[str, ...] = ("stat", "list", "read", "write", "mkdir"), guard: str = "auto", client: str = "",
                 jobs: bool = True) -> None:
        self.url = url
        self.client = client
        """The process this client says it is; one link per process of a device."""
        self.token = token
        self.root = root
        self.fs = fs
        self.guard = guard
        self.refusal_message = "not now"
        self.sessions: list[str] = []
        self.drop_on: set[str] = set()
        """Methods whose next request drops the link right after the work started; the work goes on."""
        self.requests: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.executed: list[str] = []
        """The ids of the commands this client really ran."""
        self.output_sent: list[str] = []
        self.results: dict[str, dict[str, Any]] = {}
        self.unsent: set[str] = set()
        self.running: dict[str, asyncio.Task[None]] = {}
        self.welcome: dict[str, Any] | None = None
        self._ws: ClientConnection | None = None
        self.close_code: int | None = None
        self._reader: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()
        self.jobs_capable = jobs
        """Whether it keeps commands past their calls (``capabilities.jobs``), as daedalus-cli does."""
        self.jobs: dict[str, dict[str, Any]] = {}
        """The jobs it keeps, by the server's id: their process, log and end."""

    # -- the link ---------------------------------------------------------------------------------

    def known(self) -> list[dict[str, str]]:
        return [{"id": rid, "state": "running"} for rid in self.running] + [{"id": rid, "state": "done"} for rid in sorted(self.unsent)]

    async def connect(self, *, origin: str | None = None, hello: dict[str, Any] | None = None) -> None:
        headers = {"Authorization": f"Device {self.token}"}
        if origin:
            headers["Origin"] = origin
        self.closed = asyncio.Event()
        self._ws = await connect(self.url, additional_headers=headers, open_timeout=5)
        params = hello if hello is not None else {
            "protocol": 1, "client": self.client, "client_version": "test", "os": "linux", "arch": "amd64", "shell": "/bin/bash", "home": str(self.root),
            "capabilities": {"exec": True, "fs": list(self.fs), "pty": False, **({"jobs": True} if self.jobs_capable else {})},
            "sessions": [{"session_id": sid, "guard": self.guard} for sid in self.sessions],
            "known": self.known(),
        }
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "method": "device.hello", "params": params}))
        first = json.loads(await asyncio.wait_for(self._ws.recv(), 5))
        assert first.get("method") == "device.welcome", first
        self.welcome = first["params"]
        self._reader = asyncio.create_task(self._read(self._ws))

    async def disconnect(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()
        if self._reader is not None:
            with contextlib.suppress(Exception):
                await self._reader

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    async def _read(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                message = json.loads(raw)
                if "id" in message and "method" in message:
                    asyncio.create_task(self._handle(ws, message))
                elif "method" in message:
                    self.notifications.append(message)
        except Exception:  # noqa: BLE001 — a closed socket ends the reader
            pass
        finally:
            self.close_code = ws.close_code
            self.closed.set()

    async def _send(self, ws: ClientConnection | None, message: dict[str, Any]) -> bool:
        target = self._ws if self._ws is not None else ws
        if target is None:
            return False
        try:
            await target.send(json.dumps(message))
        except Exception:  # noqa: BLE001
            return False
        return True

    # -- the requests -----------------------------------------------------------------------------

    async def _handle(self, ws: ClientConnection, message: dict[str, Any]) -> None:
        rid, method, params = message["id"], message["method"], dict(message.get("params") or {})
        self.requests.append(message)
        if method == "device.result":
            stored = self.results.get(str(params.get("id")))
            self.unsent.discard(str(params.get("id")))
            answer = stored if stored is not None else {"error": {"code": 1001, "message": "no such request"}}
            await self._send(ws, {"jsonrpc": "2.0", "id": rid, **answer})
            return
        intent = str((params.get("_meta") or {}).get("intent") or "")
        if intent in ("write", "exec") and self.guard in ("plan", "ask"):
            reason = "plan_guard" if self.guard == "plan" else "refused_by_operator"
            data = {"reason": reason, **({"message": self.refusal_message} if reason == "refused_by_operator" else {})}
            await self._send(ws, {"jsonrpc": "2.0", "id": rid, "error": {"code": 1004, "message": "refused", "data": data}})
            return
        drop = method in self.drop_on
        self.drop_on.discard(method)
        work = asyncio.create_task(self._work(rid, method, params))
        self.running[rid] = work
        if drop:
            await asyncio.sleep(0.05)
            await self.disconnect()
        try:
            answer = await work
        finally:
            self.running.pop(rid, None)
        self.results[rid] = answer
        if not await self._send(None, {"jsonrpc": "2.0", "id": rid, **answer}):
            self.unsent.add(rid)

    async def _work(self, rid: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"result": await getattr(self, "_" + method.replace(".", "_"))(rid, params)}
        except FileNotFoundError as exc:
            return {"error": {"code": 1001, "message": str(exc)}}
        except AttributeError:
            return {"error": {"code": -32601, "message": f"no method {method}"}}

    def _inside(self, path: str) -> Path:
        real = Path(os.path.realpath(path))
        if real != self.root and self.root not in real.parents:
            raise PermissionError(path)
        return real

    async def _exec_run(self, rid: str, params: dict[str, Any]) -> dict[str, Any]:
        self.executed.append(rid)
        job = params.get("job") if self.jobs_capable else None
        process = await asyncio.create_subprocess_exec(*params["argv"], cwd=params.get("cwd") or str(self.root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                       start_new_session=True)
        assert process.stdout is not None
        chunks: list[bytes] = []
        stream = process.stdout

        async def pump() -> None:
            while chunk := await stream.read(4096):
                chunks.append(chunk)
                text = chunk.decode("utf-8", "replace")
                self.output_sent.append(text)
                await self._send(None, {"jsonrpc": "2.0", "method": "exec.output", "params": {"id": rid, "data": text}})

        reading = asyncio.create_task(pump())
        after = (int(job.get("after_ms") or 0) / 1000) if isinstance(job, dict) else None
        if after is not None:
            done, _ = await asyncio.wait({reading}, timeout=after) if after > 0 else (set(), set())
            if not done:
                return await self._keep(job, process, reading, chunks)
        await reading
        code = await process.wait()
        return {"exit_code": code, "signal": "", "stdout": b"".join(chunks).decode("utf-8", "replace"), "stderr": "", "truncated": False, "timed_out": False, "duration_ms": 1}

    async def _keep(self, job: dict[str, Any], process: asyncio.subprocess.Process, reading: asyncio.Task[None], chunks: list[bytes]) -> dict[str, Any]:
        """The command goes on as a job: the reader stops streaming and writes into the job's log."""
        reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)
        log = self._inside(str(job["log"]))
        log.parent.mkdir(parents=True, exist_ok=True)
        so_far = b"".join(chunks)
        log.write_bytes(so_far)
        record: dict[str, Any] = {"process": process, "log": log, "exit_code": None, "ended_at": None}
        assert process.stdout is not None
        stream = process.stdout

        async def keep() -> None:
            with log.open("ab") as handle:
                while chunk := await stream.read(4096):
                    handle.write(chunk)
                    handle.flush()
            record["exit_code"] = await process.wait()
            record["ended_at"] = time.time()

        record["task"] = asyncio.create_task(keep())
        self.jobs[str(job["id"])] = record
        return {"exit_code": -1, "signal": "", "stdout": so_far.decode("utf-8", "replace"), "stderr": "", "truncated": False, "timed_out": False,
                "duration_ms": 1, "handed_over": True, "pid": process.pid}

    def _job(self, params: dict[str, Any]) -> dict[str, Any]:
        record = self.jobs.get(str(params.get("job")))
        if record is None:
            raise FileNotFoundError(f"this terminal holds no job {params.get('job')}")
        return record

    async def _job_status(self, rid: str, params: dict[str, Any]) -> dict[str, Any]:
        record = self._job(params)
        size = record["log"].stat().st_size if record["log"].exists() else -1
        if record["exit_code"] is None:
            return {"state": "running", "size": size, "pid": record["process"].pid}
        return {"state": "exited", "exit_code": record["exit_code"], "signal": "", "ended_at": record["ended_at"], "size": size}

    async def _job_tail(self, rid: str, params: dict[str, Any]) -> dict[str, Any]:
        record = self._job(params)
        lines = record["log"].read_text(errors="replace").splitlines()[-int(params.get("lines") or 100):]
        return {"text": "\n".join(lines)}

    async def _job_kill(self, rid: str, params: dict[str, Any]) -> dict[str, Any]:
        record = self._job(params)
        if record["exit_code"] is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(record["process"].pid, signal.SIGTERM)
            await record["task"]
        return await self._job_status(rid, params)

    async def stop_jobs(self) -> None:
        """End what it still keeps, as a client that exits takes its jobs with it."""
        for record in self.jobs.values():
            if record["exit_code"] is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(record["process"].pid, signal.SIGKILL)
                await asyncio.gather(record["task"], return_exceptions=True)

    async def _fs_stat(self, rid: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self._inside(params["path"])
        if not path.exists():
            return {"exists": False}
        return {"exists": True, "type": "dir" if path.is_dir() else "file", "size": path.stat().st_size, "mode": "0644"}

    async def _fs_read(self, rid: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self._inside(params["path"])
        data = path.read_bytes()
        offset, most = int(params.get("offset") or 0), int(params.get("max") or (1 << 20))
        piece = data[offset : offset + most]
        return {"data_b64": base64.b64encode(piece).decode(), "offset": offset, "size": len(data), "eof": offset + len(piece) >= len(data)}

    async def _fs_write(self, rid: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self._inside(params["path"])
        temporary = path.with_name(f".{path.name}.part")
        data = base64.b64decode(params["data_b64"])
        with open(temporary, "wb" if int(params["offset"]) == 0 else "ab") as handle:
            handle.write(data)
        created = not path.exists()
        if params.get("final"):
            temporary.replace(path)
        return {"size": (path if params.get("final") else temporary).stat().st_size, "created": created}

    async def _fs_list(self, rid: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self._inside(params["path"])
        return {"entries": [{"name": entry.name, "type": "dir" if entry.is_dir() else "file", "size": 0, "mtime": ""} for entry in sorted(path.iterdir())], "truncated": False}

    async def _fs_mkdir(self, rid: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self._inside(params["path"])
        created = not path.exists()
        path.mkdir(parents=True, exist_ok=True)
        return {"created": created}


class FakeDevices:
    """The pairing's store as the relay reads it: one token per device, and each device's scope."""

    def __init__(self) -> None:
        self.tokens: dict[str, SimpleNamespace] = {}
        self.scope: dict[str, set[str]] = {}

    def pair(self, device_id: str, token: str, name: str = "laptop") -> None:
        self.tokens[token] = SimpleNamespace(id=device_id, name=name)

    async def check(self, token: str, *, address: str = "") -> Any:
        return self.tokens.get(token)

    async def in_scope(self, device_id: str, session_id: str) -> bool:
        return session_id in self.scope.get(device_id, set())


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextlib.asynccontextmanager
async def relay_server(extensions: dict[str, Any]) -> AsyncIterator[str]:
    """The host's relay route on a free loopback port; yields the ``ws://`` address of ``/api/cli/relay``."""
    api = FastAPI()
    api_remote_relay.register(api, SimpleNamespace(extensions=extensions), lambda: {})  # type: ignore[arg-type]
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port, log_level="warning", lifespan="off", ws="websockets-sansio"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    try:
        yield f"ws://127.0.0.1:{port}/api/cli/relay"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


__all__ = ["FakeDevices", "FakeTerminal", "free_port", "relay_server"]
