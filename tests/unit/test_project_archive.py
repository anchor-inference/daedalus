"""Archiving a project: a settings flag the operator sets and clears, and the wake-ups it holds back.

Nothing the project owns is deleted or switched off; what changes is that its coordinator is not
woken by an event, a schedule or a watch while the flag is set, and that restoring brings it back
without a backlog.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from daedalus.config import Settings
from daedalus.stores.control import Principal
from daedalus.stores.database import Database
from tests.unit.test_orchestrator import rig
from tests.unit.test_project_commands import _revision, running  # noqa: F401 — the fixture is used by name
from tests.unit.test_project_folders_api import HEADERS
from tests.unit.test_recurring import system  # noqa: F401 — the fixture is used by name
from tests.unit.test_staff_runtime import close_team
from tests.unit.test_watches import fired, with_watches


async def _set_archived(db: Database, project_id: str, archived: bool) -> None:
    await db.execute("UPDATE projects SET settings = json_set(settings, '$.archived', json(?)) WHERE id = ?",
                     ("true" if archived else "false", project_id))


async def test_archive_and_restore_through_the_settings_write(running: Any, tmp_path: Path, db: Database) -> None:  # noqa: F811
    manager, client = running
    folder = tmp_path / "site"
    folder.mkdir()
    pid = (await client.post("/api/projects", headers=HEADERS, json={"name": "Bakery", "folders": [{"path": str(folder)}]})).json()["id"]
    path = f"/api/projects/{pid}"

    archived = await client.patch(path, headers=HEADERS, json={"archived": True, "client_operation_id": "archive-1",
                                                               "expected_entity_revision": await _revision(client, pid)})
    assert archived.status_code == 200, archived.text
    listed = next(p for p in (await client.get("/api/projects", headers=HEADERS)).json() if p["id"] == pid)
    # Still listed, with the flag: the app hides it and draws it under the archived fold.
    assert listed["settings"]["archived"] is True and listed["folders"][0]["path"] == str(folder)
    rows = await db.fetchall("SELECT payload_json FROM app_events WHERE type = 'project.changed' AND project_id = ?", (pid,))
    assert "archived" in [json.loads(r["payload_json"]).get("change") for r in rows]
    assert any(e.text == "archived the project" for e in await manager.projects.journal(pid))

    restored = await client.patch(path, headers=HEADERS, json={"archived": False, "client_operation_id": "restore-1",
                                                               "expected_entity_revision": await _revision(client, pid)})
    assert restored.status_code == 200
    assert (await manager.projects.get(pid)).settings.archived is False  # type: ignore[union-attr]
    assert any(e.text == "restored the project from the archive" for e in await manager.projects.journal(pid))

    # The installation's own project is not the operator's to put away.
    voice = await manager.projects.ensure_system("voice", name="Voice", root=tmp_path / "voice")
    refused = await client.patch(f"/api/projects/{voice.id}", headers=HEADERS, json={
        "archived": True, "client_operation_id": "archive-voice", "expected_entity_revision": await _revision(client, voice.id)})
    assert refused.status_code == 400 and "can be archived" in refused.json()["detail"]


async def test_an_archived_project_does_not_wake_its_coordinator(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        await r.orch.enable(r.project.id)
        pid = r.project.id

        async def wake() -> Any:
            event = await r.manager.bus.publish("schedule.fired", {"schedule_id": "w1", "name": "check", "kind": "wake"}, project_id=pid)
            return await r.orch.classify(pid, event)

        assert (await wake()).urgent
        await _set_archived(db, pid, True)
        assert await wake() is None
        await _set_archived(db, pid, False)
        assert (await wake()).urgent
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_an_archived_projects_watch_holds_its_fire(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        ada = await r.manager.staff.hire(r.project.id, name="Ada")
        made = await keeper.create(project, when={"event": "staff_finished", "staff": "Ada"}, then={"action": "wake"}, cooldown_minutes=5)

        async def finish() -> None:
            await keeper.on_event(await r.manager.bus.publish("staff.status", {"status": "turn_done_unseen", "previous": "working"}, project_id=r.project.id, staff_id=ada.id))

        await _set_archived(db, r.project.id, True)
        await finish()
        assert await fired(r) == []
        # Still switched on: restoring the project needs nothing switched back.
        assert keeper.get(r.project.id, made.id).enabled  # type: ignore[union-attr]
        await _set_archived(db, r.project.id, False)
        await finish()
        assert [e.payload["watch_id"] for e in await fired(r)] == [made.id]
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


async def test_an_archived_projects_schedule_is_skipped_not_held(system: Any) -> None:  # noqa: F811
    recurring = system.extensions["recurring"]

    async def schedule_in(name: str) -> tuple[str, str]:
        project = await system.manager.projects.create(name)
        rev = await system.db.fetchone("SELECT revision FROM domain_collection_revisions WHERE scope_kind='project' AND scope_id=?", (project.id,))
        created = await recurring.create(
            Principal("operator:1", "operator"), name=f"{name} reminder", prompt="inspect work",
            cron=None, run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(), kind="message", target_session=None,
            project_id=project.id, expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat(),
            expected_collection_revision=rev["revision"], client_operation_id=f"{name}-reminder",
        )
        return project.id, created["id"]

    shelved, shelved_schedule = await schedule_in("Shelved")
    _, live_schedule = await schedule_in("Live")
    await _set_archived(system.db, shelved, True)

    await system.extensions["scheduler"].tick()

    skipped = await system.db.fetchone("SELECT action_state,effect_id FROM recurring_cycles WHERE schedule_id = ?", (shelved_schedule,))
    assert skipped["action_state"] == "skipped" and skipped["effect_id"] is None
    row = await system.db.fetchone("SELECT enabled,next_run_at,failure_count FROM schedules WHERE id = ?", (shelved_schedule,))
    assert (row["enabled"], row["next_run_at"], row["failure_count"]) == (0, None, 0)
    fired_cycle = await system.db.fetchone("SELECT action_state,effect_id FROM recurring_cycles WHERE schedule_id = ?", (live_schedule,))
    assert fired_cycle["effect_id"] is not None and fired_cycle["action_state"] != "skipped"
