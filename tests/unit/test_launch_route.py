"""A member's launch over the project's route moves on only after a definite refusal before admission."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.extensions.launch_route import RouteRefused, route_assign, story, window_refusal
from daedalus.staff_runtime import Availability
from daedalus.stores.control import ControlConflict, Principal
from daedalus.stores.database import Database
from daedalus.stores.launch_routes import LaunchRoutes
from daedalus.stores.projects import ProjectError, ProjectSettings, parse_route
from daedalus.stores.staff import StaffStore

OPERATOR = Principal.operator({"via": "token", "user_id": 1})


class FakeRuntime:
    def __init__(self, ok: bool = True, reason: str = "") -> None:
        self.ok, self.reason = ok, reason

    async def available(self, env: str) -> Availability:
        return Availability(self.ok, self.reason)


class Journal:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, dict[str, Any]]] = []

    async def record(self, project_id: str, author: str, kind: str, text: str, refs: dict[str, Any] | None = None) -> None:
        self.lines.append((kind, text, refs or {}))


class Bus:
    async def publish(self, *args: Any, **kwargs: Any) -> None:
        return None


async def fixture(db: Database, route: list[dict[str, str]], *, harness: str = "claude",
                  runtimes: dict[str, FakeRuntime] | None = None) -> SimpleNamespace:
    settings = {"member_route": route}
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01',?)",
                     (json.dumps(settings),))
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                     " VALUES ('task','Work','todo',3,'project','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,model,agent,created_by,created_at)"
                     " VALUES ('worker','project','Ada',?,'opus','reviewer','operator','2026-01-01')", (harness,))
    staff = StaffStore(db)
    journal = Journal()
    notes: list[str] = []
    ended: list[str] = []
    folder = SimpleNamespace(id="folder", path=Path("/tmp/route-fixture"), env="container")

    async def project(project_id: str) -> Any:
        row = await db.fetchone("SELECT settings FROM projects WHERE id = ?", (project_id,))
        return SimpleNamespace(id=project_id, settings=ProjectSettings.load(json.loads(row["settings"])))

    async def member(staff_id: str) -> Any:
        return await staff.get(staff_id)

    async def task(task_id: str) -> Any:
        return SimpleNamespace(id=task_id)

    async def live_of(member: Any) -> Any:
        return await staff.live(member.id)

    async def end(live: Any, reason: str, *, stop: bool) -> None:
        ended.append(reason)
        await staff.end_session(live.id, reason)

    async def note_on_card(task_id: str, text: str) -> None:
        notes.append(text)

    manager = SimpleNamespace(db=db, projects=SimpleNamespace(local_env="container", record=journal.record),
                              config=SimpleNamespace(has_model=True, presets={"default": object()}),
                              providers=None, bus=Bus())
    team = SimpleNamespace(app=SimpleNamespace(db=db), manager=manager, project=project, member=member, task=task,
                           folder_for=lambda *_: folder, live_of=live_of, _end=end, note_on_card=note_on_card,
                           runtimes=runtimes if runtimes is not None else {name: FakeRuntime() for name in ("claude", "codex", "daedalus")})
    return SimpleNamespace(team=team, staff=staff, journal=journal, notes=notes, ended=ended)


class Launches:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.fail = fail

    async def __call__(self, member: Any, effort: str | None) -> dict[str, Any]:
        self.calls.append((member.harness, member.model, effort))
        if self.fail is not None:
            raise self.fail
        return {"task_id": "task", "effect_id": f"effect-{len(self.calls)}", "state": "queued"}


async def assign(env: SimpleNamespace, launch: Launches, operation: str = "op-1", effort: str | None = None) -> dict[str, Any]:
    member = await env.staff.get("worker")
    return await route_assign(env.team, member, "task", principal=OPERATOR, client_operation_id=operation,
                              effort=effort, launch=launch)


async def spent_window(db: Database, harness: str, *, minutes: int = 300, at: datetime | None = None) -> None:
    """A finished session of another member on the same CLI whose last snapshot shows a full window."""
    observed = (at or datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                     " VALUES (?, 'project', ?, ?, 'operator', '2026-01-01')", (f"other-{harness}", f"Other {harness}", harness))
    usage = {"input_tokens": 1, "output_tokens": 1, "source": "subscription", "at": observed,
             "windows": [{"minutes": minutes, "used_pct": 100.0}, {"minutes": 10080, "used_pct": 40.0}]}
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,status,status_at,started_at,ended_at,usage_json)"
                     " VALUES (?, ?, 'cli', 'exited', ?, ?, ?, ?)",
                     (f"spent-{harness}", f"other-{harness}", observed, observed, observed, json.dumps(usage)))


async def test_a_spent_usage_window_moves_to_the_next_runtime_and_says_why(db: Database) -> None:
    env = await fixture(db, [{"runtime": "claude"}, {"runtime": "codex", "model": "gpt-5-codex"}])
    await spent_window(db, "claude")
    launch = Launches()
    result = await assign(env, launch, effort="high")
    assert launch.calls == [("codex", "gpt-5-codex", None)]  # effort means something only to Daedalus
    route = result["route"]
    assert route["outcome"] == "accepted" and route["selected_harness"] == "codex"
    assert [a["outcome"] for a in route["attempts"]] == ["pre_admission_rejected", "accepted"]
    assert route["reason"].startswith("Claude Code (opus): usage window exhausted")
    assert route["reason"].endswith("→ Codex (gpt-5-codex) accepted")
    member = await env.staff.get("worker")
    assert (member.harness, member.model, member.agent) == ("codex", "gpt-5-codex", "")
    assert member.route_home is not None and member.route_home["harness"] == "claude" and member.route_home["agent"] == "reviewer"
    assert member.route_home["reason"] == route["reason"]
    assert env.journal.lines and env.journal.lines[0][0] == "launch_route"
    assert env.notes and "usage window exhausted" in env.notes[0]


async def test_the_member_returns_to_its_own_runtime_once_it_takes_launches_again(db: Database) -> None:
    env = await fixture(db, [{"runtime": "codex"}])
    await db.execute("UPDATE staff SET harness = 'codex',model = '',agent = '',route_home = ? WHERE id = 'worker'",
                     (json.dumps({"harness": "claude", "model": "opus", "agent": "reviewer", "permission_mode": "",
                                  "effort": "", "env": ""}),))
    launch = Launches()
    result = await assign(env, launch)
    assert launch.calls == [("claude", "opus", None)]
    assert result["route"]["selected_harness"] == "claude"
    member = await env.staff.get("worker")
    assert (member.harness, member.agent, member.route_home) == ("claude", "reviewer", None)
    assert env.journal.lines == []  # the first runtime took it: nothing to tell


@pytest.mark.parametrize("reason", ["Claude Code is not signed in in the container environment",
                                    "Claude Code is not installed in the container environment"])
async def test_missing_sign_in_or_install_is_a_definite_refusal(db: Database, reason: str) -> None:
    env = await fixture(db, [{"runtime": "codex"}],
                        runtimes={"claude": FakeRuntime(False, reason), "codex": FakeRuntime(), "daedalus": FakeRuntime()})
    launch = Launches()
    result = await assign(env, launch)
    assert launch.calls == [("codex", "", None)]
    assert result["route"]["attempts"][0]["reason"] == reason


async def test_an_unknown_admission_stops_the_chain_and_is_never_replayed(db: Database) -> None:
    env = await fixture(db, [{"runtime": "codex"}, {"runtime": "daedalus"}])
    launch = Launches(fail=TimeoutError("the launch command's reply was lost"))
    with pytest.raises(TimeoutError):
        await assign(env, launch)
    assert launch.calls == [("claude", "opus", None)]  # never a second runtime for the same task
    record = await LaunchRoutes(db).latest_for_task("task")
    assert record is not None and record.state == "admission_unknown"
    assert [a["outcome"] for a in record.attempts] == ["admission_unknown"]
    again = Launches()
    with pytest.raises(RouteRefused) as refused:
        await assign(env, again)
    assert again.calls == []
    assert refused.value.route["outcome"] == "admission_unknown"


async def test_a_repeated_operation_id_is_answered_from_the_record(db: Database) -> None:
    env = await fixture(db, [{"runtime": "codex"}])
    await spent_window(db, "claude")
    launch = Launches()
    first = await assign(env, launch)
    second = await assign(env, launch)
    assert len(launch.calls) == 1
    assert second["effect_id"] == first["effect_id"] and second["route"]["outcome"] == "accepted"


async def test_a_claim_left_by_a_stopped_host_is_reported_and_never_launched(db: Database) -> None:
    env = await fixture(db, [{"runtime": "codex"}])
    store = LaunchRoutes(db)
    claimed, record = await store.claim("op-1", project_id="project", task_id="task", staff_id="worker", actor_id=OPERATOR.actor_id)
    assert claimed
    record.attempts.append({"harness": "claude", "model": "opus", "outcome": "claimed", "at": "2026-01-01"})
    await store.save(record)
    launch = Launches()
    with pytest.raises(RouteRefused, match="in flight"):
        await assign(env, launch)
    assert launch.calls == []
    settled = await store.settle_interrupted()
    assert [r.state for r in settled] == ["admission_unknown"]
    assert settled[0].attempts[0]["outcome"] == "admission_unknown"
    assert "nothing was launched again" in settled[0].reason
    with pytest.raises(RouteRefused):
        await assign(env, launch)
    assert launch.calls == []


async def test_only_one_caller_claims_an_operation(db: Database) -> None:
    await fixture(db, [{"runtime": "codex"}])
    store = LaunchRoutes(db)
    first = await store.claim("op", project_id="project", task_id="task", staff_id="worker", actor_id="a")
    second = await store.claim("op", project_id="project", task_id="task", staff_id="worker", actor_id="a")
    assert first[0] is True and second[0] is False
    assert second[1].claim_token == first[1].claim_token


async def test_a_live_member_on_another_runtime_requires_a_handoff(db: Database) -> None:
    env = await fixture(db, [{"runtime": "codex"}])
    await spent_window(db, "claude")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at)"
                     " VALUES ('busy','project','Bo','claude','operator','2026-01-01')")
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status,status_at,started_at)"
                     " VALUES ('live','busy','cli','task','working','2026-01-01','2026-01-01')")
    launch = Launches()
    with pytest.raises(RouteRefused, match="handoff required") as refused:
        await assign(env, launch)
    assert launch.calls == []
    assert refused.value.route["outcome"] == "handoff_required"
    member = await env.staff.get("worker")
    assert member.harness == "claude"  # nothing switched


async def test_a_member_waiting_on_a_question_stops_the_chain(db: Database) -> None:
    env = await fixture(db, [{"runtime": "codex"}])
    await spent_window(db, "claude")
    await db.execute("INSERT INTO board_tasks(id,title,status,priority,project_id,created_at,updated_at)"
                     " VALUES ('other','Other','doing',3,'project','2026-01-01','2026-01-01')")
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,task_id,status,status_at,started_at)"
                     " VALUES ('asking','worker','cli','other','question','2026-01-01','2026-01-01')")
    launch = Launches()
    with pytest.raises(RouteRefused, match="still in a session"):
        await assign(env, launch)
    assert launch.calls == []


async def test_a_command_refusal_stops_the_chain_and_puts_the_member_back(db: Database) -> None:
    env = await fixture(db, [{"runtime": "codex"}, {"runtime": "daedalus"}])
    await spent_window(db, "claude")
    launch = Launches(fail=ControlConflict("reopen the task before launching a new attempt"))
    with pytest.raises(ControlConflict):
        await assign(env, launch)
    assert launch.calls == [("codex", "", None)]
    member = await env.staff.get("worker")
    assert (member.harness, member.model, member.route_home) == ("claude", "opus", None)
    record = await LaunchRoutes(db).latest_for_task("task")
    assert record is not None and record.state == "refused"


async def test_no_runtime_left_launches_nothing(db: Database) -> None:
    env = await fixture(db, [{"runtime": "codex"}],
                        runtimes={"claude": FakeRuntime(False, "not signed in"), "codex": FakeRuntime(False, "not installed")})
    launch = Launches()
    with pytest.raises(RouteRefused, match="no runtime of the route") as refused:
        await assign(env, launch)
    assert launch.calls == []
    assert refused.value.route["outcome"] == "no_runtime"


async def test_native_provider_quota_moves_on_only_when_every_rung_rests(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    env = await fixture(db, [{"runtime": "daedalus"}, {"runtime": "codex"}], harness="daedalus")
    await db.execute("UPDATE staff SET model = '', agent = '' WHERE id = 'worker'")
    vendor, spare = (SimpleNamespace(endpoint=SimpleNamespace(id=name)) for name in ("vendor", "spare"))
    rungs = [(vendor, "m"), (spare, "m")]
    env.team.manager.providers = SimpleNamespace(rungs_for=lambda config, preset: rungs)
    resting = {"vendor": "vendor refused with quota and is not expected to answer before 2026-01-01T14:20+00:00"}

    async def rest(conn: Any, provider_ids: list[str], at: Any = None) -> dict[str, str]:
        return {pid: why for pid, why in resting.items() if pid in provider_ids}

    monkeypatch.setattr("daedalus.extensions.launch_route.resting_providers_in", rest)
    launch = Launches()
    await assign(env, launch, operation="spare-answers", effort="high")
    assert launch.calls == [("daedalus", "", "high")]  # the run's own chain falls back to the spare
    resting["spare"] = "spare refused with auth"
    await db.execute("UPDATE board_tasks SET status = 'todo'")
    result = await assign(env, launch, operation="all-resting")
    assert launch.calls[-1] == ("codex", "", None)
    assert "vendor refused with quota" in result["route"]["attempts"][0]["reason"]


async def test_a_window_past_its_length_proves_nothing(db: Database) -> None:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('project','Work','2026-01-01','{}')")
    await spent_window(db, "claude", minutes=60, at=datetime.now(UTC) - timedelta(hours=2))
    assert await window_refusal(db, "claude") == ""
    await db.execute("DELETE FROM staff_sessions")
    await db.execute("DELETE FROM staff")
    await spent_window(db, "claude", minutes=300)
    assert "until" in await window_refusal(db, "claude")


def test_the_route_is_validated_when_written_and_tolerated_when_read() -> None:
    assert [e.runtime for e in parse_route([{"runtime": "native"}, {"runtime": "Codex", "model": " gpt "}])] == ["daedalus", "codex"]
    assert parse_route([{"runtime": "nope"}, {"runtime": "pi"}]) == parse_route([{"runtime": "pi"}])
    with pytest.raises(ProjectError, match="named twice"):
        parse_route([{"runtime": "pi"}, {"runtime": "pi"}], strict=True)
    with pytest.raises(ProjectError, match="one of"):
        parse_route([{"runtime": "nope"}], strict=True)
    settings = ProjectSettings.load({"member_route": [{"runtime": "claude", "model": "opus"}]})
    assert settings.dump()["member_route"] == [{"runtime": "claude", "model": "opus"}]
    assert ProjectSettings.load({}).member_route == ()


def test_the_story_reads_as_one_line() -> None:
    attempts = [{"harness": "claude", "model": "", "outcome": "pre_admission_rejected", "reason": "usage window exhausted until 14:20 UTC at the latest"},
                {"harness": "codex", "model": "", "outcome": "accepted"}]
    assert story(attempts, "accepted") == "Claude Code: usage window exhausted until 14:20 UTC at the latest → Codex accepted"
