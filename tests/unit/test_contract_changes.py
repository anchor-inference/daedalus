"""A semantic change reaches a new attempt only after its predecessor has exited."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.config import Settings
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.runtime_observations import admit_native_run, observe_exit
from daedalus.extensions.task_controls import TaskStopEffect
from daedalus.stores.control import ControlStore, Entity, Scope
from daedalus.stores.database import Database
from tests.support.authorized_launch import operator_task
from tests.support.authorized_stop import bind_native_run, finish_native_stop
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import rig
from tests.unit.test_orchestrator_team import admitted, fake, office
from tests.unit.test_staff_runtime import close_team
from tests.unit.test_task_contract import SCRIPT, live, member


async def test_idle_requirement_is_staged_applied_once_and_sent_on_next_launch(
    settings: Settings, db: Database, tmp_path: Path,
) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        sid = await office(r)
        await member(r)
        task_id = await operator_task(db, r.project.id, "Script", brief=SCRIPT)
        revision = await ControlStore(db).revision(Scope("project", r.project.id), Entity("task", task_id))
        await db.execute("INSERT INTO comparison_groups(id,task_id,contract_revision,max_attempts,created_at,state)"
                         " VALUES (?,?,?,?,?,'planned')",
                         (uuid.uuid4().hex, task_id, 1, 2, datetime.now(UTC).isoformat()))
        with pytest.raises(Refused, match="active comparison"):
            await r.call(sid, "require", task_id=task_id, text="Use English", kind="quality", source="operator",
                         client_operation_id="script-language:comparison", expected_entity_revision=revision)
        await db.execute("DELETE FROM comparison_groups WHERE task_id = ?", (task_id,))
        stage = json.loads(await r.call(sid, "require", task_id=task_id,
                                        text="Use English", kind="quality", source="operator",
                                        client_operation_id="script-language", expected_entity_revision=revision))
        assert stage["state"] == "ready_to_apply"
        assert (await db.fetchone("SELECT COUNT(*) FROM task_requirements WHERE task_id = ?", (task_id,)))[0] == 0
        applied = json.loads(await r.call(sid, "require", op="apply", task_id=task_id,
                                          intent_id=stage["intent_id"], client_operation_id="script-language:apply",
                                          expected_entity_revision=stage["entity_revision"]))
        assert applied["state"] == "applied" and applied["contract_revision"] == 2
        replay = json.loads(await r.call(sid, "require", op="apply", task_id=task_id,
                                         intent_id=stage["intent_id"], client_operation_id="script-language:apply",
                                         expected_entity_revision=stage["entity_revision"]))
        assert replay == applied
        await r.call(sid, "assign", task_id=task_id, staff="Mira")
        await until_await(lambda: admitted(r, task_id), "the revised task launched")
        assert "Use English" in runtime.started[-1].first_message
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_active_requirement_waits_for_exact_exit_then_new_attempt(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = fake(r)
        r.team.app.extensions["effects"].register("task.stop", TaskStopEffect(r.team.app))
        sid = await office(r)
        worker = await member(r)
        task_id = await operator_task(db, r.project.id, "Script", brief=SCRIPT)
        await r.call(sid, "assign", task_id=task_id, staff="Mira")
        await until_await(lambda: admitted(r, task_id), "the original worker launched")
        previous = await live(r, worker)
        source = await db.fetchone("SELECT tenant_id FROM sessions WHERE id = ?", (previous.session.session_id,))
        assert source is not None
        run_id = uuid.uuid4().hex
        at = datetime.now(UTC).isoformat()
        await db.execute("INSERT INTO runs(id,tenant_id,session_id,status,created_at,updated_at)"
                         " VALUES (?,?,?,'running',?,?)",
                         (run_id, source["tenant_id"], previous.session.session_id, at, at))
        await admit_native_run(r.team.app, previous.id, previous.session.session_id, run_id)
        state = SimpleNamespace(run_id=run_id, running=True)
        monkeypatch.setattr(r.manager, "live_state", lambda session_id: state if session_id == previous.session.session_id else None)
        stops: list[tuple[str, str]] = []

        async def stop_run(session_id: str, exact_run_id: str) -> bool:
            stops.append((session_id, exact_run_id))
            return True

        monkeypatch.setattr(r.manager, "stop_run", stop_run)
        revision = await ControlStore(db).revision(Scope("project", r.project.id), Entity("task", task_id))
        stage = json.loads(await r.call(sid, "require", task_id=task_id,
                                        text="Use English", kind="quality", source="operator",
                                        client_operation_id="active-language", expected_entity_revision=revision))
        assert stage["state"] == "pending_physical_exit"
        assert runtime.sent == []
        assert (await db.fetchone("SELECT COUNT(*) FROM task_requirements WHERE task_id = ?", (task_id,)))[0] == 0
        current = await ControlStore(db).revision(Scope("project", r.project.id), Entity("task", task_id))
        with pytest.raises(ValueError, match="physical exit|stopped|reconciled"):
            await r.call(sid, "require", op="apply", task_id=task_id, intent_id=stage["intent_id"],
                         client_operation_id="active-language:early", expected_entity_revision=current)
        steps = []
        for _ in range(5):
            steps.append(await r.team.app.extensions["effects"].step())
            action = await db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (stage["stop_effect_id"],))
            if action is not None and action["state"] != "pending":
                break
        action = await db.fetchone("SELECT state,error FROM effect_outbox WHERE id = ?", (stage["stop_effect_id"],))
        all_actions = await db.fetchall("SELECT kind,state,error FROM effect_outbox ORDER BY created_at")
        assert action is not None and action["state"] == "unknown", {
            "actions": [dict(item) for item in all_actions], "steps": steps,
            "handlers": list(r.team.app.extensions["effects"].handlers),
            "postponed": list(r.team.app.extensions["effects"].postponed),
        }
        async def stopped() -> bool:
            return bool(stops)

        await until_await(stopped, "the exact run received a stop request")
        assert stops == [(previous.session.session_id, run_id)]
        await db.execute("UPDATE runs SET status = 'cancelled' WHERE id = ?", (run_id,))
        assert await observe_exit(r.team.app, staff_session_id=previous.id, runtime_ref=run_id,
                                  observed_status="cancelled")
        await r.team.app.extensions["effects"].reconcile()
        current = await ControlStore(db).revision(Scope("project", r.project.id), Entity("task", task_id))
        applied = json.loads(await r.call(sid, "require", op="apply", task_id=task_id,
                                          intent_id=stage["intent_id"], client_operation_id="active-language:apply",
                                          expected_entity_revision=current))
        assert applied["contract_revision"] == 2
        with pytest.raises(PermissionError):
            await r.team.ingress.report(previous, "checkpoint", "old worker saw it", acknowledged=["R1"],
                                        call_id=f"fixture-report:{uuid.uuid4().hex}")
        await r.call(sid, "assign", task_id=task_id, staff="Mira")
        await until_await(lambda: admitted(r, task_id), "the revised worker launched")
        assert len(runtime.started) == 2
        assert "Use English" in runtime.started[-1].first_message
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_staged_requirement_can_retry_stop_after_a_run_becomes_observable(
    settings: Settings, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        fake(r)
        r.team.app.extensions["effects"].register("task.stop", TaskStopEffect(r.team.app))
        sid = await office(r)
        worker = await member(r)
        task_id = await operator_task(db, r.project.id, "Script", brief=SCRIPT)
        await r.call(sid, "assign", task_id=task_id, staff="Mira")
        await until_await(lambda: admitted(r, task_id), "the worker launched")
        previous = await live(r, worker)
        scope = Scope("project", r.project.id)
        entity = Entity("task", task_id)
        stage = json.loads(await r.call(sid, "require", task_id=task_id,
                                        text="Use English", kind="quality", source="operator",
                                        client_operation_id="late-run-language",
                                        expected_entity_revision=await ControlStore(db).revision(scope, entity)))
        assert stage["state"] == "stop_unavailable"
        assert "no active native run" in stage["blocker"]
        assert (await db.fetchone("SELECT COUNT(*) FROM task_requirements WHERE task_id = ?", (task_id,)))[0] == 0
        with monkeypatch.context() as patch:
            run_id, stops = await bind_native_run(r.team, previous, patch)
            stop = json.loads(await r.call(sid, "require", op="stop", task_id=task_id,
                                           intent_id=stage["intent_id"], client_operation_id="late-run-language:stop",
                                           expected_entity_revision=await ControlStore(db).revision(scope, entity)))
            assert stop["state"] == "pending_physical_exit"
            await finish_native_stop(r.team, previous, run_id, stop["stop_effect_id"], stops)
        applied = json.loads(await r.call(sid, "require", op="apply", task_id=task_id,
                                          intent_id=stage["intent_id"], client_operation_id="late-run-language:apply",
                                          expected_entity_revision=await ControlStore(db).revision(scope, entity)))
        assert applied["state"] == "applied" and applied["contract_revision"] == 2
    finally:
        await close_team(r.manager)
        await r.manager.close()
