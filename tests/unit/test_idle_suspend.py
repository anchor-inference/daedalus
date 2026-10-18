"""Idle command-line members are suspended and resumed on their own session.

End to end on the same stand as the CLI runtime's tests: a fake Claude in a real terminal of the
daemon's test double, behind the real terminals service and the team. The idle time is not waited
out: the sweep is given a moment far enough after the member's last word.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from daedalus.config import Settings
from daedalus.extensions import staff_suspend
from daedalus.harness import conditions
from daedalus.harness.contract import LaunchPlan, LaunchSpec
from daedalus.stores.database import Database
from daedalus.stores.harness import HarnessStore
from daedalus.stores.staff import Staff, StaffError
from tests.unit.test_cli_staff_runtime import Stand, StubClaude, eventually, stand, terminals_service, trust

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="pseudo-terminals and process groups as on Linux")

DONE = "report:done:the menu is written"


class QuietResume(StubClaude):
    """The stub, resumed without a prompt as the real adapters are: a CLI woken for a message waits at
    its prompt for that message instead of running its script again."""

    def __init__(self, script: str = "echo:hello") -> None:
        super().__init__(script)
        self.resumed: list[str] = []

    def resume_plan(self, spec: LaunchSpec, ref: str) -> LaunchPlan:
        self.resumed.append(ref)
        plan = super().resume_plan(spec, ref)
        if spec.first_prompt:
            return plan
        return replace(plan, argv=plan.argv[:-1], first_prompt=None)


class NoResume(QuietResume):
    capabilities = replace(StubClaude.capabilities, resumes=False)


def later(minutes: int = 21) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


async def finished(s: Stand, script: str = DONE) -> Staff:
    """A member that worked a task, handed it in and sits idle at its prompt."""
    trust(s)
    ada = await s.hire()
    task_id = await s.task()
    await s.assign(ada, task_id)
    await s.status_event(ada, "turn_done_unseen")

    async def handed_in() -> bool:
        row = await s.manager.db.fetchone("SELECT status FROM board_tasks WHERE id = ?", (task_id,))
        return row is not None and row["status"] in ("review", "done")

    if script == DONE:
        await eventually(handed_in, "the task was handed in")
    return ada


def resumed(s: Stand) -> list[str]:
    """The native sessions the runtime launched the CLI again on, in order."""
    return list(s.runtime.adapter.resumed)  # type: ignore[attr-defined]


async def test_an_idle_member_is_suspended_and_its_session_kept(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        row = await s.session_row(ada)
        assert row.cli_session_id
        # Not yet: twenty minutes have not passed.
        assert await staff_suspend.suspend_idle(s.team) == 0
        assert await staff_suspend.suspend_idle(s.team, later()) == 1
        after = await s.session_row(ada)
        assert after.status == "suspended" and after.ended_at is None
        assert after.cli_session_id == row.cli_session_id
        assert await HarnessStore(db).open_launch_for(row.id) is None
        view = await s.terminals.get(row.terminal_id)
        assert view["status"] != "running"
        assert ada.id and row.id not in s.runtime.sessions
        found = await conditions.load(db, staff_session_id=row.id, session_status=after.status, session_status_at=after.status_at)
        first = found[0]
        assert (first.name, first.status, first.reason, first.params["idle_minutes"]) == ("running", "false", "suspended", 20)
        assert "suspended after 20 min idle" in first.message
        assert conditions.blocking(found) is first
        # No crash was reported for the terminal the host stopped on purpose.
        assert "exited" not in await s.statuses(ada)


async def test_a_member_with_work_in_flight_is_not_suspended(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume("echo:still on it")) as s:
        ada = await finished(s, script="echo")
        row = await s.session_row(ada)
        # Its turn ended without a report: the task is still in doing and its attempt open.
        live = await s.team.live(row.id)
        assert live is not None
        assert "doing" in await staff_suspend.blocker(s.team, live, 20, later())
        assert await staff_suspend.suspend_idle(s.team, later()) == 0
        assert (await s.session_row(ada)).status == "turn_done_unseen"


async def test_a_queued_message_or_an_open_request_keeps_it_running(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        row = await s.session_row(ada)
        live = await s.team.live(row.id)
        assert live is not None
        assert await staff_suspend.blocker(s.team, live, 20, later()) == ""
        message = await s.manager.staff.add_message(ada.id, "one more thing", origin="operator", mode="after_turn", staff_session_id=row.id)
        assert await staff_suspend.blocker(s.team, live, 20, later()) == "a message is on its way to it"
        await s.manager.staff.set_message_state(message.id, "acknowledged")
        await s.team.ingress.question(live, "q-1", "Which oven?", ["left", "right"])
        live = await s.team.live(row.id)
        assert live is not None
        assert await staff_suspend.blocker(s.team, live, 20, later()) == "it is question"
        # Even shown idle, a request still open keeps it.
        await s.team.ingress.status(live, "idle")
        live = await s.team.live(row.id)
        assert live is not None
        assert await staff_suspend.blocker(s.team, live, 20, later()) == "a request of it is open"
        assert await staff_suspend.suspend_idle(s.team, later()) == 0
        assert (await s.session_row(ada)).status == "idle"


async def test_a_message_sent_while_the_sweep_runs_is_delivered(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        sweep = asyncio.create_task(staff_suspend.suspend_idle(s.team, later()))
        await asyncio.sleep(0)
        told = await s.team.tell(ada, "echo:in the middle", when="after_turn")
        await sweep

        async def acknowledged() -> bool:
            stored = await s.manager.db.fetchone("SELECT state FROM staff_messages WHERE id = ?", (told["message_id"],))
            return stored is not None and stored["state"] == "acknowledged"

        # Whichever came first, the message is not lost: either the sweep saw it and left the
        # member running, or the member was suspended and the message woke it.
        await eventually(acknowledged, "the message reached the CLI")
        assert (await s.session_row(ada)).status != "suspended"


async def test_a_change_at_the_moment_of_the_stop_keeps_it_running(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        row = await s.session_row(ada)
        live = await s.team.live(row.id)
        assert live is not None
        marked: list[str] = []

        async def arrived(fresh: Any) -> str:
            return "a message arrived"

        async def mark(fresh: Any) -> None:
            marked.append(fresh.id)

        assert await s.runtime.suspend(live, recheck=arrived, mark=mark) == "a message arrived"
        assert marked == []
        assert (await s.terminals.get(row.terminal_id))["status"] == "running"
        assert s.runtime.continuity(live) == "live"
        assert (await s.session_row(ada)).status in ("turn_done_unseen", "idle")


async def test_a_stop_that_cannot_be_confirmed_is_reported_not_retried(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)

        async def refuse(live: Any) -> None:
            raise RuntimeError("the daemon did not answer")

        s.runtime.stop = refuse  # type: ignore[method-assign]
        assert await staff_suspend.suspend_idle(s.team, later()) == 0
        event = await s.status_event(ada, "error")
        assert "outcome is unknown" in event.payload["detail"]
        # An error is not idle: the next sweep leaves it alone.
        assert await staff_suspend.suspend_idle(s.team, later(60)) == 0


async def test_a_message_resumes_the_session_and_is_delivered(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        row = await s.session_row(ada)
        assert await staff_suspend.suspend_idle(s.team, later()) == 1
        told = await s.team.tell(ada, "echo:back again", when="after_turn")
        assert told["state"] in ("queued", "written", "submitted", "acknowledged")

        async def acknowledged() -> bool:
            stored = await s.manager.db.fetchone("SELECT state FROM staff_messages WHERE id = ?", (told["message_id"],))
            return stored is not None and stored["state"] == "acknowledged"

        await eventually(acknowledged, "the message reached the resumed CLI")
        again = await s.session_row(ada)
        assert again.id == row.id and again.cli_session_id == row.cli_session_id
        assert again.terminal_id != row.terminal_id
        assert resumed(s) == [row.cli_session_id]
        await s.status_event(ada, "working")
        assert await db.kv_get(staff_suspend.KEY + row.id) is None


async def test_an_automatic_notice_does_not_wake_a_suspended_member(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        assert await staff_suspend.suspend_idle(s.team, later()) == 1
        with pytest.raises(StaffError):
            await s.team.tell(ada, "CI failed on main", automatic=True)
        assert (await s.session_row(ada)).status == "suspended"
        assert resumed(s) == []


async def test_an_assignment_resumes_the_suspended_conversation(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        row = await s.session_row(ada)
        assert await staff_suspend.suspend_idle(s.team, later()) == 1
        await s.assign(ada, await s.task("Price list"))

        async def started_again() -> bool:
            # The new session's row is written before its CLI is launched; asked as soon as the row
            # appeared, the runtime had sometimes not launched it yet and nothing was resumed.
            live = await s.manager.staff.live(ada.id)
            return live is not None and live.id != row.id and bool(resumed(s))

        await eventually(started_again, "the next task started on the resumed conversation")
        fresh = await s.session_row(ada)
        assert fresh.predecessor_id == row.id
        assert resumed(s) == [row.cli_session_id]
        old = await s.manager.staff.session(row.id)
        assert old is not None and old.ended_at is not None



@pytest.mark.parametrize("named", ["the suspended session", "an earlier session of its conversation"])
async def test_an_assignment_naming_the_held_conversation_resumes_it(settings: Settings, db: Database, named: str) -> None:
    """A coordinator names the conversation to resume: the suspended session itself, or the earlier
    one it continues. Both used to be refused before the suspended session ended, so the launch
    failed and the member asleep after its idle minutes never got its next task."""
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        row = await s.session_row(ada)
        assert await staff_suspend.suspend_idle(s.team, later()) == 1
        source = row.id
        if named != "the suspended session":
            source = "ss-earlier"
            await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,terminal_id,cli_session_id,transcript_ref,task_id,"
                             "status,status_at,started_at,ended_at,end_reason,folder_id,launch_cwd)"
                             " SELECT 'ss-earlier',staff_id,kind,'gone',cli_session_id,transcript_ref,task_id,'exited',"
                             "status_at,started_at,started_at,'released',folder_id,launch_cwd FROM staff_sessions WHERE id = ?",
                             (row.id,))
        await s.assign(ada, await s.task("Price list"), resume_from=source)

        async def started_again() -> bool:
            live = await s.manager.staff.live(ada.id)
            return live is not None and live.id != row.id and bool(resumed(s))

        await eventually(started_again, "the next task started on the held conversation")
        assert resumed(s) == [row.cli_session_id]
        old = await s.manager.staff.session(row.id)
        assert old is not None and old.ended_at is not None

async def test_a_restarted_host_leaves_a_suspended_member_suspended(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        assert await staff_suspend.suspend_idle(s.team, later()) == 1
        s.runtime.close()
        await s.terminals.close()
        s.terminals = terminals_service(db, s.manager, s.root / "run", s.home)
        s.team.app.extensions["terminals"] = s.terminals
        await s.terminals.start()
        assert await s.terminals.wait_available("container")
        runtime = s.restart_runtime(QuietResume(DONE))
        assert await runtime.reconcile(wait=5) == 0
        await asyncio.sleep(0.2)
        assert (await s.session_row(ada)).status == "suspended"
        assert "exited" not in await s.statuses(ada)
        # And it still wakes on the next message.
        await s.team.tell(ada, "echo:after the restart", when="after_turn")
        await s.status_event(ada, "working")


async def test_a_host_gone_in_the_middle_of_a_suspend_finishes_it(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        ada = await finished(s)
        row = await s.session_row(ada)
        live = await s.team.live(row.id)
        assert live is not None
        # The row says suspended, and the host went before the stop.
        await s.team.ingress.status(live, "suspended")
        s.runtime.close()
        runtime = s.restart_runtime(QuietResume(DONE))
        assert await runtime.reconcile(wait=5) == 0
        assert await HarnessStore(db).open_launch_for(row.id) is None

        async def stopped() -> bool:
            return (await s.terminals.get(row.terminal_id))["status"] != "running"

        await eventually(stopped, "the terminal of the half-suspended member was stopped")
        assert (await s.session_row(ada)).status == "suspended"


async def test_a_runtime_that_cannot_resume_is_never_suspended(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=NoResume(DONE)) as s:
        ada = await finished(s)
        live = await s.team.live((await s.session_row(ada)).id)
        assert live is not None
        assert "cannot take a session up again" in s.runtime.suspendable(live)
        assert await staff_suspend.suspend_idle(s.team, later(600)) == 0
        assert (await s.session_row(ada)).status != "suspended"


async def test_zero_minutes_never_suspends(settings: Settings, db: Database) -> None:
    async with stand(settings, db, adapter=QuietResume(DONE)) as s:
        await finished(s)
        s.manager.config.harness.idle_suspend_minutes = 0
        assert await staff_suspend.suspend_idle(s.team, later(600)) == 0


def test_the_condition_says_how_long_it_was_idle() -> None:
    found = conditions.suspended({"idle_minutes": 20, "since": "2026-10-15T08:00:00+00:00"}, None)
    assert found.view()["params"] == {"idle_minutes": 20}
    assert found.since == "2026-10-15T08:00:00+00:00"
    assert conditions.line([found]).startswith("condition: running: suspended after 20 min idle")
