"""The member route through the orchestrator's own Hire and Assign, down to a started worker."""

from __future__ import annotations

import json
from pathlib import Path

from daedalus.config import Settings
from daedalus.staff_runtime import Availability
from daedalus.stores.database import Database
from tests.unit.test_orchestrator import rig
from tests.unit.test_orchestrator_team import admitted, fake, office
from tests.unit.test_staff_runtime import BRIEF, ObservedFakeStaffRuntime, close_team


async def test_an_unsigned_cli_falls_back_to_daedalus_and_the_orchestrator_is_told(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        native = fake(r)
        claude = ObservedFakeStaffRuntime(kind="claude", availability=Availability(False, "Claude Code is not signed in in the container environment"))
        claude.manager = r.manager
        r.team.runtimes["claude"] = claude
        sid = await office(r)
        await r.manager.db.execute(
            "UPDATE projects SET settings = json_set(settings,'$.member_route',json(?)) WHERE id = ?",
            ('[{"runtime": "claude"}, {"runtime": "native"}]', r.project.id))
        await r.manager.projects.list()
        member = await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", harness="claude", isolation="shared")
        said = await r.call(sid, "assign", staff=member.id, title="Menu page", **BRIEF)
        assert "Launch route: Claude Code: Claude Code is not signed in in the container environment → Daedalus accepted." in said
        task_id = (await r.manager.db.fetchone("SELECT id FROM board_tasks WHERE title = 'Menu page'"))["id"]
        assert await admitted(r, task_id)
        assert claude.started == [] and len(native.started) == 1
        moved = await r.manager.staff.get(member.id)
        assert moved is not None and moved.harness == "daedalus" and moved.route_home is not None
        journal = await r.manager.db.fetchone("SELECT text FROM project_journal WHERE kind = 'launch_route'")
        assert journal is not None and "Daedalus accepted" in journal["text"]
        notes = (await r.manager.db.fetchone("SELECT notes FROM board_tasks WHERE id = ?", (task_id,)))["notes"]
        assert "Launch route:" in notes
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_hire_without_a_runtime_takes_the_route_first(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        sid = await office(r)
        await r.manager.db.execute(
            "UPDATE projects SET settings = json_set(settings,'$.member_route',json(?)) WHERE id = ?",
            (json.dumps([{"runtime": "cursor"}, {"runtime": "daedalus", "model": next(iter(r.manager.config.presets))}]), r.project.id))
        await r.manager.projects.list()
        said = await r.call(sid, "hire", name="Bo", role="Docs")
        assert "hired Bo" in said
        member = next(m for m in await r.manager.staff.list(r.project.id) if m.name == "Bo")
        # Cursor has no runtime installed here, so the first installed runtime of the route is taken.
        assert member.harness == "daedalus" and member.model == next(iter(r.manager.config.presets))
    finally:
        await close_team(r.manager)
        await r.manager.close()
