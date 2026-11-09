"""Canvases: the JSON Canvas document and its Obsidian layout, item operations and the merge of two
writers, the store's scopes and history, the conversion of Excalidraw diagrams, the routes the board
calls, and the agent's tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.events import EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.stores import canvas_doc
from daedalus.stores.canvas_convert import convert
from daedalus.stores.canvas_doc import CanvasError
from daedalus.stores.canvas_layout import card_size, layout
from daedalus.stores.canvas_preview import preview_svg
from daedalus.stores.canvas_schema import SQL, convert_diagrams
from daedalus.stores.canvases import CanvasStore, NotFound, Stale, store_for
from daedalus.stores.database import Database
from daedalus.stores.notes import Scope
from daedalus.tools import canvas as canvas_tools
from tests.unit.test_projects import HEADERS, _client


def card(node_id: str, text: str = "", x: int = 0, y: int = 0, **extra: Any) -> dict[str, Any]:
    return {"id": node_id, "type": "text", "text": text, "x": x, "y": y, "width": 200, "height": 80, **extra}


def edge(edge_id: str, source: str, target: str, **extra: Any) -> dict[str, Any]:
    return {"id": edge_id, "fromNode": source, "toNode": target, **extra}


class Published:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def __call__(self, name: str, payload: dict[str, Any], **ids: Any) -> None:
        self.events.append((name, payload, ids))


async def _chat(db: Database, sid: str, *, project: str = "p", ephemeral: bool = False, system: str = "") -> None:
    if not await db.fetchone("SELECT 1 FROM projects WHERE id = ?", (project,)):
        await db.execute(
            "INSERT INTO projects(id, name, created_at, settings, system) VALUES (?, ?, 'now', ?, ?)",
            (project, project.upper(), json.dumps({"ephemeral": True} if ephemeral else {}), system),
        )
    await db.execute(
        "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id, kind) VALUES (?, 't', ?, 'now', 'now', '{}', ?, 'chat')",
        (sid, f"Chat {sid}", project),
    )


@pytest.fixture
def published() -> Published:
    return Published()


@pytest.fixture
def store(db: Database, published: Published) -> CanvasStore:
    return CanvasStore(db, publish=published)


# -- the document ----------------------------------------------------------------------------------


def test_obsidian_layout_one_plain_item_per_line_and_nested_ones_spread() -> None:
    doc = canvas_doc.check({"nodes": [card("a", "Привет", 0.0, -10), {**card("b"), "daedalus": {"shape": "diamond"}}], "edges": [edge("e", "a", "b", label="go")]})
    text = canvas_doc.write(doc)
    assert text == (
        '{\n\t"nodes":[\n'
        '\t\t{"id":"a","type":"text","text":"Привет","x":0,"y":-10,"width":200,"height":80},\n'
        '\t\t{\n\t\t\t"id":"b",\n\t\t\t"type":"text",\n\t\t\t"text":"",\n\t\t\t"x":0,\n\t\t\t"y":0,\n\t\t\t"width":200,\n\t\t\t"height":80,\n'
        '\t\t\t"daedalus":{"shape":"diamond"}\n\t\t}\n'
        '\t],\n\t"edges":[\n\t\t{"id":"e","fromNode":"a","toNode":"b","label":"go"}\n\t]\n}'
    )
    assert canvas_doc.parse(text) == doc
    assert canvas_doc.write(canvas_doc.empty()) == '{\n\t"nodes":[],\n\t"edges":[]\n}'


def test_unknown_keys_survive_and_a_new_file_from_obsidian_is_an_empty_canvas() -> None:
    doc = canvas_doc.parse('{"nodes":[{"id":"n","type":"link","url":"https://example.com","x":1,"y":2,"width":3,"height":4,"plugin":{"k":1}}],"edges":[],"meta":"kept"}')
    assert doc["nodes"][0]["plugin"] == {"k": 1} and doc["meta"] == "kept"
    assert canvas_doc.parse("") == {"nodes": [], "edges": []}


@pytest.mark.parametrize(("doc", "words"), [
    ({"nodes": [card("a"), card("a")]}, "used twice"),
    ({"nodes": [card("a")], "edges": [edge("e", "a", "ghost")]}, "ghost"),
    ({"nodes": [{**card("a"), "type": "shape"}]}, "type is one of"),
    ({"nodes": [{**card("a"), "color": "red"}]}, "colour"),
    ({"nodes": [{**card("a"), "width": 0}]}, "at least 1"),
    ({"nodes": [card("a")], "edges": [edge("e", "a", "a", fromSide="middle")]}, "fromSide"),
    ({"nodes": [{**card("a"), "x": float("nan")}]}, "number"),
    ([], "JSON object"),
])
def test_a_broken_document_is_refused_in_words(doc: Any, words: str) -> None:
    with pytest.raises(CanvasError, match=words):
        canvas_doc.check(doc)


def test_operations_put_delete_and_reorder() -> None:
    doc = canvas_doc.check({"nodes": [card("a"), card("b"), card("c")], "edges": [edge("ab", "a", "b"), edge("bc", "b", "c")]})
    changed, touched = canvas_doc.apply_ops(doc, [
        {"op": "delete", "id": "b"},
        {"op": "put", "kind": "node", "item": {"id": "g", "type": "group", "x": -50, "y": -50, "width": 600, "height": 400}, "at": "back"},
        {"op": "put", "kind": "node", "item": card("a", "renamed")},
    ])
    assert [node["id"] for node in changed["nodes"]] == ["g", "a", "c"]
    assert changed["edges"] == []
    assert touched == {"b", "ab", "bc", "g", "a"}
    with pytest.raises(CanvasError, match="no node or edge"):
        canvas_doc.apply_ops(doc, [{"op": "delete", "id": "zzz"}])
    with pytest.raises(CanvasError, match="already names"):
        canvas_doc.apply_ops(doc, [{"op": "put", "kind": "edge", "item": edge("a", "b", "c")}])


def test_change_summary_counts_by_kind() -> None:
    before = canvas_doc.check({"nodes": [card("a"), card("b")], "edges": [edge("e", "a", "b")]})
    after = canvas_doc.check({"nodes": [card("a", "x"), card("c"), {"id": "l", "type": "link", "url": "https://x", "x": 0, "y": 0, "width": 9, "height": 9}], "edges": []})
    assert canvas_doc.change_summary(before, "T", after, "U") == {"added": {"card": 1, "link": 1}, "removed": {"card": 1, "edge": 1}, "changed": 1, "renamed": True}
    assert canvas_doc.change_summary(None, None, after, "U") == {"created": True}


def test_the_preview_carries_no_text_from_the_document() -> None:
    svg = preview_svg(canvas_doc.check({"nodes": [card("a", "<script>alert(1)</script>", color="1"), card("b", x=400)], "edges": [edge("e", "a", "b")]}))
    assert svg.startswith("<svg") and "script" not in svg and "<path" in svg


# -- layout ----------------------------------------------------------------------------------------


def test_layout_puts_each_step_a_layer_further_and_survives_cycles() -> None:
    cards, lines = layout([{"id": "a", "text": "Start"}, {"id": "b", "text": "Work"}, {"id": "c", "text": "Done"}],
                          [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "a", "label": "again"}])
    by_id = {node["id"]: node for node in cards}
    assert by_id["a"]["x"] < by_id["b"]["x"] < by_id["c"]["x"]
    assert lines[0]["fromSide"] == "right" and lines[0]["toSide"] == "left"
    assert lines[2]["fromSide"] == "left" and lines[2]["label"] == "again"
    canvas_doc.check({"nodes": cards, "edges": lines})
    width, height = card_size("a much longer label that has to wrap over several lines of the card\nand a second paragraph")
    assert width == 360 and height > 60


# -- the store -------------------------------------------------------------------------------------


async def test_a_canvas_belongs_to_a_chat_or_its_project_and_lists_by_scope(db: Database, store: CanvasStore, published: Published) -> None:
    await _chat(db, "c1")
    await _chat(db, "c2")
    await _chat(db, "loner", project="scratch", ephemeral=True)
    mine = await store.create("Mine", Scope("chat", "c1"))
    shared = await store.create("Project board", Scope("project", "p"), owner_session_id="c2")
    await store.create("Other chat", Scope("chat", "c2"))
    listing = await store.list(session_id="c1")
    assert [scope["kind"] for scope in listing["scopes"]] == ["chat", "project"]
    assert {item["id"] for item in listing["items"]} == {mine["id"], shared["id"]}
    assert mine["owner_session_id"] == "c1" and shared["owner_session_id"] == "c2"
    assert (await store.list(session_id="loner"))["scopes"] == [{"kind": "chat", "id": "loner", "name": "Chat loner"}]
    everything = await store.list()
    assert len(everything["items"]) == 3 and {scope["kind"] for scope in everything["scopes"]} == {"chat", "project"}
    with pytest.raises(CanvasError, match="not a chat of that project"):
        await store.create("x", Scope("project", "p"), owner_session_id="loner")
    with pytest.raises(NotFound):
        await store.create("x", Scope("chat", "nobody"))
    assert published.events[0][0] == "canvas.changed" and published.events[0][2] == {"session_id": "c1", "project_id": None}


async def test_two_writers_on_different_items_both_save(db: Database, store: CanvasStore) -> None:
    await _chat(db, "c1")
    made = await store.create("Plan", Scope("chat", "c1"), doc={"nodes": [card("a"), card("b")], "edges": []})
    # The operator moves a; the agent, still holding version 1, relabels b.
    moved, merged = await store.apply(made["id"], 1, [{"op": "put", "kind": "node", "item": card("a", x=500)}])
    assert not merged and moved["version"] == 2
    relabelled, merged = await store.apply(made["id"], 1, [{"op": "put", "kind": "node", "item": card("b", "done")}], source="agent")
    assert merged and relabelled["version"] == 3
    nodes = {node["id"]: node for node in relabelled["doc"]["nodes"]}
    assert nodes["a"]["x"] == 500 and nodes["b"]["text"] == "done"
    # Now the agent, still at version 1, edits a: that one the operator changed, so it is refused.
    with pytest.raises(Stale) as refused:
        await store.apply(made["id"], 1, [{"op": "put", "kind": "node", "item": card("a", "agent's")}], source="agent")
    assert refused.value.ids == ["a"] and refused.value.current["version"] == 3
    # An edge onto a card the other writer deleted names that card.
    await store.apply(made["id"], 3, [{"op": "delete", "id": "b"}])
    with pytest.raises(Stale) as gone:
        await store.apply(made["id"], 3, [{"op": "put", "kind": "edge", "item": edge("e", "a", "b")}], source="agent")
    assert gone.value.ids == ["b"]
    # A rename while the cards moved still goes through; a second rename from the old version does not.
    assert (await store.rename(made["id"], "Plan B", 2))["title"] == "Plan B"
    with pytest.raises(Stale):
        await store.rename(made["id"], "Plan C", 2)
    with pytest.raises(CanvasError, match="not a version"):
        await store.apply(made["id"], 99, [{"op": "delete", "id": "a"}])


async def test_history_folds_the_operators_run_and_restores_as_a_new_version(db: Database, store: CanvasStore) -> None:
    await _chat(db, "c1")
    made = await store.create("Plan", Scope("chat", "c1"))
    await store.apply(made["id"], 1, [{"op": "put", "kind": "node", "item": card("a")}])
    await store.apply(made["id"], 2, [{"op": "put", "kind": "node", "item": card("b")}])
    await store.apply(made["id"], 3, [{"op": "put", "kind": "node", "item": card("c")}], source="agent")
    history = await store.versions(made["id"])
    assert [(row["version"], row["source"], row["kind"]) for row in history] == [(4, "agent", "edit"), (3, "operator", "edit"), (1, "operator", "create")]
    assert history[1]["summary"] == {"added": {"card": 2}, "removed": {}, "changed": 0, "renamed": False}
    restored = await store.restore(made["id"], 3, 4)
    assert restored["version"] == 5 and [node["id"] for node in restored["doc"]["nodes"]] == ["a", "b"]
    assert (await store.versions(made["id"]))[0]["summary"]["restored_from"] == 3
    with pytest.raises(Stale):
        await store.restore(made["id"], 1, 4)
    assert (await store.preview(made["id"]))["svg"].startswith("<svg")


async def test_share_export_import_duplicate_move_and_cascade(db: Database, store: CanvasStore) -> None:
    await _chat(db, "c1")
    made = await store.create("Plan", Scope("chat", "c1"), doc={"nodes": [card("a", "x")], "edges": []})
    token = await store.share(made["id"])
    assert (await store.shared(token))["doc"]["nodes"][0]["text"] == "x"
    await store.revoke_share(made["id"])
    assert await store.shared(token) is None
    title, text = await store.export_text(made["id"])
    assert title == "Plan" and text.startswith('{\n\t"nodes":[')
    imported = await store.import_text("Copy", text, Scope("project", "p"), owner_session_id="c1")
    assert imported["doc"] == made["doc"] and (await store.versions(imported["id"]))[0]["kind"] == "import"
    with pytest.raises(CanvasError, match="not JSON"):
        await store.import_text("Bad", "{nope", Scope("chat", "c1"))
    copy = await store.duplicate(made["id"])
    assert copy["title"] == "Plan (copy)" and copy["scope"] == "chat"
    moved = await store.move(made["id"], Scope("project", "p"))
    assert moved["scope"] == "project" and moved["owner_session_id"] == "c1"
    await db.execute("DELETE FROM sessions WHERE id='c1'")
    assert await store.get(copy["id"]) is None
    assert (await store.get(made["id"]))["owner_session_id"] is None


# -- conversion of the old diagrams ----------------------------------------------------------------


def _shape(element_id: str, kind: str, x: float, y: float, w: float = 200, h: float = 100, **extra: Any) -> dict[str, Any]:
    return {"id": element_id, "type": kind, "x": x, "y": y, "width": w, "height": h, "strokeColor": "#1e1e1e", **extra}


def _label(element_id: str, container: str, text: str) -> dict[str, Any]:
    return {"id": element_id, "type": "text", "x": 0, "y": 0, "width": 10, "height": 10, "containerId": container, "originalText": text, "text": text}


SCENE = {"elements": [
    _shape("box", "rectangle", 0, 0, strokeColor="#2f9e44"),
    _label("box-label", "box", "Start"),
    _shape("oval", "ellipse", 400, 0),
    _shape("gem", "diamond", 800, 0),
    {**_shape("hint", "text", 820, 30, 100, 20), "originalText": "inside the gem", "fontSize": 20},
    {**_shape("title", "text", 0, -200, 600, 50), "originalText": "Big title\nsub", "fontSize": 36},
    {**_shape("bound", "arrow", 205, 50, 190, 0), "points": [[0, 0], [190, 0]], "startBinding": {"elementId": "box"}, "endBinding": {"elementId": "oval"}, "endArrowhead": "arrow"},
    _label("bound-label", "bound", "next"),
    {**_shape("loose", "arrow", 604, 50, 192, 0), "points": [[0, 0], [100, 40], [192, 0]], "endArrowhead": "arrow"},
    {**_shape("axis", "line", 0, 400, 800, 0), "points": [[0, 0], [800, 0]]},
    {**_shape("gone", "rectangle", 0, 0), "isDeleted": True},
    {**_shape("scribble", "freedraw", 0, 0), "points": [[0, 0], [1, 1]]},
]}


def test_conversion_keeps_boxes_words_and_connections_and_counts_the_losses() -> None:
    result = convert(SCENE)
    nodes = {node["id"]: node for node in result.doc["nodes"]}
    assert set(nodes) == {"box", "oval", "gem", "title"}
    assert nodes["box"]["text"] == "Start" and nodes["box"]["color"] == "#2f9e44" and "daedalus" not in nodes["box"]
    assert nodes["oval"]["daedalus"] == {"shape": "ellipse"}
    assert nodes["gem"]["text"] == "inside the gem", "a free text inside an unlabelled shape is its label"
    assert nodes["title"]["text"] == "# Big title\nsub" and nodes["title"]["daedalus"] == {"shape": "text"}
    edges = {item["id"]: item for item in result.doc["edges"]}
    assert edges["bound"] == {"id": "bound", "fromNode": "box", "fromSide": "right", "toNode": "oval", "toSide": "left", "label": "next"}
    assert edges["loose"]["fromNode"] == "oval" and edges["loose"]["toNode"] == "gem", "a hand-drawn arrow touching two boxes is joined to them"
    assert result.shapes == {"ellipse": 1, "diamond": 1} and result.snapped == 1 and result.bends == 1
    assert result.dropped == {"line": 1, "freedraw": 1}
    canvas_doc.check(result.doc)


async def test_the_migration_converts_each_diagram_into_its_chats_project(db: Database) -> None:
    await _chat(db, "maker", project="work")
    await _chat(db, "solo", project="solo-scratch", ephemeral=True)
    await _chat(db, "boss", project="main", system="dispatcher")
    conn = db.conn
    await conn.executescript("""
        CREATE TABLE diagrams (id TEXT PRIMARY KEY, title TEXT, scene_json TEXT, version INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT,
            share_token TEXT DEFAULT '', updated_by TEXT DEFAULT 'user', preview_svg TEXT DEFAULT '');
        CREATE TABLE diagram_revisions (diagram_id TEXT, version INTEGER, title TEXT, scene_json TEXT, saved_at TEXT);
    """)
    for diagram_id, owner in (("d" * 32, "maker"), ("e" * 32, "solo"), ("f" * 32, None)):
        await conn.execute(
            "INSERT INTO diagrams(id,title,scene_json,created_at,updated_at,updated_by,preview_svg) VALUES(?,?,?,?,?,?,?)",
            (diagram_id, f"Plan: {diagram_id[0]} — v2", json.dumps(SCENE), "2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00", "agent",
             '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 300"></svg>'),
        )
        await conn.execute("INSERT INTO diagram_revisions VALUES (?,1,'x','{}','now')", (diagram_id,))
        if owner:
            await conn.execute(
                "INSERT INTO session_events(session_id,event_seq,history_revision,kind,payload,created_at) VALUES(?,1,1,?,?,?)",
                (owner, "tool_result", json.dumps({"metadata": {"diagram_id": diagram_id}}), "2026-10-01T00:00:00+00:00"),
            )
    await conn.execute("DELETE FROM canvases")
    await convert_diagrams(conn)
    await conn.executescript(SQL)
    await convert_diagrams(conn)  # a second run finds nothing to convert and changes nothing
    await conn.commit()
    rows = {row["id"][0]: dict(row) for row in await db.fetchall("SELECT id,title,session_id,project_id,owner_session_id,updated_by,created_at FROM canvases")}
    assert (rows["d"]["project_id"], rows["d"]["owner_session_id"]) == ("work", "maker")
    assert (rows["e"]["session_id"], rows["e"]["owner_session_id"]) == ("solo", "solo")
    assert (rows["f"]["project_id"], rows["f"]["owner_session_id"]) == ("main", None)
    assert rows["d"]["updated_by"] == "agent" and rows["d"]["created_at"] == "2026-10-01T00:00:00+00:00"
    tables = {row["name"] for row in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "diagrams" not in tables and "diagram_revisions" not in tables
    canvas = await CanvasStore(db).get("d" * 32)
    group = canvas["doc"]["nodes"][0]
    assert group["type"] == "group" and group["label"] == "Original Excalidraw drawing"
    files = {file["name"]: file for file in canvas["files"]}
    assert set(files) == {"preview.svg", "Plan d v2.excalidraw"}
    picture = next(node for node in canvas["doc"]["nodes"] if node["id"].startswith("legacy-picture"))
    assert (picture["width"], picture["height"]) == (900, 300) and picture["file"] == f"files/{files['preview.svg']['id']}/preview.svg"
    original = await CanvasStore(db).file("d" * 32, files["Plan d v2.excalidraw"]["id"])
    assert json.loads(original["data"]) == SCENE
    history = await CanvasStore(db).versions("d" * 32)
    assert history[0]["kind"] == "import" and history[0]["summary"]["converted"]["dropped"] == {"line": 1, "freedraw": 1}


# -- the routes ------------------------------------------------------------------------------------


@pytest.fixture
async def manager(settings: Settings, config: RuntimeConfig, db: Database) -> Any:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    yield manager
    await manager.close()


async def test_the_board_routes(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager, tmp_path: Path) -> None:
    root = tmp_path / "firm"
    root.mkdir()
    async with await _client(settings, config, db, manager) as client:
        assert (await client.get("/api/canvases")).status_code == 401
        project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Firm", "folders": [{"path": str(root)}]})).json()
        member = (await manager.create_session("Member", project_id=project["id"])).session.id
        made = await client.post("/api/canvases", headers=HEADERS, json={"title": "Board", "scope": "project", "scope_id": project["id"], "owner_session_id": member})
        assert made.status_code == 201, made.text
        canvas = made.json()
        listing = (await client.get(f"/api/canvases?session_id={member}", headers=HEADERS)).json()
        assert [item["id"] for item in listing["items"]] == [canvas["id"]] and listing["scopes"][1]["name"] == "Firm"

        put = {"base_version": 1, "ops": [{"op": "put", "kind": "node", "item": card("a", "hello")}]}
        saved = await client.post(f"/api/canvases/{canvas['id']}/ops", headers=HEADERS, json=put)
        assert saved.status_code == 200 and saved.json()["canvas"]["version"] == 2 and saved.json()["merged"] is False
        clash = await client.post(f"/api/canvases/{canvas['id']}/ops", headers=HEADERS, json={"base_version": 1, "ops": [{"op": "put", "kind": "node", "item": card("a", "other")}]})
        assert clash.status_code == 409
        assert clash.json()["detail"]["conflicts"] == ["a"] and clash.json()["detail"]["current"]["version"] == 2
        bad = await client.post(f"/api/canvases/{canvas['id']}/ops", headers=HEADERS, json={"base_version": 2, "ops": [{"op": "jump"}]})
        assert bad.status_code == 400

        exported = await client.get(f"/api/canvases/{canvas['id']}/export", headers=HEADERS)
        assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"] and exported.text.startswith("{\n\t")
        imported = await client.post("/api/canvases/import", headers=HEADERS, json={"title": "Again", "scope": "chat", "scope_id": member, "text": exported.text})
        assert imported.status_code == 201 and imported.json()["doc"]["nodes"][0]["text"] == "hello"

        url = (await client.post(f"/api/canvases/{canvas['id']}/share", headers=HEADERS)).json()["url"]
        public = await client.get(f"/api/public/canvases/{url.rsplit('/', 1)[1]}")
        assert public.status_code == 200 and "id" not in public.json() and public.json()["title"] == "Board"
        assert (await client.get("/api/public/canvases/nope")).status_code == 404
        assert len((await client.get(f"/api/canvases/{canvas['id']}/versions", headers=HEADERS)).json()) == 2
        assert (await client.delete(f"/api/canvases/{canvas['id']}", headers=HEADERS)).json() == {"ok": True}
        assert (await client.get(f"/api/canvases/{canvas['id']}", headers=HEADERS)).status_code == 404


async def test_an_svg_attachment_is_served_inert(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> None:
    chat = (await manager.create_session("Chat")).session.id
    made = await store_for(manager).create("Board", Scope("chat", chat))
    await db.execute(
        "INSERT INTO canvas_files(id,canvas_id,name,mime,size,data,created_at) VALUES('f1',?,'p.svg','image/svg+xml',5,?,'now')",
        (made["id"], b"<svg/>"),
    )
    async with await _client(settings, config, db, manager) as client:
        shown = await client.get(f"/api/canvases/{made['id']}/files/f1", headers=HEADERS)
        assert shown.status_code == 200 and shown.headers["content-type"].startswith("image/svg+xml")
        assert "sandbox" in shown.headers["content-security-policy"] and shown.headers["x-content-type-options"] == "nosniff"
        saved = await client.get(f"/api/canvases/{made['id']}/files/f1?save=1", headers=HEADERS)
        assert saved.headers["content-type"] == "application/octet-stream" and "attachment" in saved.headers["content-disposition"]


# -- the agent's tools -----------------------------------------------------------------------------


async def test_the_agent_creates_reads_and_edits_alongside_the_operator(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    member = (await manager.create_session("Member")).session.id
    stranger = (await manager.create_session("Stranger")).session.id
    foreign = await store_for(manager).create("Not yours", Scope("chat", stranger))
    services = SimpleNamespace(max_tool_output_chars=60_000, extra={"manager": manager})
    monkeypatch.setattr("daedalus.tools._common.services_for", lambda _: services)
    monkeypatch.setattr(canvas_tools, "services_for", lambda _: services)
    context = ToolContext(tenant_id="operator", run_id="run", session_id=member, metadata={"tool_call_id": "call-1"})

    async def call(tool: Any, **arguments: Any) -> Any:
        return await tool().invoke(context, arguments)

    async with manager.bus.subscribe(EventFilter(types=("canvas.changed",))) as subscription:
        made = await call(canvas_tools.canvas_create, title="Flow", nodes=[{"id": "a", "text": "Order"}, {"id": "b", "text": "Pay", "color": "4"}], edges=[{"from": "a", "to": "b", "label": "then"}])
        assert not made.is_error, made.content
        event = await asyncio.wait_for(subscription.__anext__(), 2)
    assert event.payload["by"] == "agent" and event.session_id == member
    canvas_id = made.metadata["canvas_id"]
    assert f"/app/canvas/{canvas_id}" in made.content
    assert (await call(canvas_tools.canvas_create, title="x", scope="project")).is_error, "a plain chat has no project scope"

    listed = await call(canvas_tools.canvas_list)
    assert "Flow" in listed.content and "Not yours" not in listed.content
    read = await call(canvas_tools.canvas_read, canvas_id=canvas_id)
    assert "version 1" in read.content and 'a text at' in read.content and "a.right -> b.left" in read.content and '"then"' in read.content
    assert (await call(canvas_tools.canvas_read, canvas_id=foreign["id"])).is_error

    # The operator drags card a meanwhile; the agent's edit of b and a new card still saves.
    await store_for(manager).apply(canvas_id, 1, [{"op": "put", "kind": "node", "item": {**(await store_for(manager).get(canvas_id))["doc"]["nodes"][0], "x": 900}}])
    edited = await call(canvas_tools.canvas_edit, canvas_id=canvas_id, version=1, nodes=[{"id": "b", "text": "Pay by card\nor cash"}, {"id": "c", "text": "Ship"}], edges=[{"from": "b", "to": "c"}])
    assert not edited.is_error, edited.content
    assert "operator's newer changes" in edited.content
    doc = (await store_for(manager).get(canvas_id))["doc"]
    nodes = {node["id"]: node for node in doc["nodes"]}
    assert nodes["a"]["x"] == 900 and nodes["b"]["text"] == "Pay by card\nor cash" and nodes["b"]["color"] == "4"
    assert nodes["c"]["x"] > max(node["x"] + node["width"] for node in (nodes["a"], nodes["b"])) and nodes["c"]["width"] >= 180
    assert any(item["fromNode"] == "b" and item["toNode"] == "c" for item in doc["edges"])
    stale = await call(canvas_tools.canvas_edit, canvas_id=canvas_id, version=1, nodes=[{"id": "a", "color": "1"}])
    assert stale.is_error and "operator changed a" in stale.content

    laid = await call(canvas_tools.canvas_edit, canvas_id=canvas_id, version=3, layout_graph={"nodes": [{"id": "x", "text": "X"}, {"id": "y", "text": "Y"}], "edges": [{"from": "x", "to": "y"}]}, delete_ids=["c"])
    assert not laid.is_error, laid.content
    doc = (await store_for(manager).get(canvas_id))["doc"]
    assert {"x", "y"} <= {node["id"] for node in doc["nodes"]} and "c" not in {node["id"] for node in doc["nodes"]}
    assert not any("c" in (item["fromNode"], item["toNode"]) for item in doc["edges"])


def test_the_navigation_keeps_the_spot_of_the_screen_it_renames() -> None:
    from daedalus.config import _migrate_diagrams_to_canvas

    raw: dict[str, Any] = {"navigation": {"rail": ["terminals", "diagrams", "board"], "hidden": ["diagrams", "canvas"], "icons": {"diagrams": "pen"}}}
    assert _migrate_diagrams_to_canvas(raw)
    assert raw["navigation"] == {"rail": ["terminals", "canvas", "board"], "hidden": ["canvas"], "icons": {"canvas": "pen"}}
    assert not _migrate_diagrams_to_canvas(raw)
