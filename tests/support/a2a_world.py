"""Two whole installations and the strangers at their doors, in one process, for the agent links end to end.

Each installation has its own state folder (so its own key), its own database, its own real door (the guest
daemon's store, mailbox, control and public applications), its own session manager with a scripted model, and the
extensions wired as the application wires them: the bridge to its door, conversations at other doors, contacts,
the flows, real notifications. The operator's screens are the real API routes, reached over ASGI. The bridge and
the conversations follow their doors with their own loops, as they do in the running host, rather than being
pumped by the test: what is proved is what happens when nothing helps.

Between the installations is a router by host name, which is what the internet is here. A plain HTTP guest (an agent
such as Claude Code, following the page it was handed) and an A2A client reach the same router. Nothing leaves the
process: every address is ``*.example.test``, answered in memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from guestd.control import ControlDoor
from guestd.mailbox import Mailbox
from guestd.public import PublicDoor
from guestd.standin import ModelReply, StandIn
from guestd.store import Store

from daedalus.config import Settings
from daedalus.extensions import (
    agent_contacts,
    agent_talk,
    api_agent_talk,
    api_contacts,
    api_guest,
    api_link_export,
    api_link_flows,
    guest_door,
    link_flows,
)
from daedalus.extensions.notifications import NotificationService
from daedalus.stores.database import Database
from tests.unit.test_guest_turns import manager_with
from tests.unit.test_session_runner import ScriptedProvider

Step = dict[str, Any] | Callable[[Any], dict[str, Any]]


class Brain(ScriptedProvider):
    """A scripted model whose steps may also be decided from the request, and which can be told more as a test goes.

    A step that is a callable is handed the request and returns the step: what a test needs when the model's next move
    depends on a tool's result it has not seen yet, such as an id."""

    def __init__(self) -> None:
        super().__init__([])

    def then(self, *steps: Step) -> Brain:
        self.script.extend(steps)
        return self

    async def stream_with_tools(self, request: Any) -> Any:
        if self.script and callable(self.script[0]):
            self.script[0] = self.script[0](request)
        async for delta in super().stream_with_tools(request):
            yield delta


class Doors:
    """Every door by its host name, as one transport."""

    def __init__(self) -> None:
        self.apps: dict[str, Any] = {}
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        host = dict(scope.get("headers") or []).get(b"host", b"").decode().split(":")[0]
        if scope["type"] == "http":
            self.calls.append((host, scope["method"], scope["path"]))
        app = self.apps.get(host)
        if app is None:
            await send({"type": "http.response.start", "status": 502, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await app(scope, receive, send)


class StandInModel:
    """A stand-in's model: says the next line it was given, or a fixed line."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, link: Any, request: dict[str, Any]) -> ModelReply:
        self.requests.append(request)
        return ModelReply(self.lines.pop(0) if self.lines else "The stand-in answers from its briefing.", 10, 10)


class App:
    """As much of the application as the extensions read: the same attribute names, nothing else."""

    def __init__(self, settings: Settings, db: Database, manager: Any) -> None:
        self.settings = settings
        self.db = db
        self.manager = manager
        self.config = manager.config
        self.front = None
        self.extensions: dict[str, Any] = {}
        self.notifications: NotificationService | None = None
        self.stopping = asyncio.Event()

    @property
    def bus(self) -> Any:
        return self.manager.bus


class Installation:
    """One Daedalus: its door, its agent, the operator's API, and the loops that follow both doors."""

    def __init__(self, name: str, settings: Settings, db: Database, manager: Any, brain: Brain, doors: Doors) -> None:
        self.name = name
        self.host = f"{name}.example.test"
        self.settings = settings
        self.db = db
        self.manager = manager
        self.brain = brain
        self.doors = doors
        self.store = Store(settings.state_dir / "guest.sqlite")
        self.mailbox = Mailbox(self.store, public_base=lambda: f"https://{self.host}")
        self.control = ControlDoor(self.store, self.mailbox, "test")
        self.public = PublicDoor(self.mailbox)
        self.stand_in_model = StandInModel()
        self.stand_in = StandIn(self.store, self.mailbox, self.stand_in_model)
        doors.apps[self.host] = self.public
        self.app = App(settings, db, manager)
        self.tasks: list[asyncio.Task[Any]] = []
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(FastAPI()), base_url="http://app.test")
        self.chat = ""

    async def start(self) -> None:
        """Wire it as :mod:`daedalus.extensions` does, then start following the doors."""
        app = self.app
        service = NotificationService(self.db, self.manager.bus)
        app.notifications = service
        app.extensions["notifications"] = service
        self.manager.add_sink(service.on_event)
        for module in (guest_door, agent_talk, agent_contacts, link_flows):
            started = await module.install(app)  # type: ignore[arg-type]
            for task in started:
                task.cancel()
            await asyncio.gather(*started, return_exceptions=True)
        bridge = app.extensions[guest_door.EXTENSION_KEY]
        await bridge.client.aclose()
        bridge.client = guest_door.DoorClient(None, transport=httpx.ASGITransport(self.control), token="test")
        talks = app.extensions[agent_talk.EXTENSION_KEY]
        talks.dialer = agent_talk.Dialer(private=True, transport=httpx.ASGITransport(self.doors))
        api = FastAPI()

        async def owner() -> dict[str, Any]:
            return {"id": 1}

        for module in (api_guest, api_agent_talk, api_contacts, api_link_export, api_link_flows):
            module.register(api, app, owner)  # type: ignore[arg-type]
        await self.api.aclose()
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(api), base_url="http://app.test")
        self.tasks += [
            asyncio.create_task(bridge.serve(), name=f"{self.name}-bridge"),
            asyncio.create_task(talks.serve(), name=f"{self.name}-talks"),
            asyncio.create_task(app.extensions[link_flows.EXTENSION_KEY].serve(), name=f"{self.name}-flows"),
            asyncio.create_task(self.stand_in.serve(), name=f"{self.name}-stand-in"),
        ]
        self.chat = (await self.manager.create_session("chat", own_directory=True)).session.id

    @property
    def bridge(self) -> guest_door.GuestBridge:
        return self.app.extensions[guest_door.EXTENSION_KEY]

    @property
    def talks(self) -> agent_talk.AgentTalks:
        return self.app.extensions[agent_talk.EXTENSION_KEY]

    @property
    def contacts(self) -> agent_contacts.Contacts:
        return self.app.extensions[agent_contacts.EXTENSION_KEY]

    @property
    def flows(self) -> link_flows.LinkFlows:
        return self.app.extensions[link_flows.EXTENSION_KEY]

    @property
    def notifications(self) -> NotificationService:
        assert self.app.notifications is not None
        return self.app.notifications

    def state(self, session_id: str = "") -> Any:
        return self.manager.live_state(session_id or self.chat)

    @property
    def workspace(self) -> Path:
        return Path(self.state().workspace)

    async def call(self, method: str, path: str, **options: Any) -> Any:
        answer = await self.api.request(method, path, **options)
        assert answer.status_code < 300, f"{method} {path}: {answer.status_code} {answer.text}"
        return answer.json()

    async def make_link(self, *, mode: str = "direct", tools: str = "read", session_id: str = "", **fields: Any) -> tuple[str, str]:
        """A link made as the operator makes one in the app: the link's id and the address to hand over."""
        body = {
            "label": fields.pop("label", "a colleague"), "topic": fields.pop("topic", "Questions about this chat's work."), "mode": mode, "tools": tools, "approval": False,
            "usd_limit": 0, "turns_per_day": 0, "turns_total": 0, **fields,
        }
        if mode == "stand_in":
            options = (await self.call("GET", "/api/guest/door", params={"session_id": session_id or self.chat}))["models"]
            body.update(model_id=options[0]["id"] if options else "", briefing=body.get("briefing") or "Harbor listens on port 7312.", briefing_approved=True)
        made = await self.call("POST", f"/api/sessions/{session_id or self.chat}/guest/links", json=body)
        return made["link"]["id"], made["url"]

    async def say(self, text: str, session_id: str = "") -> None:
        """The operator writes in a chat, and the run it starts is waited for."""
        session = session_id or self.chat
        await self.manager.submit(session, text)
        await self.settled(session)

    async def submit(self, text: str, session_id: str = "") -> None:
        """The operator writes, and the test goes on while the run works: for a run that waits on the operator."""
        await self.manager.submit(session_id or self.chat, text)

    async def settled(self, session_id: str = "", seconds: float = 20.0) -> None:
        state = self.state(session_id)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not state.running and not self.manager._settling(state):
                await asyncio.sleep(0.02)
                if not state.running and (state.housekeeping is None or state.housekeeping.done()):
                    return
            await asyncio.sleep(0.02)
        raise AssertionError(f"{self.name}: the run did not settle")

    async def guest_lines_idle(self, seconds: float = 20.0) -> None:
        """Every guest message this side has taken has been answered."""
        await until(lambda: all(line.worker is None or line.worker.done() for line in self.bridge.lines.values()) and not any(line.waiting for line in self.bridge.lines.values()), seconds)

    async def close(self) -> None:
        workers = [line.worker for line in self.bridge.lines.values() if line.worker is not None]
        for task in [*self.tasks, *workers]:
            task.cancel()
        await asyncio.gather(*self.tasks, *workers, return_exceptions=True)
        talks = self.talks
        for task in list(talks._background):
            task.cancel()
        await asyncio.gather(*talks._background, return_exceptions=True)
        await self.bridge.client.aclose()
        await self.api.aclose()
        await self.manager.close()
        self.store.close()


async def until(check: Callable[[], Any], seconds: float = 10.0, what: str = "it") -> Any:
    deadline = time.monotonic() + seconds
    while True:
        found = check()
        if asyncio.iscoroutine(found) or isinstance(found, Awaitable):
            found = await found
        if found:
            return found
        if time.monotonic() >= deadline:
            raise AssertionError(f"{what} did not happen in time")
        await asyncio.sleep(0.02)


async def world(settings: Settings, tmp_path: Path, names: tuple[str, ...] = ("a", "b")) -> tuple[list[Installation], list[Database], Doors]:
    doors = Doors()
    sides: list[Installation] = []
    databases: list[Database] = []
    for name in names:
        own = settings.model_copy(update={"state_dir": tmp_path / name / "state", "workspaces_dir": tmp_path / name / "workspaces", "guest_public_url": f"https://{name}.example.test"})
        own.state_dir.mkdir(parents=True)
        db = Database(own.db_path, workspaces_dir=own.workspaces_dir)
        await db.open()
        databases.append(db)
        brain = Brain()
        manager = await manager_with(own, db, brain)
        # The auto-review is a model call of its own, on a model this world does not have; a test that is about the
        # review gives it one.
        manager.config.action_rules.review = []
        side = Installation(name, own, db, manager, brain, doors)
        await side.start()
        sides.append(side)
    return sides, databases, doors


class HttpGuest:
    """An agent with a shell and curl, doing what the link's page and the door's answers say, and nothing else.

    Each step reads the address it needs from what it was told: the page for the first ones, the redeem's answer and
    each read's ``detail`` after that. A step the page does not describe is a step this guest cannot take."""

    def __init__(self, doors: Doors, url: str) -> None:
        self.url = url.rstrip("/")
        self.base = self.url.split("/a/")[0]
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(doors), base_url=self.base)
        self.page = ""
        self.token = ""
        self.cursor = 0
        self.seen: list[dict[str, Any]] = []
        self.last: dict[str, Any] = {}

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def read_page(self) -> str:
        answer = await self.client.get(self.url)
        self.page = answer.text
        return self.page

    def described(self, pattern: str) -> bool:
        return re.search(pattern, self.page) is not None

    async def redeem(self) -> dict[str, Any]:
        answer = await self.client.post(f"{self.url}/redeem")
        body = answer.json()
        self.token = body.get("token", "")
        return body

    async def send(self, text: str = "", **body: Any) -> dict[str, Any]:
        answer = await self.client.post("/a/messages", json={"text": text, **body} if text else body, headers=self.headers)
        self.last = answer.json()
        return self.last

    async def read(self, wait: int = 0) -> dict[str, Any]:
        answer = await self.client.get(f"/a/messages?after={self.cursor}&wait={wait}", headers=self.headers)
        body = answer.json()
        self.last = body
        for message in body.get("messages") or []:
            self.seen.append(message)
        self.cursor = int(body.get("cursor") or self.cursor)
        return body

    async def read_until(self, check: Callable[[list[dict[str, Any]]], bool], seconds: float = 20.0) -> list[dict[str, Any]]:
        """Read as the page says to, until ``check`` holds over everything read, and fail if the door said to stop first."""
        deadline = time.monotonic() + seconds
        while not check(self.seen):
            body = await self.read(wait=1)
            if check(self.seen):
                break
            if body.get("stop_polling") and not body.get("messages"):
                raise AssertionError(f"the door said to stop before it happened: {body}")
            if time.monotonic() > deadline:
                raise AssertionError(f"it did not come: {self.seen}")
        return self.seen

    async def upload(self, name: str, data: bytes, *, piece: int = 512 * 1024, stop_after: int | None = None) -> str:
        made = (await self.client.post("/a/files", json={"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}, headers=self.headers)).json()
        file_id = made["id"]
        await self.resume_upload(file_id, data, piece=piece, stop_after=stop_after)
        return file_id

    async def resume_upload(self, file_id: str, data: bytes, *, piece: int = 512 * 1024, stop_after: int | None = None) -> dict[str, Any]:
        offset = int((await self.client.get(f"/a/files/{file_id}", headers=self.headers)).json().get("received") or 0)
        sent = 0
        answer: dict[str, Any] = {}
        while offset < len(data):
            if stop_after is not None and sent >= stop_after:
                return answer
            answer = (await self.client.put(f"/a/files/{file_id}?offset={offset}", content=data[offset:offset + piece], headers=self.headers)).json()
            offset = int(answer.get("received") or offset + piece)
            sent += 1
        return answer

    async def download(self, file: dict[str, Any], *, stop_after: int | None = None, start: int = 0, into: bytearray | None = None) -> bytes:
        got = into if into is not None else bytearray()
        offset = start
        pieces = 0
        while offset < int(file["size"]):
            if stop_after is not None and pieces >= stop_after:
                break
            answer = await self.client.get(f"/a/files/{file['id']}/content?offset={offset}", headers=self.headers)
            assert answer.status_code == 200, answer.text
            got.extend(answer.content)
            offset = int(answer.headers["X-Next-Offset"])
            pieces += 1
        return bytes(got)

    async def end(self) -> dict[str, Any]:
        return (await self.client.post("/a/end", headers=self.headers)).json()

    async def close(self) -> None:
        await self.client.aclose()


class A2AClient:
    """A client of the A2A protocol over JSON-RPC: what an agent on another framework does, after one redeem."""

    def __init__(self, doors: Doors, url: str) -> None:
        self.url = url.rstrip("/")
        self.base = self.url.split("/a/")[0]
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(doors), base_url=self.base)
        self.token = ""
        self.ids = 0

    async def card(self) -> dict[str, Any]:
        return (await self.client.get(f"{self.url}/.well-known/agent-card.json", headers={"Authorization": f"Bearer {self.token}"} if self.token else {})).json()

    async def redeem(self) -> None:
        self.token = (await self.client.post(f"{self.url}/redeem")).json()["token"]

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.ids += 1
        answer = await self.client.post("/a/rpc", json={"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params}, headers={"Authorization": f"Bearer {self.token}", "A2A-Version": "1.0"})
        return answer.json()

    async def close(self) -> None:
        await self.client.aclose()


def tool_results(request: Any) -> str:
    from protocore.contracts.types import ToolResultBlock

    return "\n".join(str(block.content) for message in request.messages for block in message.content_blocks if isinstance(block, ToolResultBlock))


def last_user_text(request: Any) -> str:
    from protocore.contracts.types import MessageRole, TextBlock

    for message in reversed(request.messages):
        if message.role is MessageRole.user:
            return "".join(block.text for block in message.content_blocks if isinstance(block, TextBlock))
    return ""


def dumped(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = ["A2AClient", "Brain", "Doors", "HttpGuest", "Installation", "StandInModel", "dumped", "last_user_text", "tool_results", "until", "world"]
