"""The team over HTTP: hire, list, edit, dismiss, and the refusals the app shows as they are."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.host.events import EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database

HEADERS = {"X-Daedalus-Token": "tok"}


def _client(settings: Settings, config: RuntimeConfig, db: Database, manager: SessionManager, extensions: dict[str, Any] | None = None) -> httpx.AsyncClient:
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager, front=None, extensions=extensions or {}, guard=None)
    api = build_app(app, "tok")  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test")  # type: ignore[arg-type]


async def test_hire_list_edit_and_dismiss(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    repo = tmp_path / "bakery"
    (repo / ".git").mkdir(parents=True)
    try:
        async with _client(settings, config, db, manager) as client:
            project = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(repo)}]})).json()
            pid = project["id"]
            assert (await client.get(f"/api/projects/{pid}/staff")).status_code == 401

            empty = (await client.get(f"/api/projects/{pid}/staff", headers=HEADERS)).json()
            assert empty["staff"] == [] and empty["counts"] == {"staff": 0, "working": 0}
            assert empty["project"]["concurrency"] == 1 and empty["project"]["local_env"] == "container"
            assert empty["project"]["folders"][0]["is_git"] is True
            assert empty["choices"]["harnesses"] == ["daedalus", "claude", "codex", "grok", "opencode", "pi", "cursor"]
            assert [p["id"] for p in empty["choices"]["presets"]] == list(config.presets)
            assert empty["choices"]["personas"] == manager.staff.personas()

            async with manager.bus.subscribe(EventFilter(types=("project.changed",), project_id=pid), name="test") as events:
                hired = await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Ada", "role": "Menu page"})
                assert hired.status_code == 201, hired.text
                ada = hired.json()
                # A git folder gets an own worktree by default.
                assert (ada["name"], ada["isolation"], ada["harness"], ada["status"], ada["live"], ada["sessions"]) == ("Ada", "worktree", "daedalus", "off", None, 0)
                event = await anext(events)
                assert (event.type, event.staff_id, event.payload) == ("project.changed", ada["id"], {"change": "staff.hired", "actor": "operator"})

            taken = await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "ada"})
            assert taken.status_code == 400 and "already has someone called" in taken.json()["detail"]
            bad = await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Bo", "model": "nothing-like-it"})
            assert bad.status_code == 400 and "not a model preset" in bad.json()["detail"]
            unknown = await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Bo", "surprise": 1})
            assert unknown.status_code == 422
            assert (await client.post("/api/projects/nope/staff", headers=HEADERS, json={"name": "Bo"})).status_code == 404
            cc = (await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Cleo", "harness": "claude", "model": "opus", "permission_mode": "acceptEdits", "isolation": "shared"})).json()
            assert (cc["harness"], cc["permission_mode"], cc["isolation"]) == ("claude", "acceptEdits", "shared")
            # How each setting is kept is on the member's details before any session exists.
            view = (await client.get(f"/api/staff/{cc['id']}/session", headers=HEADERS)).json()
            assert view["session"] is None
            assert {line["setting"]: line["kept"] for line in view["kept"]} == {"folder": "prompt", "asking": "cli", "network": "none", "scope": "prompt"}
            native = (await client.get(f"/api/staff/{ada['id']}/session", headers=HEADERS)).json()
            assert next(line for line in native["kept"] if line["setting"] == "folder")["kept"] == "host"

            listing = (await client.get(f"/api/projects/{pid}/staff", headers=HEADERS)).json()
            assert [m["name"] for m in listing["staff"]] == ["Ada", "Cleo"] and listing["counts"]["staff"] == 2

            edited = await client.patch(f"/api/staff/{ada['id']}", headers=HEADERS, json={"role": "Tests", "isolation": "shared", "color": "teal"})
            assert edited.status_code == 200 and (edited.json()["role"], edited.json()["isolation"], edited.json()["color"]) == ("Tests", "shared", "teal")
            assert (await client.patch(f"/api/staff/{ada['id']}", headers=HEADERS, json={"name": "Eve"})).status_code == 422
            assert (await client.patch(f"/api/staff/{ada['id']}", headers=HEADERS, json={"folder_id": "f-nope"})).status_code == 400
            assert (await client.patch(f"/api/staff/{ada['id']}", headers=HEADERS, json={"folder_id": ""})).json()["default_folder_id"] is None

            # A live session makes dismissal a conflict, with or without release while nothing can end it.
            live = await manager.staff.claim_session(ada["id"], kind="daedalus")
            await manager.staff.set_status(live.id, "working")
            working = (await client.get(f"/api/projects/{pid}/staff", headers=HEADERS)).json()
            assert working["counts"]["working"] == 1
            assert next(m for m in working["staff"] if m["id"] == ada["id"])["status"] == "working"
            one = (await client.get(f"/api/staff/{ada['id']}", headers=HEADERS)).json()
            assert one["live"]["id"] == live.id and one["sessions"] == 1
            busy = await client.delete(f"/api/staff/{ada['id']}", headers=HEADERS)
            assert busy.status_code == 409 and "is working" in busy.json()["detail"]
            assert (await client.delete(f"/api/staff/{ada['id']}?release=1", headers=HEADERS)).status_code == 409

            sessions = (await client.get(f"/api/staff/{ada['id']}/sessions", headers=HEADERS)).json()
            assert [s["id"] for s in sessions] == [live.id] and "team_token_hash" not in sessions[0]

            await manager.staff.end_session(live.id, "done")
            gone = await client.delete(f"/api/staff/{ada['id']}", headers=HEADERS)
            assert gone.status_code == 200 and gone.json()["staff"]["archived_at"]
            after = (await client.get(f"/api/projects/{pid}/staff", headers=HEADERS)).json()
            assert [m["name"] for m in after["staff"]] == ["Cleo"]
            everyone = (await client.get(f"/api/projects/{pid}/staff?archived=1", headers=HEADERS)).json()
            assert {m["name"] for m in everyone["staff"]} == {"Ada", "Cleo"}
            assert (await client.get("/api/staff/st-nope", headers=HEADERS)).status_code == 404
    finally:
        await manager.close()


async def test_release_asks_the_staff_runtime_when_there_is_one(settings: Settings, config: RuntimeConfig, db: Database, tmp_path: Path) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    released: list[str] = []

    class Runtime:
        async def release(self, member: Any, *, keep_worktree: bool) -> None:
            released.append(member.name)
            live = await manager.staff.live(member.id)
            assert live is not None
            await manager.staff.end_session(live.id, "released")

    try:
        async with _client(settings, config, db, manager, {"staff": Runtime()}) as client:
            pid = (await client.post("/api/projects", headers=HEADERS, json={"name": "Plain"})).json()["id"]
            # A folder that is not a repository gets the shared folder by default.
            ada = (await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Ada"})).json()
            assert ada["isolation"] == "shared"
            await manager.staff.claim_session(ada["id"], kind="daedalus")
            gone = await client.delete(f"/api/staff/{ada['id']}?release=1", headers=HEADERS)
            assert gone.status_code == 200 and released == ["Ada"]
    finally:
        await manager.close()


async def test_a_chats_own_project_has_no_team(settings: Settings, config: RuntimeConfig, db: Database) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    try:
        async with _client(settings, config, db, manager) as client:
            session = (await client.post("/api/sessions", headers=HEADERS, json={"title": "Chat"})).json()
            pid = (await client.get(f"/api/sessions/{session['id']}", headers=HEADERS)).json()["project"]["id"]
            refused = await client.post(f"/api/projects/{pid}/staff", headers=HEADERS, json={"name": "Ada"})
            assert refused.status_code == 400 and "chat's own project" in refused.json()["detail"]
    finally:
        await manager.close()
