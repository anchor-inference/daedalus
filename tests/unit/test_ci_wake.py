"""A failed required check on a task's branch head reaches the worker, or else the coordinator, once."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.extensions.ci_observations import observation_from_webhook, record_observation
from daedalus.extensions.ci_wake import CiFailures, ci_link
from daedalus.host.events import EventBus
from daedalus.stores.database import Database
from daedalus.stores.staff import StaffStore

HEAD = "a" * 40


class FakeTeam:
    """The staff extension as the notice uses it: the branch head, the live session and a message."""

    def __init__(self, db: Database, head: str = HEAD) -> None:
        self.head = head
        self.store = StaffStore(db)
        self.told: list[dict[str, Any]] = []
        self.fail = False
        self.review = SimpleNamespace(branch_head=self.branch_head)

    async def branch_head(self, task_id: str) -> str:
        return self.head

    async def live(self, staff_session_id: str) -> Any:
        session = await self.store.session(staff_session_id)
        if session is None or not session.live:
            return None
        return SimpleNamespace(staff=await self.store.get(session.staff_id), session=session)

    async def tell(self, member: Any, text: str, *, when: str, by: str, message_id: str) -> dict[str, Any]:
        message = await self.store.add_message(member.id, text, origin=by, mode=when, message_id=message_id)
        self.told.append({"member": member.id, "text": text, "by": by, "message_id": message.id})
        return {"message_id": message.id, "state": "failed" if self.fail else "submitted", "error": ""}


@pytest.fixture
async def world(tmp_path: Path) -> Any:
    db = Database(tmp_path / "state.sqlite")
    await db.open()
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,branch,created_at,updated_at)"
                     " VALUES ('task','Fix the parser','doing',3,'project','staff/parser','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO task_contract_versions(task_id,contract_revision,origin_kind,origin_ref,"
                     "snapshot_json,created_at) VALUES ('task',1,'operator','','{}','2026-01-01')")
    for name in ("unit", "lint"):
        await db.execute("INSERT INTO ci_required_checks(task_id,contract_revision,provider,repository_id,"
                         "check_name,created_at) VALUES ('task',1,'github','7',?,'2026-01-01')", (name,))
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                     " VALUES ('worker','project','Worker','daedalus','operator','2026-01-01')")
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status_at,started_at)"
                     " VALUES ('staff-session','worker','daedalus','task','2026-01-01','2026-01-01')")
    bus = EventBus(db)
    await bus.start()
    team = FakeTeam(db)
    app = SimpleNamespace(db=db, extensions={"staff": team}, manager=SimpleNamespace(bus=bus))
    yield SimpleNamespace(db=db, bus=bus, team=team, failures=CiFailures(app))  # type: ignore[arg-type]
    await bus.close()
    await db.close()


def body(run_id: int, conclusion: str, *, name: str = "unit", head: str = HEAD, attempt: int = 1) -> dict:
    return {"repository": {"id": 7, "full_name": "someone/example"},
            "check_run": {"id": run_id, "name": name, "head_sha": head, "status": "completed",
                          "conclusion": conclusion, "run_attempt": attempt,
                          "html_url": f"https://github.com/someone/example/runs/{run_id}"}}


async def observe(world: Any, delivery: str, payload: dict) -> list[dict[str, Any]]:
    """Store the delivery the way the webhook does, then pass it on."""
    observation = observation_from_webhook("github", "check_run", delivery, payload,
                                           payload_digest=hashlib.sha256(delivery.encode()).hexdigest())
    assert observation is not None
    async with world.db.transaction() as conn:
        await record_observation(conn, observation)
    return await world.failures.observed(observation, link=ci_link("check_run", payload))


async def ci_events(db: Database) -> list[dict[str, Any]]:
    rows = await db.fetchall("SELECT project_id,payload_json FROM app_events WHERE type = 'task.ci_failed'")
    return [{"project_id": row["project_id"], **json.loads(row["payload_json"])} for row in rows]


async def test_live_worker_is_told_once_per_run_attempt(world: Any) -> None:
    await observe(world, "lint-red", body(40, "failure", name="lint"))
    sent = await observe(world, "unit-red", body(41, "failure"))
    assert [item["to"] for item in sent] == ["worker"]
    told = world.team.told[-1]
    assert told["member"] == "worker" and told["by"] == "orchestrator"
    assert "unit (run 41)" in told["text"] and HEAD[:12] in told["text"]
    assert "Also failed at this head: lint." in told["text"]
    assert "https://github.com/someone/example/runs/41" in told["text"]
    # GitHub repeats one failure under new delivery identities; the worker hears it once.
    assert await observe(world, "unit-red-again", body(41, "failure")) == []
    assert len(world.team.told) == 2
    # A rerun is a new attempt, and a new failure worth saying.
    again = await observe(world, "unit-red-rerun", body(41, "failure", attempt=2))
    assert [item["to"] for item in again] == ["worker"] and "attempt 2" in world.team.told[-1]["text"]
    assert await ci_events(world.db) == []


async def test_without_a_live_worker_the_coordinator_wakes(world: Any) -> None:
    await world.db.execute("UPDATE staff_sessions SET ended_at = '2026-01-02' WHERE id = 'staff-session'")
    sent = await observe(world, "unit-red", body(41, "failure"))
    assert [item["to"] for item in sent] == ["coordinator"] and world.team.told == []
    events = await ci_events(world.db)
    assert len(events) == 1
    event = events[0]
    assert event["project_id"] == "project" and event["task_id"] == "task"
    assert event["check_name"] == "unit" and event["head_sha"] == HEAD and event["run_id"] == "41"
    assert event["link"].endswith("/runs/41")
    assert await observe(world, "unit-red-again", body(41, "failure")) == []
    assert len(await ci_events(world.db)) == 1


async def test_failed_delivery_to_the_worker_falls_back_to_the_coordinator(world: Any) -> None:
    world.team.fail = True
    sent = await observe(world, "unit-red", body(41, "failure"))
    assert [item["to"] for item in sent] == ["coordinator"]
    assert await observe(world, "unit-red-again", body(41, "failure")) == []


async def test_old_heads_passes_and_unrequired_checks_stay_quiet(world: Any) -> None:
    assert await observe(world, "green", body(41, "success")) == []
    assert await observe(world, "old-head", body(42, "failure", head="b" * 40)) == []
    assert await observe(world, "other", body(43, "failure", name="docs")) == []
    # A late failure of an older run after a newer run of the same check passed is history.
    await observe(world, "newer-green", body(51, "success", name="lint"))
    assert await observe(world, "older-red", body(50, "failure", name="lint")) == []
    await world.db.execute("UPDATE board_tasks SET status = 'done' WHERE id = 'task'")
    assert await observe(world, "after-done", body(60, "failure")) == []
    assert world.team.told == [] and await ci_events(world.db) == []
