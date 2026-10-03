"""The project routes: folders, the environment each lives in, the brief and the journal.

Every refusal here is one the app shows as it is written, so the tests read the sentence as well as
the status: a 409 that does not say which agent is in the way leaves the operator guessing.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions import api_projects
from daedalus.extensions.api import build_app
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import ProjectFolder

HEADERS = {"X-Daedalus-Token": "tok"}


async def _write(client: httpx.AsyncClient, method: str, path: str,
                 body: dict[str, Any] | None = None) -> httpx.Response:
    """Exercise the command API, then inspect the project projection these behavior tests describe."""
    project_id = re.search(r"/api/projects/([^/]+)", path).group(1)  # type: ignore[union-attr]
    projects = (await client.get("/api/projects", headers=HEADERS)).json()
    current = next((item for item in projects if item["id"] == project_id), None)
    payload = {**(body or {}), "client_operation_id": f"test-{uuid.uuid4().hex}",
               "expected_entity_revision": current["entity_revision"] if current else 1}
    response = await client.request(method, path, headers=HEADERS, json=payload)
    if response.status_code != 200:
        return response
    project = next(item for item in (await client.get("/api/projects", headers=HEADERS)).json()
                   if item["id"] == project_id)
    return httpx.Response(200, json=project)


def _client(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager) -> httpx.AsyncClient:
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions={}, guard=None)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")  # type: ignore[arg-type]


@pytest.fixture
async def running(settings: Settings, config: RuntimeConfig, db: Database) -> Any:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with _client(settings, config, db, manager) as client:
            yield manager, client
    finally:
        await manager.close()


def _dirs(tmp_path: Path, *names: str) -> list[Path]:
    made = []
    for name in names:
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
        made.append(tmp_path / name)
    return made


async def _events(db: Database, project_id: str) -> list[str]:
    rows = await db.fetchall("SELECT payload_json FROM app_events WHERE type = 'project.changed' AND project_id = ? ORDER BY seq", (project_id,))
    return [json.loads(r["payload_json"])["change"] for r in rows]


async def test_a_project_is_made_with_its_folders_in_order(running: Any, tmp_path: Path, db: Database) -> None:
    _, client = running
    site, docs = _dirs(tmp_path, "site", "docs")
    made = await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(site), "label": "Site"}, {"path": str(docs), "readonly": True}]})
    assert made.status_code == 200, made.text
    project = made.json()
    assert [(f["path"], f["label"], f["readonly"], f["position"], f["reach"]) for f in project["folders"]] == [
        (str(site), "Site", False, 0, "agents"),
        (str(docs), "", True, 1, "agents"),
    ]
    assert project["settings"]["snapshots"] is False and project["settings"]["default_env"] == "container"
    assert await _events(db, project["id"]) == ["created"]

    scratch = (await client.post("/api/projects", headers=HEADERS, json={"name": "Scratch"})).json()
    assert scratch["folders"][0]["managed"] is True and scratch["settings"]["snapshots"] is True

    among = await client.post("/api/projects", headers=HEADERS, json={"name": "Nested", "folders": [{"path": str(tmp_path / "a")}, {"path": str(tmp_path / "a" / "b")}]})
    assert among.status_code == 400 and "nest" in among.json()["detail"]
    old = await client.post("/api/projects", headers=HEADERS, json={"name": "Old", "root": str(site)})
    assert old.status_code == 422, "a single root is not a request this API understands any more"


async def test_a_host_folder_needs_the_host_bridge(running: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, client = running
    (site,) = _dirs(tmp_path, "site")
    environments = (await client.get("/api/project-environments", headers=HEADERS)).json()
    assert environments == {"local": "container", "available": ["container"], "host_bridge": False, "docker": True}
    refused = await client.post("/api/projects", headers=HEADERS, json={"name": "Host", "folders": [{"path": "/somewhere/on/the/host", "env": "host"}]})
    assert refused.status_code == 400 and "host terminal bridge" in refused.json()["detail"]
    project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(site)}]})).json()
    assert (await _write(client, "PATCH", f"/api/projects/{project['id']}", {"default_env": "host"})).status_code == 400

    monkeypatch.setattr(api_projects, "host_bridge", lambda *_: True)
    assert (await client.get("/api/project-environments", headers=HEADERS)).json()["available"] == ["container", "host"]
    added = await _write(client, "POST", f"/api/projects/{project['id']}/folders", {"path": "/somewhere/on/the/host", "env": "host"})
    assert added.status_code == 200, added.text
    host = added.json()["folders"][1]
    # Stored and shown, but only what runs in a host terminal can ever work in it.
    assert host["env"] == "host" and host["reach"] == "terminals" and host["is_git"] is False
    assert (await _write(client, "PATCH", f"/api/projects/{project['id']}", {"default_env": "host"})).json()["settings"]["default_env"] == "host"

    refused = await client.post("/api/sessions", headers=HEADERS, json={"title": "x", "project_id": project["id"], "folder_id": host["id"]})
    assert refused.status_code == 409 and "host folder" in refused.json()["detail"]


async def test_folders_are_added_locked_and_removed_with_a_journal_of_it(running: Any, tmp_path: Path, db: Database) -> None:
    manager, client = running
    site, docs = _dirs(tmp_path, "site", "docs")
    project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(site)}]})).json()
    pid = project["id"]
    assert (await _write(client, "POST", "/api/projects/nope/folders", {"path": str(docs)})).status_code == 404

    added = await _write(client, "POST", f"/api/projects/{pid}/folders", {"path": str(docs), "label": "Docs"})
    assert added.status_code == 200
    docs_id = added.json()["folders"][1]["id"]
    assert added.json()["folders"][1]["reachable"] is True and added.json()["folders"][1]["label"] == "Docs"

    unmounted = await _write(client, "POST", f"/api/projects/{pid}/folders", {"path": str(tmp_path / "not-mounted")})
    assert unmounted.status_code == 200 and unmounted.json()["folders"][2]["reachable"] is False
    nested = await _write(client, "POST", f"/api/projects/{pid}/folders", {"path": str(docs / "inner")})
    assert nested.status_code == 400 and "inside the project" in nested.json()["detail"]
    relative = await _write(client, "POST", f"/api/projects/{pid}/folders", {"path": "docs"})
    assert relative.status_code == 400 and "absolute" in relative.json()["detail"]

    sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Menu", "project_id": pid})).json()["id"]
    locked = await _write(client, "PATCH", f"/api/projects/{pid}/folders/{docs_id}", {"readonly": True, "label": "Reference"})
    assert locked.status_code == 200
    folder = next(f for f in locked.json()["folders"] if f["id"] == docs_id)
    assert folder["readonly"] is True and folder["writable"] is False and folder["label"] == "Reference"
    live = manager.live_state(sid).project.folder(docs_id)
    assert live is not None and live.readonly is True, "a loaded session must see the lock now, not at its next load"
    walls = manager.live_state(sid).services.walls
    assert docs in walls.readable and docs not in walls.writable, "and its walls with it: readable, never writable"
    assert (await _write(client, "PATCH", f"/api/projects/{pid}/folders/f-nope", {"readonly": True})).status_code == 404

    removed = await _write(client, "DELETE", f"/api/projects/{pid}/folders/{docs_id}")
    assert removed.status_code == 200 and [f["path"] for f in removed.json()["folders"]] == [str(site), str(tmp_path / "not-mounted")]
    assert docs.is_dir(), "forgetting a folder touches nothing on disk"

    journal = (await client.get(f"/api/projects/{pid}/journal", headers=HEADERS)).json()["entries"]
    assert [e["kind"] for e in journal] == ["folder"] * 4 and all(e["author"] == "system" for e in journal)
    assert journal[0]["text"].startswith("removed the folder") and "read-only" in journal[1]["text"]
    assert "folders" in await _events(db, pid)


async def test_a_folder_an_agent_works_in_is_not_taken_from_under_it(running: Any, tmp_path: Path) -> None:
    _, client = running
    site, docs = _dirs(tmp_path, "site", "docs")
    project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(site)}, {"path": str(docs)}]})).json()
    pid = project["id"]
    primary_id, docs_id = (f["id"] for f in project["folders"])

    only = (await client.post("/api/projects", headers=HEADERS, json={"name": "Solo", "folders": [{"path": str(tmp_path / "solo")}]})).json()
    last = await _write(client, "DELETE", f"/api/projects/{only['id']}/folders/{only['folders'][0]['id']}")
    assert last.status_code == 409 and "only folder" in last.json()["detail"]

    in_docs = await client.post("/api/sessions", headers=HEADERS, json={"title": "Writer", "project_id": pid, "folder_id": docs_id})
    assert in_docs.status_code == 200
    detail = (await client.get(f"/api/sessions/{in_docs.json()['id']}", headers=HEADERS)).json()
    assert detail["workspace"] == str(docs)
    busy = await _write(client, "DELETE", f"/api/projects/{pid}/folders/{docs_id}")
    assert busy.status_code == 409 and "Writer works in" in busy.json()["detail"]

    # The primary may be demoted while nobody works there by default; not once somebody does.
    assert (await _write(client, "PATCH", f"/api/projects/{pid}/folders/{docs_id}", {"position": 0})).status_code == 200
    assert (await _write(client, "PATCH", f"/api/projects/{pid}/folders/{primary_id}", {"position": 0})).status_code == 200
    await client.post("/api/sessions", headers=HEADERS, json={"title": "Baker", "project_id": pid})
    moved = await _write(client, "PATCH", f"/api/projects/{pid}/folders/{docs_id}", {"position": 0})
    assert moved.status_code == 409 and "Baker works in the primary folder" in moved.json()["detail"]
    primary = await _write(client, "DELETE", f"/api/projects/{pid}/folders/{primary_id}")
    assert primary.status_code == 409 and "Baker works in" in primary.json()["detail"]
    # A rename or a lock is not a move and is never refused for that reason.
    assert (await _write(client, "PATCH", f"/api/projects/{pid}/folders/{primary_id}", {"label": "Site"})).status_code == 200


async def test_an_agent_is_started_in_a_named_folder_or_refused_clearly(running: Any, tmp_path: Path) -> None:
    _, client = running
    site, docs = _dirs(tmp_path, "site", "docs")
    project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(site)}, {"path": str(tmp_path / "gone")}]})).json()
    pid, gone_id = project["id"], project["folders"][1]["id"]
    unknown = await client.post("/api/sessions", headers=HEADERS, json={"title": "x", "project_id": pid, "folder_id": "f-nope"})
    assert unknown.status_code == 404
    unmounted = await client.post("/api/sessions", headers=HEADERS, json={"title": "x", "project_id": pid, "folder_id": gone_id})
    assert unmounted.status_code == 409 and str(tmp_path / "gone") in unmounted.json()["detail"]
    loose = await client.post("/api/sessions", headers=HEADERS, json={"title": "x", "folder_id": gone_id})
    assert loose.status_code == 400

    added = (await _write(client, "POST", f"/api/projects/{pid}/folders", {"path": str(docs)})).json()
    docs_id = added["folders"][2]["id"]
    sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Writer", "project_id": pid, "folder_id": docs_id})).json()["id"]
    assert (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()["workspace"] == str(docs), "a folder added a moment ago is reachable to a new agent"


async def test_a_chat_project_is_kept_and_its_settings_edited(running: Any, db: Database) -> None:
    _, client = running
    sid = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Plain"})).json()["id"]
    pid = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()["project"]["id"]
    listing = {p["id"]: p for p in (await client.get("/api/projects", headers=HEADERS)).json()}
    assert listing[pid]["settings"]["ephemeral"] is True and listing[pid]["sessions"] == [{"id": sid, "title": "Plain", "running": False}]

    assert (await _write(client, "PATCH", f"/api/projects/{pid}", {"keep": False})).status_code == 422
    kept = await _write(client, "PATCH", f"/api/projects/{pid}", {"keep": True, "name": "Plain project", "snapshots": False, "default_env": "container"})
    assert kept.status_code == 200
    settings = kept.json()["settings"]
    assert settings["ephemeral"] is False and settings["snapshots"] is False and kept.json()["name"] == "Plain project"
    assert "kept" in await _events(db, pid)
    assert (await _write(client, "PATCH", "/api/projects/nope", {"name": "x"})).status_code == 404


async def test_the_brief_is_the_operators_to_write(running: Any, tmp_path: Path) -> None:
    _, client = running
    project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(tmp_path / "site")}]})).json()
    pid = project["id"]
    brief = (await client.get(f"/api/projects/{pid}/brief", headers=HEADERS)).json()["sections"]
    assert [s["section"] for s in brief] == ["goals", "constraints", "preferences", "done_when", "allowed_without_operator", "notes"]
    assert all(s["body"] == "" for s in brief)

    # The section that bounds what may be granted without asking is writable here: the operator is the one writing.
    written = await client.put(f"/api/projects/{pid}/brief", headers=HEADERS, json={"section": "allowed_without_operator", "body": "Run the tests."})
    assert written.status_code == 200 and written.json()["updated_by"] == "operator"
    brief = {s["section"]: s for s in (await client.get(f"/api/projects/{pid}/brief", headers=HEADERS)).json()["sections"]}
    assert brief["allowed_without_operator"]["body"] == "Run the tests."

    assert (await client.put(f"/api/projects/{pid}/brief", headers=HEADERS, json={"section": "wishes", "body": "x"})).status_code == 400
    assert (await client.put(f"/api/projects/{pid}/brief", headers=HEADERS, json={"section": "goals", "body": "x" * (api_projects.BRIEF_SECTION_MAX_CHARS + 1)})).status_code == 422
    assert (await client.get("/api/projects/nope/brief", headers=HEADERS)).status_code == 404
    assert (await client.get(f"/api/projects/{pid}/brief")).status_code == 401


async def test_the_journal_takes_a_note_and_pages_newest_first(running: Any, tmp_path: Path) -> None:
    _, client = running
    pid = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(tmp_path / "site")}]})).json()["id"]
    for text in ("first", "second", "third"):
        note = await client.post(f"/api/projects/{pid}/journal", headers=HEADERS, json={"text": f"  {text}  "})
        assert note.status_code == 200 and note.json()["author"] == "operator" and note.json()["kind"] == "note"
    assert (await client.post(f"/api/projects/{pid}/journal", headers=HEADERS, json={"text": "   "})).status_code == 400
    assert (await client.post(f"/api/projects/{pid}/journal", headers=HEADERS, json={"text": "x" * (api_projects.JOURNAL_NOTE_MAX_CHARS + 1)})).status_code == 422

    page = (await client.get(f"/api/projects/{pid}/journal", headers=HEADERS, params={"limit": 2})).json()
    assert [e["text"] for e in page["entries"]] == ["third", "second"] and page["next_before"] is not None
    rest = (await client.get(f"/api/projects/{pid}/journal", headers=HEADERS, params={"limit": 2, "before": page["next_before"]})).json()
    assert [e["text"] for e in rest["entries"]] == ["first"] and rest["next_before"] is None
    assert (await client.get("/api/projects/nope/journal", headers=HEADERS)).status_code == 404


async def test_the_operator_makes_and_lifts_rules_and_every_page_carries_those_in_force(running: Any, tmp_path: Path) -> None:
    _, client = running
    pid = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(tmp_path / "site")}]})).json()["id"]
    rule = (await client.post(f"/api/projects/{pid}/journal", headers=HEADERS, json={"text": " Hand out work the moment a member is free. ", "kind": "rule"})).json()
    assert (rule["kind"], rule["author"], rule["text"]) == ("rule", "operator", "Hand out work the moment a member is free.")
    for n in range(3):
        await client.post(f"/api/projects/{pid}/journal", headers=HEADERS, json={"text": f"note {n}"})
    page = (await client.get(f"/api/projects/{pid}/journal", headers=HEADERS, params={"limit": 2})).json()
    assert [r["id"] for r in page["rules"]] == [rule["id"]] and rule["id"] not in [e["id"] for e in page["entries"]]

    too_long = await client.post(f"/api/projects/{pid}/journal", headers=HEADERS, json={"text": "x" * 601, "kind": "rule"})
    assert too_long.status_code == 400 and "a rule is at most 600 characters" in too_long.json()["detail"]
    assert (await client.post(f"/api/projects/{pid}/journal", headers=HEADERS, json={"text": "x", "kind": "decision"})).status_code == 422

    lifted = await client.post(f"/api/projects/{pid}/journal/{rule['id']}/lift", headers=HEADERS, json={"why": "the team is small now"})
    assert lifted.status_code == 200 and lifted.json()["kind"] == "rule_lifted" and lifted.json()["refs"] == {"rule_id": rule["id"]}
    page = (await client.get(f"/api/projects/{pid}/journal", headers=HEADERS, params={"limit": 50})).json()
    assert page["rules"] == [] and next(e for e in page["entries"] if e["id"] == rule["id"])["lifted"] is True
    again = await client.post(f"/api/projects/{pid}/journal/{rule['id']}/lift", headers=HEADERS, json={})
    assert again.status_code == 400 and "was lifted already" in again.json()["detail"]
    note_id = page["entries"][0]["id"]
    assert (await client.post(f"/api/projects/{pid}/journal/{note_id}/lift", headers=HEADERS, json={})).status_code == 400


def test_who_can_reach_a_folder(tmp_path: Path) -> None:
    def folder(env: str) -> ProjectFolder:
        return ProjectFolder(id="f", project_id="p", path=tmp_path, label="", env=env, is_git=False, readonly=False, position=0, created_at=datetime.now(UTC))

    assert api_projects.reach(folder("container"), "container", False) == "agents"
    assert api_projects.reach(folder("host"), "container", True) == "terminals"
    assert api_projects.reach(folder("host"), "container", False) == "none"
    assert api_projects.reach(folder("host"), "host", False) == "agents"
    assert api_projects.environments(SimpleNamespace(), "host") == {"local": "host", "available": ["host"], "host_bridge": False, "docker": False}
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    assert api_projects.host_bridge(SimpleNamespace(terminals_host_dir=bridge)) is False, "an empty run directory is a bridge not installed"
    (bridge / "endpoint").write_text("unix:ptyd.sock")
    assert api_projects.host_bridge(SimpleNamespace(terminals_host_dir=bridge)) is True
