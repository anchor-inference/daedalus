"""A custom page's own agent: the session bound to the page, the page's messages to it, and the data the
agent pushes back to the open page and keeps in the page's storage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.types import OPERATOR_WORDS_METADATA_KEY, Message, MessageRole, TextBlock

from daedalus.config import PagesConfig, RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host import prompts
from daedalus.host.events import EventBus
from daedalus.host.pages import PageError, PageStore, agent_brief, store_for
from daedalus.host.tool_groups import SESSION_LOADS_KEY
from daedalus.host.transcript_view import message_view
from daedalus.stores.database import Database
from daedalus.tools import pages as page_tools

H = {"X-Daedalus-Token": "tok"}


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, event_type: str, payload: dict[str, Any], **_: Any) -> None:
        self.events.append((event_type, payload))


class FakeSessions:
    """The parts of the session manager the page agent's routes use, recording what they were asked."""

    def __init__(self) -> None:
        self.bus = FakeBus()
        self.titles: dict[str, str] = {"s1": "Mesh work", "s2": "Another"}
        self.running: set[str] = set()
        self.created: list[dict[str, Any]] = []
        self.submitted: list[dict[str, Any]] = []
        self.presets: list[tuple[str, str]] = []

    async def get_state(self, session_id: str) -> Any:
        if session_id not in self.titles:
            return None
        return SimpleNamespace(session=SimpleNamespace(id=session_id, title=self.titles[session_id]), running=session_id in self.running)

    async def create_session(self, title: str, *, metadata: dict[str, Any] | None = None, project_id: str | None = None) -> Any:
        if project_id == "nowhere":
            raise KeyError(project_id)
        session_id = f"new{len(self.created) + 1}"
        self.titles[session_id] = title
        self.created.append({"id": session_id, "title": title, "metadata": metadata, "project_id": project_id})
        return SimpleNamespace(session=SimpleNamespace(id=session_id, title=title), running=False)

    async def set_model(self, session_id: str, *, preset: str) -> None:
        if preset == "missing":
            raise ValueError("no preset 'missing'")
        self.presets.append((session_id, preset))

    async def submit(self, session_id: str, text: str, attachments: Any = (), **options: Any) -> str:
        if session_id not in self.titles:
            raise KeyError(session_id)
        self.submitted.append({"session_id": session_id, "text": text, "files": [(a.name, a.path.read_bytes()) for a in attachments], **options})
        return f"run{len(self.submitted)}"


@pytest.fixture
async def site(settings: Settings, db: Database, tmp_path: Path) -> Any:
    settings.bot_repo_dir = tmp_path / "repo"
    sessions = FakeSessions()
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=sessions, front=None, extensions={}, guard=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        assert (await client.post("/api/pages", json={"slug": "cad", "title": {"en": "CAD", "ru": "САПР"}, "icon": "grid"}, headers=H)).status_code == 200
        yield client, sessions


# -- binding -----------------------------------------------------------------------------------


async def test_a_page_is_bound_changed_and_unbound_over_the_api(site: Any) -> None:
    client, sessions = site
    page = (await client.get("/api/pages/cad", headers=H)).json()
    assert page["session_id"] is None and page["agent"] is None
    bound = await client.patch("/api/pages/cad", json={"session_id": "s1"}, headers=H)
    assert bound.status_code == 200
    assert bound.json()["agent"] == {"id": "s1", "title": "Mesh work", "gone": False, "running": False}
    assert (await client.patch("/api/pages/cad", json={"session_id": "nobody"}, headers=H)).status_code == 404
    assert (await client.get("/api/pages/cad", headers=H)).json()["session_id"] == "s1", "a refused binding leaves the old one"
    # A session deleted after it was bound is named as gone, so the screen can offer another.
    del sessions.titles["s1"]
    assert (await client.get("/api/pages/cad", headers=H)).json()["agent"] == {"id": "s1", "title": "", "gone": True, "running": False}
    unbound = (await client.patch("/api/pages/cad", json={"session_id": None}, headers=H)).json()
    assert unbound["session_id"] is None and unbound["agent"] is None
    assert ("pages.changed", {"slug": "cad", "change": "updated", "version": None}) in sessions.bus.events


async def test_a_new_page_agent_carries_the_page_in_its_brief_and_is_bound(site: Any) -> None:
    client, sessions = site
    made = await client.post("/api/pages/cad/agent", json={"title": "CAD agent", "preset": "strong"}, headers=H)
    assert made.status_code == 200, made.text
    answer = made.json()
    assert answer["session_id"] == "new1" and answer["page"]["agent"]["title"] == "CAD agent"
    created = sessions.created[0]
    meta = created["metadata"]
    assert meta["page"] == "cad" and meta["telegram_detached"] is True and meta[SESSION_LOADS_KEY] == {"pages": "eager"}
    assert "/app/p/cad" in meta["brief"] and "PageEmit" in meta["brief"] and "PageStorage" in meta["brief"] and "daedalus-pages" in meta["brief"]
    assert sessions.presets == [("new1", "strong")]
    # No title: the page's own.
    again = (await client.post("/api/pages/cad/agent", headers=H)).json()
    assert again["title"] == "CAD" and (await client.get("/api/pages/cad", headers=H)).json()["session_id"] == again["session_id"]
    assert (await client.post("/api/pages/cad/agent", json={"project_id": "nowhere"}, headers=H)).status_code == 404
    assert (await client.post("/api/pages/nope/agent", headers=H)).status_code == 404


def test_the_brief_names_the_page_and_its_state() -> None:
    manifest = {"slug": "cad", "title": {"en": "CAD"}, "version": 3, "draft": {"kind": "static"}}
    brief = agent_brief(manifest)
    assert "«CAD»" in brief and "'cad'" in brief and "version 3 is live" in brief
    assert "nothing is published yet" in agent_brief({**manifest, "version": None})


# -- the page asks its agent -------------------------------------------------------------------


async def test_the_page_asks_its_agent_marked_as_the_page(site: Any) -> None:
    client, sessions = site
    assert (await client.post("/api/pages/cad/ask", json={"text": "Optimise the mesh"}, headers=H)).status_code == 409
    await client.patch("/api/pages/cad", json={"session_id": "s1"}, headers=H)
    asked = await client.post("/api/pages/cad/ask", json={"text": "Optimise the mesh"}, headers=H)
    assert asked.status_code == 200 and asked.json() == {"run_id": "run1", "session_id": "s1"}
    sent = sessions.submitted[0]
    assert sent["origin"] == "page:cad" and sent["via"] == "page"
    assert sent["text"].startswith("(Sent from the page «CAD» (/app/p/cad) at the operator's request.)\n\n")
    assert sent["text"].endswith("Optimise the mesh")
    assert sent["operator_words"] == ["Optimise the mesh"] and sent["as_answer"] is True and sent["follow_up"] is False
    # The page reporting on its own: not the operator's words, never an answer, never an interruption.
    await client.post("/api/pages/cad/ask", json={"text": "Selected: part 12", "silent": True}, headers=H)
    quiet = sessions.submitted[1]
    assert "the page reports this itself" in quiet["text"] and quiet["operator_words"] is None
    assert quiet["as_answer"] is False and quiet["follow_up"] is True
    assert (await client.post("/api/pages/cad/ask", json={"text": "  "}, headers=H)).status_code == 400
    assert (await client.post("/api/pages/cad/ask", json={"text": "x", "colour": "red"}, headers=H)).status_code == 422


async def test_the_page_sends_files_with_a_form(site: Any, settings: Settings) -> None:
    client, sessions = site
    await client.patch("/api/pages/cad", json={"session_id": "s1"}, headers=H)
    asked = await client.post("/api/pages/cad/ask", data={"text": "", "silent": "false"}, files=[("files", ("part.stl", b"solid x", "model/stl"))], headers=H)
    assert asked.status_code == 200, asked.text
    sent = sessions.submitted[0]
    assert sent["files"] == [("part.stl", b"solid x")] and sent["text"].endswith("File attached.")
    assert not any((settings.state_dir / "upload-staging").iterdir()), "the staged copy is gone once handed over"
    await client.post("/api/pages/cad/ask", data={"text": "Look", "silent": "1"}, headers=H)
    assert sessions.submitted[1]["follow_up"] is True


async def test_a_gone_agent_is_reported(site: Any) -> None:
    client, sessions = site
    await client.patch("/api/pages/cad", json={"session_id": "s1"}, headers=H)
    del sessions.titles["s1"]
    assert (await client.post("/api/pages/cad/ask", json={"text": "hi"}, headers=H)).status_code == 404


def test_the_page_note_is_for_the_model_only() -> None:
    note = prompts.page_note("cad", "CAD (v2)", silent=False)
    assert prompts.without_host_notes(note + "Make it red") == "Make it red"
    quoted = "Make it red\n\n" + note
    assert prompts.without_host_notes(quoted) == quoted.rstrip(), "a note anywhere but the start is the operator's text"
    message = Message(role=MessageRole.user, content_blocks=[TextBlock(text=prompts.page_note("cad", "CAD", silent=True) + "Selected: part 12")],
                      metadata={"daedalus.origin": "page:cad", OPERATOR_WORDS_METADATA_KEY: False}, created_at=datetime.now(UTC))
    view = message_view(message)
    assert view["origin"] == "page:cad" and view["silent"] is True and view["text"] == "Selected: part 12"
    spoken = message.model_copy(update={"metadata": {"daedalus.origin": "page:cad", OPERATOR_WORDS_METADATA_KEY: ["Make it red"]}})
    assert message_view(spoken)["silent"] is False
    assert message_view(message.model_copy(update={"metadata": {"daedalus.origin": "operator"}}))["silent"] is False


# -- the agent pushes to the open page ---------------------------------------------------------


async def test_emit_goes_on_the_bus_live_only(site: Any) -> None:
    client, sessions = site
    sent = await client.post("/api/pages/cad/emit", json={"event": "scene.updated", "data": {"file": "model.glb"}}, headers=H)
    assert sent.status_code == 200
    assert ("pages.emit", {"slug": "cad", "event": "scene.updated", "data": {"file": "model.glb"}}) in sessions.bus.events
    for bad in ("theme", "agent.message", "", "has space", "x" * 65):
        assert (await client.post("/api/pages/cad/emit", json={"event": bad, "data": 1}, headers=H)).status_code in (400, 422), bad
    assert (await client.post("/api/pages/nope/emit", json={"event": "x"}, headers=H)).status_code == 404


async def test_emit_through_the_real_bus_is_ephemeral_and_bounded(settings: Settings, db: Database) -> None:
    bus = EventBus(db)
    await bus.start()
    try:
        app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=SimpleNamespace(bus=bus), extensions={})
        store = store_for(app)
        await store.create("cad", title="CAD")
        head = bus.head
        await store.emit("cad", "scene.updated", {"file": "model.glb"})
        assert bus.head == head, "an emitted event is not kept in the ring"
        with pytest.raises(PageError) as big:
            await store.emit("cad", "scene.updated", {"blob": "x" * 70_000})
        assert big.value.status == 413 and "storage" in str(big.value)
    finally:
        await bus.close()


async def test_emit_refuses_what_json_cannot_carry(tmp_path: Path) -> None:
    heard: list[tuple[str, str, Any]] = []

    async def hear(slug: str, event: str, data: Any) -> None:
        heard.append((slug, event, data))

    store = PageStore(tmp_path / "pages", PagesConfig(), on_emit=hear)
    await store.create("cad", title="CAD")
    with pytest.raises(PageError):
        await store.emit("cad", "x", {1, 2})
    with pytest.raises(PageError):
        await store.emit("cad", "x", float("nan"))
    await store.emit("cad", "x", [1, "два"])
    assert heard == [("cad", "x", [1, "два"])]
    with pytest.raises(PageError) as nobody:
        await PageStore(tmp_path / "other", PagesConfig()).emit("cad", "x")
    assert nobody.value.status == 404


# -- the tools ---------------------------------------------------------------------------------


class Desk:
    def __init__(self, store: PageStore) -> None:
        self.store = store
        self.titles = {"s1": "This one", "s2": "Mesh work"}

    async def session_title(self, session_id: str) -> str | None:
        return self.titles.get(session_id)


@pytest.fixture
async def desk(tmp_path: Path, db: Database) -> Desk:
    heard: list[tuple[str, str, Any]] = []

    async def hear(slug: str, event: str, data: Any) -> None:
        heard.append((slug, event, data))

    store = PageStore(tmp_path / "pages", PagesConfig(), db=db, on_emit=hear)
    store.heard = heard  # type: ignore[attr-defined]
    await store.create("cad", title="CAD")
    return Desk(store)


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch, desk: Desk) -> Any:
    services = SimpleNamespace(extra={"manager": SimpleNamespace(service_hooks={"pages": desk})}, max_tool_output_chars=60_000)
    monkeypatch.setattr("daedalus.tools.pages.services_for", lambda _: services)
    monkeypatch.setattr("daedalus.tools._common.services_for", lambda _: services)
    return SimpleNamespace(session_id="s1", metadata={"tool_call_id": "c1"})


async def call(tool: Any, context: Any, **arguments: Any) -> Any:
    return await tool().invoke(context, arguments)


async def test_page_bind_binds_this_session_another_or_none(context: Any, desk: Desk) -> None:
    mine = await call(page_tools.page_bind, context, slug="cad")
    assert not mine.is_error and (await desk.store.require("cad"))["session_id"] == "s1"
    other = await call(page_tools.page_bind, context, slug="cad", session_id="s2")
    assert not other.is_error and "Mesh work" in other.content and "it was s1" in other.content
    assert (await desk.store.require("cad"))["session_id"] == "s2"
    missing = await call(page_tools.page_bind, context, slug="cad", session_id="ghost")
    assert missing.is_error and (await desk.store.require("cad"))["session_id"] == "s2"
    gone = await call(page_tools.page_bind, context, slug="cad", unbind=True)
    assert not gone.is_error and (await desk.store.require("cad"))["session_id"] is None
    assert "nothing changed" in (await call(page_tools.page_bind, context, slug="cad", unbind=True)).content
    assert (await call(page_tools.page_bind, context, slug="nope")).is_error


async def test_page_emit_reaches_the_store(context: Any, desk: Desk) -> None:
    sent = await call(page_tools.page_emit, context, slug="cad", event="scene.updated", data={"file": "model.glb"})
    assert not sent.is_error and "opened later" in sent.content
    assert desk.store.heard == [("cad", "scene.updated", {"file": "model.glb"})]  # type: ignore[attr-defined]
    refused = await call(page_tools.page_emit, context, slug="cad", event="theme", data=None)
    assert refused.is_error and "bridge" in refused.content


async def test_page_storage_shares_state_with_the_page(context: Any, desk: Desk) -> None:
    assert "keeps nothing" in (await call(page_tools.page_storage, context, slug="cad", op="list")).content
    stored = await call(page_tools.page_storage, context, slug="cad", op="set", key="scene/main", value={"parts": 3, "name": "корпус"})
    assert not stored.is_error and "PageEmit" in stored.content
    # What the agent wrote is what the page reads through its own storage route.
    assert (await desk.store.storage_get("cad", "scene/main"))["value"] == {"parts": 3, "name": "корпус"}
    got = await call(page_tools.page_storage, context, slug="cad", op="get", key="scene/main")
    assert '"parts": 3' in got.content and "корпус" in got.content
    listed = await call(page_tools.page_storage, context, slug="cad", op="list", key="scene/")
    assert "scene/main" in listed.content and listed.metadata["count"] == 1
    assert (await call(page_tools.page_storage, context, slug="cad", op="get", key="nothing")).metadata["found"] is False
    assert (await call(page_tools.page_storage, context, slug="cad", op="delete", key="scene/main")).metadata["deleted"] is True
    assert (await call(page_tools.page_storage, context, slug="cad", op="get")).is_error
    assert (await call(page_tools.page_storage, context, slug="cad", op="drop", key="x")).is_error
    assert (await call(page_tools.page_storage, context, slug="nope", op="get", key="x")).is_error
