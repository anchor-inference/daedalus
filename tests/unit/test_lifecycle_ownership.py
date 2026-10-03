"""Cancellation ownership is explicit, revision-pinned and transactionally frozen."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from daedalus.extensions.lifecycle import Lifecycle
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
        await db.execute(
            "UPDATE lifecycle_owners SET cancel_state = 'drained' WHERE child_kind = 'task' AND child_id = 'task-a'"
        )
        await db.execute(
            "UPDATE lifecycle_parents SET cancel_state = 'drained' WHERE parent_kind = 'project_goal' AND parent_id = 'project'"
        )
        await db.execute("UPDATE projects SET goal_revision = 2 WHERE id = 'project'")
        async with db.transaction() as conn:
            next_owned = await admit_child(
                conn, control=control, principal=operator, parent_kind="project_goal", parent_id="project",
                project_id="project", goal_revision=2, child_kind="task", child_id="task-b",
            )
        assert next_owned["generation"] == 2 and next_owned["source_revision"] == 2
        async with db.transaction() as conn:
            carried = await admit_child(
                conn, control=control, principal=operator, parent_kind="project_goal", parent_id="project",
                project_id="project", goal_revision=2, child_kind="task", child_id="task-a",
            )
        assert carried["generation"] == 2
        history = await db.fetchall(
            "SELECT generation,source_revision,cancel_state FROM lifecycle_owners "
            "WHERE child_id = 'task-a' ORDER BY generation"
        )
        assert [(row["generation"], row["source_revision"], row["cancel_state"]) for row in history] == [
            (1, 1, "drained"), (2, 2, "active"),
        ]
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


async def test_new_goal_revision_transfers_current_task_without_losing_history(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
        await db.execute(
            "INSERT INTO board_tasks(id,title,status,project_id,created_at,updated_at) "
            "VALUES ('root','Root','todo','project','now','now')"
        )
        operator = Principal.operator({"via": "token", "user_id": 1})
        control = ControlStore(db)
        async with db.transaction() as conn:
            await admit_child(
                conn, control=control, principal=operator, parent_kind="project_goal", parent_id="project",
                project_id="project", goal_revision=1, child_kind="task", child_id="root",
            )
        await db.execute("UPDATE projects SET goal_revision = 2 WHERE id = 'project'")
        async with db.transaction() as conn:
            current = await admit_child(
                conn, control=control, principal=operator, parent_kind="project_goal", parent_id="project",
                project_id="project", goal_revision=2, child_kind="task", child_id="root",
            )
        assert current["generation"] == 2
        rows = await db.fetchall(
            "SELECT generation,cancel_state FROM lifecycle_owners WHERE child_id = 'root' ORDER BY generation"
        )
        assert [(row["generation"], row["cancel_state"]) for row in rows] == [
            (1, "transferred"), (2, "active"),
        ]
    finally:
        await db.close()


async def test_cancellation_freezes_only_explicit_goal_children_and_replays_receipt(tmp_path) -> None:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id,name,created_at) VALUES ('project','Project','now')")
        for task_id in ("owned", "independent"):
            await db.execute(
                "INSERT INTO board_tasks(id,title,status,project_id,created_at,updated_at) "
                "VALUES (?,?, 'todo','project','now','now')", (task_id, task_id),
            )
        operator = Principal.operator({"via": "token", "user_id": 1})
        control = ControlStore(db)
        async with db.transaction() as conn:
            await admit_child(
                conn, control=control, principal=operator, parent_kind="project_goal", parent_id="project",
                project_id="project", goal_revision=1, child_kind="task", child_id="owned",
            )
        notified = []
        app = SimpleNamespace(db=db, extensions={"effects": SimpleNamespace(notify=lambda: notified.append(True))})
        service = Lifecycle(app)
        preview = await service.preview("project_goal", "project")
        first = await service.cancel_command(
            operator, "project_goal", "project", "operator stop", expected_entity_revision=1,
            expected_source_revision=1, client_operation_id="goal-stop",
            preview_fingerprint=preview["preview_fingerprint"],
        )
        assert first["cancel_state"] == "requested" and first["children"] == [
            {"kind": "task", "id": "owned", "status": "draining"},
        ]
        assert first == await service.cancel_command(
            operator, "project_goal", "project", "operator stop", expected_entity_revision=1,
            expected_source_revision=1, client_operation_id="goal-stop",
            preview_fingerprint=preview["preview_fingerprint"],
        )
        assert len(await db.fetchall("SELECT id FROM effect_outbox WHERE kind = 'lifecycle.stop'")) == 1
        assert len(notified) == 2
        assert (await db.fetchone(
            "SELECT cancel_state FROM lifecycle_parents WHERE parent_kind = 'task' AND parent_id = 'owned'"
        ))["cancel_state"] == "requested"
        assert not await db.fetchone("SELECT 1 FROM lifecycle_parents WHERE parent_id = 'independent'")
        assert (await db.fetchone("SELECT status FROM board_tasks WHERE id = 'independent'"))["status"] == "todo"
    finally:
        await db.close()
