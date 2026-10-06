"""The diagram store and its routes: the version lock, the coarse history behind it, restore, duplicate,
the cheap head the editor polls, thumbnails, sharing, and the migration that repaired old scenes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from daedalus.extensions.api_diagrams import register
from daedalus.stores import database
from daedalus.stores import diagrams as diagram_store
from daedalus.stores.database import MIGRATIONS, Database, DataMigration
from daedalus.stores.diagram_preview import preview_svg
from daedalus.stores.diagrams import DiagramStore, change_summary, repair_history

BOX = {"id": "box", "type": "rectangle", "x": 20, "y": 20, "width": 200, "height": 100, "strokeColor": "#1e1e1e", "backgroundColor": "transparent", "version": 1, "versionNonce": 1}


def scene(*elements: dict) -> dict:
    return {"elements": [dict(element) for element in elements], "appState": {}, "files": {}}


class Clock:
    """The store's idea of now, moved by the test: coalescing is a matter of minutes."""

    def __init__(self) -> None:
        self.minute = 0

    def __call__(self) -> str:
        return f"2026-10-06T10:{self.minute:02d}:00+00:00" if self.minute < 60 else f"2026-10-06T11:{self.minute - 60:02d}:00+00:00"


@pytest.fixture
async def store(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(diagram_store, "now", clock)
    db = Database(tmp_path / "diagrams.sqlite")
    await db.open()
    try:
        yield DiagramStore(db), clock
    finally:
        await db.close()


async def test_a_stale_version_is_refused(store):
    diagrams, _ = store
    made = await diagrams.create("Flow")
    saved = await diagrams.save(made["id"], "Flow", scene(BOX), made["version"])
    assert saved["version"] == 2 and saved["scene"]["elements"][0]["id"] == "box"
    with pytest.raises(RuntimeError, match="changed elsewhere"):
        await diagrams.save(made["id"], "Old", scene(BOX), made["version"])
    with pytest.raises(KeyError):
        await diagrams.save("missing", "Flow", scene(BOX), 1)


async def test_the_operators_autosaves_fold_into_one_revision_for_ten_minutes(store):
    diagrams, clock = store
    made = await diagrams.create("Flow")
    version = made["version"]
    for minute in (1, 2, 5, 9):
        clock.minute = minute
        moved = {**BOX, "x": minute * 10}
        version = (await diagrams.save(made["id"], "Flow", scene(moved), version))["version"]
    history = await diagrams.versions(made["id"])
    assert [(item["version"], item["kind"], item["source"]) for item in history] == [(5, "edit", "user"), (1, "create", "user")]
    assert history[0]["summary"] == {"added": {"shape": 1}, "removed": {}, "changed": 0, "renamed": False}
    assert history[0]["saved_at"].startswith("2026-10-06T10:09") and history[0]["started_at"].startswith("2026-10-06T10:01")
    clock.minute = 12
    version = (await diagrams.save(made["id"], "Flow", scene({**BOX, "x": 500}), version))["version"]
    history = await diagrams.versions(made["id"])
    assert [item["version"] for item in history] == [6, 5, 1]
    # The new run is measured against the revision before it, the folded one, not against a save a second ago.
    assert history[0]["summary"] == {"added": {}, "removed": {}, "changed": 1, "renamed": False}


async def test_agent_edits_and_restores_keep_revisions_of_their_own(store):
    diagrams, clock = store
    made = await diagrams.create("Flow", source="agent")
    clock.minute = 1
    first = await diagrams.save(made["id"], "Flow", scene(BOX), 1, source="agent")
    clock.minute = 2
    second = await diagrams.save(made["id"], "Flow chart", scene(BOX, {**BOX, "id": "other", "x": 300}), first["version"], source="agent")
    clock.minute = 3
    mine = await diagrams.save(made["id"], "Flow chart", scene(BOX), second["version"])
    clock.minute = 4
    restored = await diagrams.restore(made["id"], second["version"], mine["version"])
    assert restored["version"] == 5 and len(restored["scene"]["elements"]) == 2 and restored["updated_by"] == "user"
    clock.minute = 5
    after = await diagrams.save(made["id"], "Flow chart", scene(BOX), restored["version"])
    history = await diagrams.versions(made["id"])
    assert [(item["version"], item["source"], item["kind"]) for item in history] == [
        (after["version"], "user", "edit"), (5, "user", "restore"), (4, "user", "edit"), (3, "agent", "edit"), (2, "agent", "edit"), (1, "agent", "create"),
    ]
    assert history[1]["restored_from"] == 3 and history[1]["summary"]["restored_from"] == 3
    assert history[3]["summary"] == {"added": {"shape": 1}, "removed": {}, "changed": 0, "renamed": True}
    assert (await diagrams.head(made["id"])) == {"id": made["id"], "title": "Flow chart", "version": 6, "updated_at": "2026-10-06T10:05:00+00:00", "updated_by": "user"}


async def test_a_restore_on_a_stale_version_is_refused_and_changes_nothing(store):
    diagrams, _ = store
    made = await diagrams.create("Flow")
    saved = await diagrams.save(made["id"], "Flow", scene(BOX), 1)
    with pytest.raises(RuntimeError):
        await diagrams.restore(made["id"], 1, 1)
    with pytest.raises(ValueError):
        await diagrams.restore(made["id"], 99, saved["version"])
    assert (await diagrams.get(made["id"]))["version"] == saved["version"]
    assert [item["version"] for item in await diagrams.versions(made["id"])] == [2, 1]


async def test_the_history_is_capped(store, monkeypatch):
    diagrams, clock = store
    monkeypatch.setattr(diagram_store, "REVISION_CAP", 3)
    made = await diagrams.create("Flow")
    version = 1
    for step in range(5):
        clock.minute = step
        version = (await diagrams.save(made["id"], "Flow", scene({**BOX, "x": step}), version, source="agent"))["version"]
    assert [item["version"] for item in await diagrams.versions(made["id"])] == [6, 5, 4]


async def test_duplicate_rename_and_previews(store):
    diagrams, _ = store
    made = await diagrams.create("Flow", scene(BOX))
    copy = await diagrams.duplicate(made["id"])
    assert copy["id"] != made["id"] and copy["title"] == "Flow (copy)" and copy["scene"]["elements"][0]["id"] == "box"
    assert (await diagrams.duplicate(made["id"], "Second"))["title"] == "Second"
    renamed = await diagrams.rename(made["id"], "Pipeline", 1)
    assert renamed["title"] == "Pipeline" and renamed["version"] == 2
    assert (await diagrams.versions(made["id"]))[0]["summary"]["renamed"] is True
    preview = await diagrams.preview(made["id"])
    assert preview is not None and preview["version"] == 2 and preview["svg"].startswith("<svg") and "<rect" in preview["svg"]
    assert (await diagrams.preview(made["id"], 1))["svg"] == preview["svg"]
    listed = await diagrams.list()
    assert {item["title"] for item in listed} == {"Pipeline", "Flow (copy)", "Second"}
    assert all("scene" not in item and "preview_svg" not in item and item["shared"] is False for item in listed)


def test_a_change_summary_speaks_of_items_not_element_ids():
    label = {"id": "box-label", "type": "text", "containerId": "box", "text": "Old", "originalText": "Old", "x": 0, "y": 0}
    arrow = {"id": "a", "type": "arrow", "x": 0, "y": 0, "points": [[0, 0], [10, 0]]}
    before = scene(BOX, label)
    # A renamed label is a change to its box; a redraw that only moved Excalidraw's bookkeeping is no change at all.
    after = scene({**BOX, "version": 7, "versionNonce": 99, "boundElements": [{"id": "box-label", "type": "text"}]}, {**label, "text": "New", "originalText": "New"}, arrow, {"id": "note", "type": "text", "text": "hi", "x": 0, "y": 0})
    assert change_summary(before, "Flow", after, "Flow") == {"added": {"connector": 1, "text": 1}, "removed": {}, "changed": 1, "renamed": False}
    assert change_summary(None, None, after, "Flow") == {"created": True}
    deleted = scene({**BOX, "isDeleted": True})
    assert change_summary(before, "Flow", deleted, "Flow")["removed"] == {"shape": 1}


def test_a_preview_keeps_only_plain_colours():
    svg = preview_svg(scene({**BOX, "strokeColor": "url(javascript:alert(1))", "backgroundColor": "#a5d8ff"}, {"id": "t", "type": "text", "text": "<b>hi</b>", "x": 0, "y": 0, "width": 40, "height": 25, "fontSize": 20}))
    assert "javascript" not in svg and "<b>" not in svg and 'fill="#a5d8ff"' in svg
    assert preview_svg(scene()) == ""


async def test_routes_answer_for_the_whole_life_of_a_diagram(tmp_path):
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        app = FastAPI()
        register(app, SimpleNamespace(db=db), lambda: {"user_id": "operator"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            made = await client.post("/api/diagrams", json={"title": "Flow"})
            assert made.status_code == 201
            diagram = made.json()
            saved = await client.put(f"/api/diagrams/{diagram['id']}", json={"title": "Flow", "scene": scene(BOX), "version": 1})
            assert saved.json()["version"] == 2 and saved.json()["updated_by"] == "user"
            listed = (await client.get("/api/diagrams")).json()
            assert listed[0]["title"] == "Flow" and listed[0]["updated_by"] == "user" and "scene" not in listed[0]
            head = (await client.get(f"/api/diagrams/{diagram['id']}/head")).json()
            assert head["version"] == 2 and set(head) == {"id", "title", "version", "updated_at", "updated_by"}
            versions = (await client.get(f"/api/diagrams/{diagram['id']}/versions")).json()
            assert [(item["version"], item["source"]) for item in versions] == [(2, "user"), (1, "user")]
            assert (await client.get(f"/api/diagrams/{diagram['id']}/versions/1")).json()["scene"]["elements"] == []
            assert (await client.get(f"/api/diagrams/{diagram['id']}/versions/2/preview")).json()["svg"].startswith("<svg")
            assert (await client.get(f"/api/diagrams/{diagram['id']}/preview")).json()["version"] == 2
            stale = await client.post(f"/api/diagrams/{diagram['id']}/restore", json={"revision": 1, "version": 1})
            assert stale.status_code == 409
            restored = await client.post(f"/api/diagrams/{diagram['id']}/restore", json={"revision": 1, "version": 2})
            assert restored.status_code == 200 and restored.json()["version"] == 3 and restored.json()["scene"]["elements"] == []
            renamed = await client.patch(f"/api/diagrams/{diagram['id']}", json={"title": "Pipeline", "version": 3})
            assert renamed.json()["title"] == "Pipeline"
            assert (await client.patch(f"/api/diagrams/{diagram['id']}", json={"title": "Again", "version": 3})).status_code == 409
            copy = await client.post(f"/api/diagrams/{diagram['id']}/duplicate", json={})
            assert copy.status_code == 201 and copy.json()["title"] == "Pipeline (copy)"
            assert (await client.post("/api/diagrams/missing/duplicate", json={})).status_code == 404
            assert (await client.get("/api/diagrams/missing/head")).status_code == 404
            shared = await client.post(f"/api/diagrams/{diagram['id']}/share")
            token = shared.json()["url"].split("/")[-1]
            assert (await client.get("/api/diagrams")).json()[1]["shared"] is True
            public = (await client.get(f"/api/public/diagrams/{token}")).json()
            assert public["title"] == "Pipeline" and "share_token" not in public and "updated_at" in public
            guest_app = FastAPI()

            def deny() -> None:
                raise HTTPException(401)

            register(guest_app, SimpleNamespace(db=db), deny)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest:
                assert (await guest.get(f"/api/public/diagrams/{token}")).status_code == 200
                assert (await guest.get(f"/api/diagrams/{diagram['id']}")).status_code == 401
                assert (await guest.get(f"/api/diagrams/{diagram['id']}/head")).status_code == 401
            assert (await client.delete(f"/api/diagrams/{diagram['id']}/share")).status_code == 200
            assert (await client.get(f"/api/public/diagrams/{token}")).status_code == 404
            assert (await client.delete(f"/api/diagrams/{diagram['id']}")).status_code == 200
            assert (await client.get(f"/api/diagrams/{diagram['id']}/versions")).status_code == 404
    finally:
        await db.close()


async def test_the_session_list_finds_diagrams_by_the_created_id(store):
    diagrams, _ = store
    made = await diagrams.create("Flow")
    await diagrams.create("Elsewhere")
    db = diagrams.db
    await db.execute("INSERT INTO projects(id,name,created_at,settings,system) VALUES('project','Flow','2026-01-01','{}','')")
    await db.execute("INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,project_id) VALUES(?,?,?,?,?,?)", ("session", "operator", "Flow", "2026-01-01", "2026-01-01", "project"))
    event = {"metadata": {"diagram_id": made["id"]}, "content_blocks": [{"type": "text", "text": "created"}]}
    await db.execute("INSERT INTO session_events(session_id,event_seq,history_revision,kind,payload,created_at) VALUES(?,?,?,?,?,?)", ("session", 1, 0, "tool_result", json.dumps(event), "2026-01-01"))
    assert [item["id"] for item in await diagrams.list("session")] == [made["id"]]


HISTORY = next(index for index, migration in enumerate(MIGRATIONS) if isinstance(migration, DataMigration) and "summary_json" in migration.sql)
"""The schema the history migration upgrades from, found by what it does rather than by its number."""


def seed_before_history(path: Path) -> str:
    """A diagram as the earlier build left it: the agent created it and drew a labelled box whose label
    the old tool dropped, then the operator moved the box twice."""
    raw = sqlite3.connect(path)
    raw.executescript(f"CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES ({HISTORY});")
    opening = SimpleNamespace(workspaces_dir=path.parent / "workspaces", local_env="container")
    for script in MIGRATIONS[:HISTORY]:
        script = script(opening) if callable(script) else script
        raw.executescript(script[0] if isinstance(script, tuple) else script)
    diagram_id = "d" * 32
    raw.execute("INSERT INTO projects(id,name,created_at,settings,system) VALUES('project','Flow','2026-01-01','{}','')")
    raw.execute("INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,project_id) VALUES('session','operator','Flow','2026-01-01','2026-01-01','project')")
    created = {"metadata": {"diagram_id": diagram_id}, "content_blocks": [{"type": "text", "text": "created"}]}
    edit = {"final_input": {"diagram_id": diagram_id, "version": 1, "elements": [{"id": "box", "type": "rectangle", "text": "Inside the box"}]}}
    raw.execute("INSERT INTO session_events(session_id,event_seq,history_revision,kind,payload,created_at) VALUES('session',1,0,'tool_result',?,'2026-10-01T00:00:00+00:00')", (json.dumps(created),))
    raw.execute("INSERT INTO session_events(session_id,event_seq,history_revision,kind,payload,created_at) VALUES('session',2,0,'tool_use_stop',?,'2026-10-01T00:01:00+00:00')", (json.dumps(edit),))
    scenes = [scene(), scene(BOX), scene({**BOX, "x": 40}), scene({**BOX, "x": 60})]
    raw.execute("INSERT INTO diagrams(id,title,scene_json,version,created_at,updated_at) VALUES(?,?,?,?,?,?)", (diagram_id, "Flow", json.dumps(scenes[-1]), 4, "2026-10-01T00:00:00+00:00", "2026-10-01T00:04:00+00:00"))
    for version, content in enumerate(scenes, start=1):
        raw.execute("INSERT INTO diagram_revisions(diagram_id,version,title,scene_json,saved_at) VALUES(?,?,?,?,?)", (diagram_id, version, "Flow", json.dumps(content), f"2026-10-01T00:0{version}:00+00:00"))
    raw.commit()
    raw.close()
    return diagram_id


async def test_the_history_migration_repairs_lost_labels_and_tells_the_agent_from_the_operator(tmp_path):
    path = tmp_path / "state.sqlite"
    diagram_id = seed_before_history(path)
    db = Database(path)
    await db.open()
    try:
        diagrams = DiagramStore(db)
        found = await diagrams.get(diagram_id)
        label = next(item for item in found["scene"]["elements"] if item["type"] == "text")
        assert label["containerId"] == "box" and label["originalText"] == "Inside the box"
        assert {"id": label["id"], "type": "text"} in found["scene"]["elements"][0]["boundElements"]
        assert found["updated_by"] == "user"
        history = await diagrams.versions(diagram_id)
        assert [(item["version"], item["source"], item["kind"]) for item in history] == [(4, "user", "edit"), (3, "user", "edit"), (2, "agent", "edit"), (1, "agent", "create")]
        assert history[2]["summary"] == {"added": {"shape": 1}, "removed": {}, "changed": 0, "renamed": False}
        assert history[0]["summary"]["changed"] == 1
        assert (await diagrams.version(diagram_id, 2))["scene"]["elements"][1]["containerId"] == "box"
        assert (await diagrams.preview(diagram_id))["svg"].startswith("<svg")
        before = await db.fetchall("SELECT * FROM diagram_revisions ORDER BY version")
        current = await db.fetchone("SELECT * FROM diagrams")
        # Run again on the repaired data: nothing moves, and the label is not added twice.
        async with db.transaction() as conn:
            await repair_history(conn)
        assert [dict(row) for row in await db.fetchall("SELECT * FROM diagram_revisions ORDER BY version")] == [dict(row) for row in before]
        assert dict(await db.fetchone("SELECT * FROM diagrams")) == dict(current)
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
    finally:
        await db.close()


async def test_a_failing_data_step_rolls_the_schema_back(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    seed_before_history(path)

    async def broken(conn) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("repair failed")

    patched = list(MIGRATIONS)
    patched[HISTORY] = DataMigration(MIGRATIONS[HISTORY].sql, broken)  # type: ignore[union-attr]
    monkeypatch.setattr(database, "MIGRATIONS", patched)
    db = Database(path)
    with pytest.raises(RuntimeError, match="repair failed"):
        await db.open()
    await db.close()
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == HISTORY
        assert "source" not in {row[1] for row in raw.execute("PRAGMA table_info(diagram_revisions)")}
