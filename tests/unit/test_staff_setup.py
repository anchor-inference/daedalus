"""A project's setup command in a new staff worktree, and the services a worker's session leaves behind."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from daedalus.config import Settings
from daedalus.extensions.services import Services, pid_alive
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.projects import Project
from tests.support.authorized_launch import operator_assignment
from tests.support.waiting import until_await
from tests.unit.test_staff_runtime import board_task, close_team, fake_team, status_of, task_row


async def set_setup(manager: SessionManager, project: Project, command: str) -> None:
    await manager.db.execute("UPDATE projects SET settings = json_set(settings, '$.setup_command', ?) WHERE id = ?",
                             (command, project.id))
    await manager.projects.list()


async def effect_failed(manager: SessionManager, effect_id: str) -> dict[str, Any]:
    """The command's end: ``failed``, not ``unknown``, because the setup ran before the worker had a
    session, and an uncertain launch would keep the card from being assigned again."""
    seen: dict[str, Any] = {}

    async def settled() -> bool:
        row = await manager.db.fetchone("SELECT state,error FROM effect_outbox WHERE id = ?", (effect_id,))
        if row is not None and row["state"] in ("failed", "unknown"):
            seen.update(dict(row))
            return True
        return False

    await until_await(settled, "the launch was refused")
    assert seen["state"] == "failed"
    return seen


async def test_the_setup_command_runs_once_in_a_new_worktree_and_not_on_reuse(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    log = tmp_path / "setup.log"
    try:
        # Written outside the worktree: a file inside it would make it dirty and refuse the reuse.
        await set_setup(manager, project, f"pwd >> {shlex.quote(str(log))}")
        ada = await manager.staff.hire(project.id, name="Ada", isolation="worktree")
        await operator_assignment(team, ada, await board_task(manager, project, "Menu"))
        [first] = runtime.started
        assert first.worktree is not None and first.worktree.created
        assert log.read_text().splitlines() == [str(first.worktree.cwd)]
        saved = await manager.db.kv_get(f"worktree_setup:{first.worktree.env}:{first.worktree.path}")
        assert saved["ok"] is True and saved["exit_code"] == 0

        assert await team.release(ada)
        await operator_assignment(team, ada, await board_task(manager, project, "Prices"))
        second = runtime.started[-1]
        assert second.worktree is not None and second.worktree.path == first.worktree.path and not second.worktree.created
        assert log.read_text().splitlines() == [str(first.worktree.cwd)], "a reused worktree is not set up again"
    finally:
        await close_team(manager)
        await manager.close()


async def test_a_failed_setup_keeps_the_task_unstarted_with_the_reason_and_is_retried(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        await set_setup(manager, project, "echo resolving; echo 'no matching version for left-pad' >&2; exit 3")
        ada = await manager.staff.hire(project.id, name="Ada", isolation="worktree")
        task_id = await board_task(manager, project, "Menu")
        receipt = await operator_assignment(team, ada, task_id, wait_for_admission=False)
        outcome = await effect_failed(manager, receipt["effect_id"])
        assert "exited with 3" in outcome["error"]

        assert runtime.started == [] and await status_of(manager, ada) == "off"
        row = await task_row(manager, task_id)
        assert (row["status"], row["assignee_staff_id"]) == ("todo", None)
        assert "Ada could not start: the setup command" in row["notes"] and "exited with 3" in row["notes"]
        assert "no matching version for left-pad" in row["notes"]

        # The worktree stays; the next launch sets it up again rather than trusting a half-done install.
        marker = tmp_path / "fixed"
        await set_setup(manager, project, f"touch {shlex.quote(str(marker))}")
        await operator_assignment(team, ada, task_id)
        [started] = runtime.started
        assert started.worktree is not None and not started.worktree.created and marker.exists()
    finally:
        await close_team(manager)
        await manager.close()


async def test_an_empty_setup_command_runs_nothing(settings: Settings, db: Database, tmp_path: Path) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        assert project.settings.setup_command == ""
        ada = await manager.staff.hire(project.id, name="Ada", isolation="worktree")
        await operator_assignment(team, ada, await board_task(manager, project, "Menu"))
        [started] = runtime.started
        assert started.worktree is not None and started.worktree.created
        assert not await manager.db.fetchall("SELECT key FROM kv WHERE key LIKE 'worktree_setup:%'")
    finally:
        await close_team(manager)
        await manager.close()


async def test_releasing_a_worker_stops_the_services_its_session_started(settings: Settings, db: Database, tmp_path: Path) -> None:
    settings.services_port_range = "18140-18143"
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        app: Any = team.app
        app.config = manager.config
        services = Services(app)
        app.extensions["services"] = services
        ada = await manager.staff.hire(project.id, name="Ada", isolation="worktree")
        await operator_assignment(team, ada, await board_task(manager, project, "Menu"))
        live = await team.live_of(ada)
        assert live is not None and live.session.session_id
        sid = live.session.session_id
        server = await services.start(sid, name="preview", command="sleep 60", port="none")
        other = await manager.create_session("Someone else's chat")
        theirs = await services.start(other.session.id, name="theirs", command="sleep 60", port="none")
        assert pid_alive(server["pid"]) and pid_alive(theirs["pid"])

        assert await team.release(ada, reason="released by the operator")

        assert not pid_alive(server["pid"])
        row = await services.get(sid, "preview")
        assert row is not None and row["status"] == "stopped" and row["restart"] == 0
        assert row["note"] == "its worker ended: released by the operator"
        assert pid_alive(theirs["pid"]), "only the worker's own session's services stop"
        await services.stop_all(other.session.id)
    finally:
        await close_team(manager)
        await manager.close()


def test_the_setup_command_round_trips_through_the_settings() -> None:
    from daedalus.stores.projects import ProjectSettings

    loaded = ProjectSettings.load(json.loads(json.dumps(ProjectSettings(setup_command="uv sync --frozen").dump())))
    assert loaded.setup_command == "uv sync --frozen"
    assert ProjectSettings.load({}).setup_command == ""
