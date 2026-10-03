"""Cancellation ownership is explicit, revision-pinned and transactionally frozen."""

from __future__ import annotations

import pytest

from daedalus.stores.control import ControlStore, Principal
from daedalus.stores.database import Database
from daedalus.stores.lifecycle import LifecycleRefused, admit_child


async def test_goal_owned_task_requires_exact_current_revision_and_stops_late_admission(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
        for task_id in ("task-a", "task-b"):
            await db.execute(
                "INSERT INTO board_tasks(id,title,status,project_id,created_at,updated_at) "
                "VALUES (?,?,'todo','project','now','now')", (task_id, task_id),
            )
        operator = Principal.operator({"via": "token", "user_id": 1})
        control = ControlStore(db)
        async with db.transaction() as conn:
            owned = await admit_child(
                conn, control=control, principal=operator, parent_kind="project_goal", parent_id="project",
                project_id="project", goal_revision=1, child_kind="task", child_id="task-a",
            )
        assert owned["generation"] == 1 and owned["source_revision"] == 1
        async with db.transaction() as conn:
            assert await admit_child(
                conn, control=control, principal=operator, parent_kind="project_goal", parent_id="project",
                project_id="project", goal_revision=1, child_kind="task", child_id="task-a",
            )
        await db.execute(
            "UPDATE lifecycle_parents SET cancel_state = 'requested' WHERE parent_kind = 'project_goal' AND parent_id = 'project'"
        )
        with pytest.raises(LifecycleRefused, match="cancelled"):
            async with db.transaction() as conn:
                await admit_child(
                    conn, control=control, principal=operator, parent_kind="project_goal", parent_id="project",
                    project_id="project", goal_revision=1, child_kind="task", child_id="task-b",
                )
        await db.execute("UPDATE projects SET goal_revision = 2 WHERE id = 'project'")
        async with db.transaction() as conn:
            next_owned = await admit_child(
                conn, control=control, principal=operator, parent_kind="project_goal", parent_id="project",
                project_id="project", goal_revision=2, child_kind="task", child_id="task-b",
            )
        assert next_owned["generation"] == 2 and next_owned["source_revision"] == 2
        previous = await db.fetchone("SELECT generation,source_revision FROM lifecycle_owners WHERE child_id = 'task-a'")
        assert (previous["generation"], previous["source_revision"]) == (1, 1)
    finally:
        await db.close()


async def test_task_parent_cannot_claim_another_projects_child(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        for project in ("a", "b"):
            await db.execute("INSERT INTO projects(id,name,created_at) VALUES (?,?, 'now')", (project, project))
            await db.execute(
                "INSERT INTO board_tasks(id,title,status,project_id,created_at,updated_at) "
                "VALUES (?,?, 'todo',?, 'now','now')", (f"task-{project}", project, project),
            )
        with pytest.raises(LifecycleRefused, match="outside"):
            async with db.transaction() as conn:
                await admit_child(
                    conn, control=ControlStore(db), principal=Principal.operator({"via": "token", "user_id": 1}),
                    parent_kind="task", parent_id="task-a", project_id="a", child_kind="task", child_id="task-b",
                )
        assert not await db.fetchall("SELECT * FROM lifecycle_owners")
    finally:
        await db.close()
