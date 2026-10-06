import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from protocore.contracts.tools import ToolContext

from daedalus.extensions.api_workspace import register
from daedalus.stores.database import Database
from daedalus.stores.diagrams import DiagramStore, shape_label


@pytest.mark.asyncio
async def test_diagram_version_rejects_concurrent_save(tmp_path):
    db = Database(tmp_path / "diagrams.sqlite")
    await db.open()
    try:
        store = DiagramStore(db)
        made = await store.create("Flow")
        scene = {"elements": [{"id": "box", "type": "rectangle", "x": 20, "y": 20}], "appState": {}, "files": {}}
        saved = await store.save(made["id"], "Flow", scene, made["version"])
        assert saved["version"] == made["version"] + 1
        assert json.loads((await db.fetchone("SELECT scene_json FROM diagrams WHERE id=?", (made["id"],)))["scene_json"])["elements"][0]["id"] == "box"
        with pytest.raises(RuntimeError, match="changed elsewhere"):
            await store.save(made["id"], "Old", scene, made["version"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_diagram_routes_share_the_store(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        app = FastAPI()
        register(app, SimpleNamespace(db=db), lambda: {"user_id": "operator"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            made = await client.post("/api/diagrams", json={"title": "Flow"})
            assert made.status_code == 201
            diagram = made.json()
            saved = await client.put(f"/api/diagrams/{diagram['id']}", json={"title": "Flow", "scene": {"elements": [], "appState": {}, "files": {}}, "version": 1})
            assert saved.json()["version"] == 2
            assert (await client.get("/api/diagrams")).json()[0]["title"] == "Flow"
            versions = (await client.get(f"/api/diagrams/{diagram['id']}/versions")).json()
            assert [item["version"] for item in versions] == [2, 1]
            assert (await client.get(f"/api/diagrams/{diagram['id']}/versions/1")).json()["scene"]["elements"] == []
            shared = await client.post(f"/api/diagrams/{diagram['id']}/share")
            assert shared.status_code == 200
            token = shared.json()["url"].split("/")[-1]
            public = await client.get(f"/api/public/diagrams/{token}")
            assert public.json()["title"] == "Flow"
            assert "share_token" not in public.json()
            guest_app = FastAPI()

            def deny() -> None:
                raise HTTPException(401)

            register(guest_app, SimpleNamespace(db=db), deny)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest:
                assert (await guest.get(f"/api/public/diagrams/{token}")).status_code == 200
                assert (await guest.get(f"/api/diagrams/{diagram['id']}")).status_code == 401
            assert (await client.delete(f"/api/diagrams/{diagram['id']}/share")).status_code == 200
            assert (await client.get(f"/api/public/diagrams/{token}")).status_code == 404
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_diagram_session_list_and_legacy_shape_labels(tmp_path):
    db = Database(tmp_path / "diagrams.sqlite")
    await db.open()
    try:
        store = DiagramStore(db)
        made = await store.create("Flow", {"elements": [{"id": "box", "type": "rectangle", "x": 10, "y": 20, "width": 200, "height": 100}], "appState": {}, "files": {}})
        await db.execute("INSERT INTO projects(id,name,created_at,settings,system) VALUES('project','Flow','2026-01-01','{}','')")
        await db.execute("INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,project_id) VALUES(?,?,?,?,?,?)", ("session", "operator", "Flow", "2026-01-01", "2026-01-01", "project"))
        event = {"metadata": {"diagram_id": made["id"]}, "content_blocks": [{"type": "text", "text": "created"}]}
        await db.execute("INSERT INTO session_events(session_id,event_seq,history_revision,kind,payload,created_at) VALUES(?,?,?,?,?,?)", ("session", 1, 0, "tool_result", json.dumps(event), "2026-01-01"))
        edit = {"final_input": {"diagram_id": made["id"], "elements": [{"id": "box", "type": "rectangle", "text": "Inside the box"}]}}
        await db.execute("INSERT INTO session_events(session_id,event_seq,history_revision,kind,payload,created_at) VALUES(?,?,?,?,?,?)", ("session", 2, 0, "tool_use_stop", json.dumps(edit), "2026-01-02"))
        assert [item["id"] for item in await store.list("session")] == [made["id"]]
        found = await store.get(made["id"])
        assert found is not None
        assert found["scene"]["elements"][1]["text"] == "Inside the box"
        assert found["scene"]["elements"][0]["boundElements"] == [{"id": "box-label", "type": "text"}]
        assert found["scene"]["elements"][1]["containerId"] == "box"
        assert (await store.get(made["id"]))["scene"] == found["scene"]
        assert shape_label(found["scene"]["elements"][0], "New")["type"] == "text"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_agent_edit_keeps_text_inside_shapes(tmp_path, monkeypatch):
    from daedalus.tools import diagrams as diagram_tools  # Lazy: the tool is needed only for this integration check.

    db = Database(tmp_path / "diagrams.sqlite")
    await db.open()
    try:
        store = DiagramStore(db)
        made = await store.create("Flow")
        monkeypatch.setattr(diagram_tools, "_store", lambda _context: store)
        context = ToolContext(tenant_id="operator", run_id="run", session_id="session")
        result = await diagram_tools.diagram_edit().invoke(context, {"diagram_id": made["id"], "version": 1, "elements": [{"id": "box", "type": "rectangle", "x": 20, "y": 30, "width": 240, "height": 120, "text": "The worker writes here"}]})
        assert not result.is_error
        scene = (await store.get(made["id"]))["scene"]
        assert [(item["id"], item.get("text")) for item in scene["elements"]] == [("box", None), ("box-label", "The worker writes here")]
        assert scene["elements"][0]["boundElements"] == [{"id": "box-label", "type": "text"}]
        assert scene["elements"][1]["containerId"] == "box"
    finally:
        await db.close()
