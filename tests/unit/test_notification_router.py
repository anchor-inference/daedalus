"""The notification router: which bus event becomes which notification, holds for an orchestrator,
answers taken from a notification (the first one wins), the channels, the words, and the routes."""

from __future__ import annotations

import asyncio
import json
import string
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from daedalus.config import NotificationsConfig, RuntimeConfig, Settings
from daedalus.extensions import notifications as notifications_module
from daedalus.extensions.api import build_app
from daedalus.extensions.notifications import (
    Action,
    ActionConflict,
    ActionOutcome,
    ActionRefused,
    ActionRequest,
    Draft,
    NotificationRouter,
    NotificationService,
    NotificationView,
    ProjectNotifyPolicy,
)
from daedalus.host import notify_text
from daedalus.host.config_validation import ConfigConflict, config_revision
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.presence import PresenceReport
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.waiting import until_await
from tests.unit.test_notifications import FakeFront
from tests.unit.test_session_events import ASK, events, run_once
from tests.unit.test_session_runner import ScriptedProvider, _manager, _wait_finished

HEAD = {"X-Daedalus-Token": "tok"}
NOW = "2026-09-24T12:00:00.000Z"


class Host:
    """What ``install`` and the routes need of the application, around a real session manager."""

    def __init__(self, settings: Settings, db: Database, manager: SessionManager, front: Any = None) -> None:
        self.settings, self.db, self.manager, self.front = settings, db, manager, front
        self.config = manager.config
        self.extensions: dict[str, Any] = {}
        self.notifications: NotificationService | None = None
        self.guard = None
        self.create_session = manager.create_session
        self.tasks: list[asyncio.Task[None]] = []

    async def install(self) -> NotificationService:
        self.tasks = await notifications_module.install(self)  # type: ignore[arg-type]
        assert self.notifications is not None
        return self.notifications

    async def save_config(self, config: RuntimeConfig, *, expected_revision: str | None = None) -> None:
        if expected_revision is not None and config_revision(self.config) != expected_revision:
            raise ConfigConflict(config_revision(self.config))
        self.config = config
        self.manager.config = config

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.manager.close()


async def host_with(settings: Settings, db: Database, script: list[dict[str, Any]] | None = None, *, front: Any = None) -> tuple[Host, NotificationService]:
    manager = await _manager(settings, db, ScriptedProvider(script or []))
    host = Host(settings, db, manager, front)
    return host, await host.install()


def event(kind: str, payload: dict[str, Any], **ids: str | None) -> AppEvent:
    return AppEvent(seq=1, at=NOW, type=kind, payload=payload, **ids)


def finished(status: str = "completed", **overrides: Any) -> dict[str, Any]:
    payload = {"run_id": "r1", "status": status, "duration_s": 45.0, "watched": False, "origin": "operator",
               "operator_facing": True, "telegram": False, "title": "Bakery", "summary": "The endpoint is ready."}
    return {**payload, **overrides}


def ask_payload(sid: str, call: str = "call-1", *, options: int = 2, telegram: bool = False, questions: int = 1) -> dict[str, Any]:
    question = {"question": "Color?", "options": [{"label": f"option {n}", "description": ""} for n in range(options)], "multi": False, "custom": True}
    return {"request_id": call, "request_ref": f"ask:{sid}:{call}", "run_id": "r1", "title": "Bakery",
            "questions": [question] * questions, "operator_facing": True, "telegram": telegram}


def permission_payload(sid: str, key: str = "0123456789ab", *, quick: bool = True) -> dict[str, Any]:
    return {"request_id": key, "request_ref": f"policy:{sid}:{key}", "kind": "policy", "title": "Bakery", "tool": "Exec",
            "text": "curl https://other.example/", "risk": "routine" if quick else "elevated", "quick": quick, "telegram": False}


async def entries(service: NotificationService) -> list[NotificationView]:
    return (await service.list("all"))["entries"]


async def notify_events(host: Host, kind: str = "notify") -> list[AppEvent]:
    return await host.manager.bus.replay(0, EventFilter(types=(kind,)), limit=1000)


# -- which event becomes which notification ---------------------------------------------------------


async def test_a_finished_run_is_announced_when_it_took_long_enough_and_was_for_the_operator(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    router = NotificationRouter(service, host.manager)
    try:
        await router.handle(event("run.finished", finished(), session_id="s1", project_id="p1"))
        await router.handle(event("run.finished", finished(duration_s=12.0), session_id="s2"))  # under the threshold
        await router.handle(event("run.finished", finished(operator_facing=False), session_id="s3"))  # a subagent's
        await router.handle(event("run.finished", finished(status="cancelled"), session_id="s4"))
        [entry] = await entries(service)
        assert (entry["category"], entry["tone"], entry["title"], entry["body"]) == ("run_finished", "ok", "Finished: Bakery", "The endpoint is ready.")
        assert (entry["link"], entry["dedupe_key"], entry["project_id"], entry["run_id"]) == ("/app/agents/s1", "run:s1", "p1", "r1")
        # Delivered by the Telegram front: recorded as handled, and nothing is pushed on top of it.
        await router.handle(event("run.finished", finished(telegram=True), session_id="s5"))
        assert (await entries(service))[0]["delivered"]["telegram"] == "handled"
    finally:
        await host.close()


async def test_a_failed_run_is_announced_unless_it_reports_itself(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    router = NotificationRouter(service, host.manager)
    try:
        beat = await host.manager.create_session("beat", metadata={"heartbeat": True})
        await router.handle(event("run.finished", finished("failed", error="the provider refused: quota"), session_id="s1"))
        await router.handle(event("run.finished", finished("failed", origin="schedule"), session_id="s2"))
        await router.handle(event("run.finished", finished("failed", origin="operator"), session_id=beat.session.id))
        await router.handle(event("run.finished", finished("failed", operator_facing=False, origin="subagent-task:x"), session_id="s3"))
        titled = [(e["session_id"], e["category"], e["tone"], e["body"]) for e in await entries(service)]
        assert titled == [
            ("s3", "run_failed", "error", notify_text.render("run.failed.body")),
            ("s1", "run_failed", "error", "the provider refused: quota"),
        ]
    finally:
        await host.close()


async def test_a_question_carries_its_options_as_buttons_when_they_fit(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    router = NotificationRouter(service, host.manager)
    try:
        await router.handle(event("ask.pending", ask_payload("s1"), session_id="s1"))
        await router.handle(event("ask.pending", ask_payload("s2", options=6), session_id="s2"))
        await router.handle(event("ask.pending", ask_payload("s3", questions=2), session_id="s3"))
        by_session = {e["session_id"]: e for e in await entries(service)}
        one = by_session["s1"]
        assert (one["category"], one["level"], one["request_ref"], one["dedupe_key"]) == ("question", "normal", "ask:s1:call-1", "ask:s1:call-1")
        assert [(a["id"], a["label"], a["quick"]) for a in one["actions"]] == [("answer:0", "option 0", True), ("answer:1", "option 1", True), ("open", "Open", False)]
        assert "Color?" in one["body"] and "· option 1" in one["body"] and one["needs_you"] is True
        assert [a["id"] for a in by_session["s2"]["actions"]] == ["open"]
        assert [a["id"] for a in by_session["s3"]["actions"]] == ["open"]
        # A restored question is the same request again: one entry, counted.
        await router.handle(event("ask.pending", ask_payload("s1"), session_id="s1"))
        assert len(await entries(service)) == 3 and (await service.get(one["id"]))["count"] == 2  # type: ignore[index]

        await router.handle(event("ask.answered", {"request_id": "call-1", "request_ref": "ask:s1:call-1", "via": "telegram"}, session_id="s1"))
        answered = await service.get(one["id"])
        assert answered is not None and (answered["resolved"], answered["needs_you"]) == ("answered", False)
        resolved = await notify_events(host, "notify.resolved")
        assert [(e.payload["request_ref"], e.payload["resolution"], e.payload["via"]) for e in resolved] == [("ask:s1:call-1", "answered", "telegram")]
        # The end of the run a question belonged to expires what is still open in it; a refusal stays open.
        await router.handle(event("permission.pending", permission_payload("s2"), session_id="s2"))
        await router.handle(event("run.finished", finished("completed", operator_facing=False), session_id="s2"))
        states = {(e["session_id"], e["category"]): e["resolved"] for e in await entries(service)}
        assert states[("s2", "question")] == "expired" and states[("s2", "permission")] is None
    finally:
        await host.close()


async def test_a_permission_request_is_urgent_and_quick_only_when_the_request_allows(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    router = NotificationRouter(service, host.manager)
    try:
        await router.handle(event("permission.pending", permission_payload("s1"), session_id="s1"))
        await router.handle(event("permission.pending", permission_payload("s2", quick=False), session_id="s2"))
        by_session = {e["session_id"]: e for e in await entries(service)}
        routine = by_session["s1"]
        assert (routine["category"], routine["level"], routine["kind"], routine["body"]) == ("permission", "urgent", "policy", "Exec: curl https://other.example/")
        assert routine["title"] == "Bakery is waiting for permission"
        assert [(a["id"], a["quick"]) for a in routine["actions"]] == [("allow", True), ("deny", True), ("open", False)]
        # A rule about the machine itself is answered in the app, never from a lock screen.
        assert [(a["id"], a["quick"]) for a in by_session["s2"]["actions"]] == [("allow", False), ("deny", False), ("open", False)]
        await router.handle(event("permission.resolved", {"request_id": "0123456789ab", "request_ref": "policy:s1:0123456789ab", "decision": "deny", "via": "app"}, session_id="s1"))
        assert (await service.get(routine["id"]))["resolved"] == "deny"  # type: ignore[index]
    finally:
        await host.close()


async def test_the_operator_can_forbid_quick_answers_altogether(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    try:
        host.config.notifications.quick_actions = False
        await NotificationRouter(service, host.manager).handle(event("permission.pending", permission_payload("s1"), session_id="s1"))
        assert not any(a["quick"] for a in (await entries(service))[0]["actions"])
    finally:
        await host.close()


async def test_staff_turns_errors_reviews_and_terminal_notifications(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    router = NotificationRouter(service, host.manager)
    try:
        await router.handle(event("staff.status", {"status": "working", "previous": "starting"}, staff_id="st1", project_id="p1"))
        await router.handle(event("staff.status", {"status": "turn_done_unseen", "previous": "working", "detail": "tests pass"}, staff_id="st1", project_id="p1"))
        await router.handle(event("staff.status", {"status": "turn_done_unseen", "previous": "working"}, staff_id="st1", project_id="p1"))
        await router.handle(event("staff.status", {"status": "error", "previous": "working", "detail": "exit 2"}, staff_id="st1", project_id="p1"))
        await router.handle(event("task.moved", {"task_id": "t1", "title": "Checkout", "from": "doing", "to": "done"}, project_id="p1"))
        await router.handle(event("task.moved", {"task_id": "t2", "title": "Payments", "from": "doing", "to": "review"}, project_id="p1", staff_id="st1"))
        await router.handle(event("terminal.notify", {"title": "Build finished", "body": "0 errors"}, terminal_id="term1"))
        rows = [(e["category"], e["title"], e["count"], e["source"]) for e in reversed(await entries(service))]
        assert rows == [
            ("staff_turn", "st1 finished a turn", 2, "staff:st1"),
            ("run_failed", "st1 ran into an error", 1, "staff:st1"),
            ("staff_review", "Ready for review: Payments", 1, "board"),
            ("agent_notify", "Build finished", 1, "terminal:term1"),
        ]
        review = (await entries(service))[1]
        assert review["link"] == "/app/board/t2" and review["project_id"] == "p1"
    finally:
        await host.close()


async def test_the_router_runs_on_the_bus(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    try:
        await host.manager.bus.publish("terminal.notify", {"title": "Done", "body": ""}, terminal_id="term1")

        async def arrived() -> bool:
            return bool(await entries(service))

        await until_await(arrived, "the router turned the bus event into a notification")
    finally:
        await host.close()


# -- holding a staff request for the orchestrator ---------------------------------------------------


async def test_a_held_request_waits_for_the_orchestrator_and_goes_out_when_its_time_comes(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    router = NotificationRouter(service, host.manager)
    hold = {"seconds": 60}
    service.set_project_policy(lambda project_id: ProjectNotifyPolicy(orchestrated=project_id == "p1", hold_seconds=hold["seconds"], orchestrator_session_id="orch"))
    try:
        staff = {"session_id": "s1", "project_id": "p1", "staff_id": "st1"}
        await router.handle(event("permission.pending", permission_payload("s1"), **staff))
        assert await entries(service) == [] and await service.summary() == {"unseen": 0, "needs_you": 0}
        assert await notify_events(host) == []  # the operator has not heard of it at all
        row = await db.fetchone("SELECT held_until, event_seq FROM notifications")
        assert row["held_until"] is not None and row["event_seq"] is None

        # The orchestrator answers it: closed without a sound.
        await service.resolve("policy:s1:0123456789ab", "allow", via="orchestrator")
        assert await notify_events(host) == [] and await notify_events(host, "notify.resolved") == []

        # The orchestrator escalates another one: out at once.
        await router.handle(event("permission.pending", permission_payload("s1", "00000000000b"), **staff))
        assert await service.release("policy:s1:00000000000b") == 1
        [escalated] = await notify_events(host)
        assert escalated.payload["notification"]["request_ref"] == "policy:s1:00000000000b"

        # The orchestrator's own request is never held for itself.
        await router.handle(event("permission.pending", permission_payload("orch", "0000000000cc"), session_id="orch", project_id="p1"))
        assert len(await notify_events(host)) == 2

        # Nobody answers: the keeper releases it when the hold ends.
        hold["seconds"] = 1
        await router.handle(event("permission.pending", permission_payload("s1", "00000000000d"), **staff))

        async def released() -> bool:
            return len(await notify_events(host)) == 3

        await until_await(released, "the held request went out when its hold ended")
        assert (await entries(service))[0]["needs_you"] is True
    finally:
        await host.close()


async def test_what_came_due_while_the_host_was_down_goes_out_at_start(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    service = NotificationService(db, manager.bus)
    service.set_project_policy(lambda _project: ProjectNotifyPolicy(orchestrated=True, hold_seconds=600))
    try:
        await service.post(Draft("question", "Naya asks", request_ref="ask:s1:c1", session_id="s1", project_id="p1", staff_id="st1"))
        past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        await db.execute("UPDATE notifications SET held_until = ?", (past,))
        restarted = NotificationService(db, manager.bus)
        assert await restarted.release_due() == 1
        [announced] = await manager.bus.replay(0, EventFilter(types=("notify",)), limit=10)
        assert announced.payload["notification"]["needs_you"] is True
        assert await restarted.release_due() == 0  # once
    finally:
        await manager.close()


async def test_a_held_question_already_answered_is_closed_at_its_release_not_announced(settings: Settings, db: Database) -> None:
    """The orchestrator answered a member's question at 06:17:11 and the answer was kept from the
    member's session, so nothing closed the operator's held copy: it went out at 06:17:28 as a
    question waiting for them. A row whose ask is resolved is closed silently when its hold ends."""
    manager = await _manager(settings, db, ScriptedProvider([]))
    service = NotificationService(db, manager.bus)
    service.set_project_policy(lambda _project: ProjectNotifyPolicy(orchestrated=True, hold_seconds=600))
    try:
        await service.post(Draft("question", "Naya asks", request_ref="ask:s1:c1", session_id="s1", project_id="p1", staff_id="st1"))
        await service.post(Draft("question", "Naya asks again", request_ref="ask:s1:c2", session_id="s1", project_id="p1", staff_id="st1"))
        now = datetime.now(UTC).isoformat()
        for ref, resolved in (("ask:s1:c1", now), ("ask:s1:c2", None)):
            await db.execute(
                "INSERT INTO asks(id, short_id, origin, kind, text, detail_json, routed_to, created_at, routed_at, resolved_at, resolved_by)"
                " VALUES (?, ?, 'staff', 'question', 'Which?', ?, 'orchestrator', ?, ?, ?, ?)",
                (f"ask-{ref[-2:]}", ref[-2:], json.dumps({"event_ref": ref}), now, now, resolved, "orchestrator" if resolved else None),
            )
        await db.execute("UPDATE notifications SET held_until = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
        await service.release_due()
        [announced] = await manager.bus.replay(0, EventFilter(types=("notify",)), limit=10)
        assert announced.payload["notification"]["request_ref"] == "ask:s1:c2", "the open one still reaches the operator"
        closed = await db.fetchone("SELECT resolved_at, resolution, event_seq FROM notifications WHERE request_ref = 'ask:s1:c1'")
        assert closed["resolved_at"] and closed["resolution"] == "answered" and closed["event_seq"] is None
    finally:
        await manager.close()


# -- answering from a notification ------------------------------------------------------------------


async def test_answering_a_question_from_its_notification_continues_the_run(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db, [ASK, {"text": "you chose Blue"}])
    try:
        state = await host.manager.create_session("colours", metadata={"telegram_detached": True})
        assert await run_once(host.manager, state, "ask me") == "awaiting"

        async def posted() -> bool:
            return any(e["category"] == "question" for e in await entries(service))

        await until_await(posted, "the question became a notification")
        [question] = [e for e in await entries(service) if e["category"] == "question"]
        assert [a["id"] for a in question["actions"]] == ["answer:0", "answer:1", "open"]
        waiter = asyncio.create_task(_wait_finished(host.manager))
        resolution, view = await service.act(question["id"], "answer:1")
        assert (await waiter)[-1][2] == "completed"
        assert resolution == "answered" and view["resolved"] == "answered" and view["seen"] is True
        answered = (await events(host.manager, "ask.answered"))[0].payload
        assert answered["via"] == "notification"
        assert '"selected": ["Blue"]' in str(state.engine.history[-2].content_blocks[0].content)  # type: ignore[union-attr]
        with pytest.raises(ActionConflict) as conflict:
            await service.act(question["id"], "answer:0")
        assert conflict.value.resolution == "answered"
    finally:
        await host.close()


async def test_allow_and_deny_from_a_notification_really_answer_the_policy(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    try:
        state = await host.manager.create_session("policy", metadata={"telegram_detached": True})
        sid = state.session.id
        host.manager.config.policy.egress_allow = ["github.com"]
        gate = host.manager.policy_gate(sid, "run-1")
        first = gate.decide("Exec", {"command": "curl https://other.example/"})
        second = gate.decide("Exec", {"command": "curl https://another.example/"})

        async def both() -> bool:
            return len([e for e in await entries(service) if e["category"] == "permission"]) == 2

        await until_await(both, "both refusals became notifications")
        by_key = {e["request_ref"].rsplit(":", 1)[1]: e for e in await entries(service)}  # type: ignore[union-attr]
        resolution, view = await service.act(by_key[first.key]["id"], "allow")
        assert resolution == "allow" and view["resolved"] == "allow"
        assert first.key in state.metadata["policy_grants"] and first.key not in state.metadata["policy_pending"]  # the dock is clear
        assert gate.decide("Exec", {"command": "curl https://other.example/"}).action == "allow"
        resolution, _ = await service.act(by_key[second.key]["id"], "deny")
        assert resolution == "deny" and second.key not in state.metadata["policy_pending"]
        decisions = [(e.payload["decision"], e.payload["via"]) for e in await events(host.manager, "permission.resolved")]
        assert decisions == [("allow", "notification"), ("deny", "notification")]
        with pytest.raises(ActionConflict) as conflict:
            await service.act(by_key[first.key]["id"], "deny")
        assert conflict.value.resolution == "allow"
    finally:
        await host.close()


async def test_the_first_answer_wins_even_when_this_host_missed_the_other_one(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    service = NotificationService(db, manager.bus)
    calls: list[str] = []

    async def elsewhere(req: ActionRequest) -> ActionOutcome:
        calls.append(req.action)
        raise ActionConflict("deny")

    async def slow(req: ActionRequest) -> ActionOutcome:
        calls.append(req.action)
        await asyncio.sleep(0.05)
        return ActionOutcome(req.action)

    try:
        service.register_resolver("harness", elsewhere)
        view = await service.post(Draft("permission", "Naya waits", request_ref="harness:ss1:r1", actions=(Action("allow", "Allow", quick=True), Action("deny", "Deny"))))
        assert view is not None
        with pytest.raises(ActionConflict):
            await service.act(view["id"], "allow")
        assert (await service.get(view["id"]))["resolved"] == "deny"  # type: ignore[index]

        service.register_resolver("harness", slow)
        other = await service.post(Draft("permission", "Again", request_ref="harness:ss1:r2", actions=(Action("allow", "Allow", quick=True), Action("deny", "Deny"))))
        assert other is not None
        results = await asyncio.gather(service.act(other["id"], "allow"), service.act(other["id"], "deny"), return_exceptions=True)
        assert results[0][0] == "allow" and isinstance(results[1], ActionConflict) and results[1].resolution == "allow"  # type: ignore[index]
        assert calls == ["allow", "allow"]  # the losing tap never reached the resolver

        # From a lock screen only what is marked quick; nothing the entry does not offer; nothing without a request.
        third = await service.post(Draft("permission", "Third", request_ref="harness:ss1:r3", actions=(Action("allow", "Allow", quick=True), Action("deny", "Deny"))))
        assert third is not None
        with pytest.raises(ActionRefused):
            await service.act(third["id"], "deny", via="push", quick_only=True)
        with pytest.raises(ActionRefused):
            await service.act(third["id"], "explode")
        plain = await service.post(Draft("system", "fyi"))
        assert plain is not None
        with pytest.raises(ActionRefused):
            await service.act(plain["id"], "allow")
        with pytest.raises(LookupError):
            await service.act(99999, "allow")
        _, opened = await service.act(plain["id"], "open")
        assert opened["seen"] is True
    finally:
        await manager.close()


async def test_deleting_a_session_withdraws_its_open_requests(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    try:
        state = await host.manager.create_session("gone")
        sid = state.session.id
        await service.post(Draft("permission", "waits", session_id=sid, request_ref=f"policy:{sid}:0123456789ab"))
        await service.post(Draft("question", "asks", session_id=sid, request_ref=f"ask:{sid}:c1"))
        await host.manager.delete_session(sid)
        assert {e["resolved"] for e in await entries(service)} == {"withdrawn"}
    finally:
        await host.close()


# -- the channels -------------------------------------------------------------------------------


class FakePush:
    name = "push"

    def __init__(self) -> None:
        self.sent: list[tuple[int, int]] = []
        self.devices = True

    def available(self) -> bool:
        return self.devices

    async def deliver(self, view: NotificationView, *, more: int) -> Any:
        self.sent.append((view["id"], more))
        return {"sent": 1}


async def test_presence_decides_between_toast_push_and_desktop(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    push = FakePush()
    service = NotificationService(db, manager.bus, presence=manager.presence)
    service.register_channel(push)
    try:
        # A site session finishing while the app shows another one: a toast, nothing else.
        await manager.presence.report(PresenceReport(client="tab", visible=True, focused=True, sessions=("other",)))
        await service.post(Draft("run_finished", "Finished: A", session_id="s1"))
        # The tab hidden and the launcher listening: push and desktop.
        await manager.presence.report(PresenceReport(client="tab", visible=False, focused=False))
        await manager.presence.stream_opened("", "launcher")
        await service.post(Draft("run_finished", "Finished: B", session_id="s2"))
        # The operator reading the session: nothing to record for its answer.
        await manager.presence.report(PresenceReport(client="tab", visible=True, focused=True, sessions=("s3",)))
        assert await service.post(Draft("run_finished", "Finished: C", session_id="s3")) is None
        present, away = [e.payload for e in await manager.bus.replay(0, EventFilter(types=("notify",)), limit=10)]
        assert (present["toast"], present["deliver"]) == (True, {"push": False, "desktop": False, "telegram": False})
        assert present["notification"]["delivered"]["push"] == "skipped: present"
        assert (away["toast"], away["deliver"]) == (True, {"push": True, "desktop": True, "telegram": False})
        assert away["notification"]["delivered"]["push"] == {"sent": 1} and len(push.sent) == 1
    finally:
        await manager.close()


async def test_a_repeat_is_silent_while_unseen_and_sounds_again_once_the_session_was_opened(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    push = FakePush()
    service.register_channel(push)
    try:
        await service.post(Draft("run_finished", "Finished: A", session_id="s1", dedupe_key="run:s1"))
        await service.post(Draft("run_finished", "Finished: A again", session_id="s1", dedupe_key="run:s1"))
        assert len(push.sent) == 1 and (await entries(service))[0]["delivered"]["push"] == "skipped: repeat"
        await host.manager.presence.report(PresenceReport(client="tab", visible=True, focused=True, sessions=("s1",)))

        async def seen() -> bool:
            return (await entries(service))[0]["seen"]

        await until_await(seen, "opening the session marked its notification seen")
        await host.manager.presence.report(PresenceReport(client="tab", visible=False, focused=False))
        await service.post(Draft("run_finished", "Finished: A a third time", session_id="s1", dedupe_key="run:s1"))
        assert len(push.sent) == 2 and (await entries(service))[0]["count"] == 3
    finally:
        await host.close()


async def test_quiet_hours_let_only_urgent_notifications_through(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    push = FakePush()
    now = datetime.now(UTC)
    window = f"{(now - timedelta(hours=1)).strftime('%H:%M')}-{(now + timedelta(hours=1)).strftime('%H:%M')}"
    service = NotificationService(db, manager.bus, presence=manager.presence, preferences=lambda: NotificationsConfig(quiet_hours=window))
    service.register_channel(push)
    try:
        await service.post(Draft("run_finished", "normal", session_id="s1"))
        await service.post(Draft("permission", "urgent", session_id="s2", request_ref="policy:s2:0123456789ab"))
        normal, urgent = [e.payload for e in await manager.bus.replay(0, EventFilter(types=("notify",)), limit=10)]
        assert normal["deliver"]["push"] is False and normal["notification"]["delivered"]["push"] == "skipped: quiet hours"
        assert urgent["deliver"]["push"] is True and len(push.sent) == 1
    finally:
        await manager.close()


async def test_telegram_lines_go_to_the_session_topic_and_never_to_a_detached_one(settings: Settings, db: Database) -> None:
    front = FakeFront()
    host, service = await host_with(settings, db, front=front)
    try:
        bound = await host.manager.create_session("bound")
        site = await host.manager.create_session("site", metadata={"telegram_detached": True})
        await service.post(Draft("reminder", "Water the plants", session_id=bound.session.id))
        await service.post(Draft("permission", "Site waits", session_id=site.session.id, request_ref=f"policy:{site.session.id}:0123456789ab"))
        assert front.topics == [(bound.session.id, "🔔 Water the plants")]
        assert front.notified == []
        delivered = {e["title"]: e["delivered"]["telegram"] for e in await entries(service)}
        assert delivered == {"Water the plants": "session", "Site waits": "skipped: kept off Telegram"}
    finally:
        await host.close()


async def test_a_subagent_is_kept_off_telegram_even_when_its_leader_is_bound(settings: Settings, db: Database) -> None:
    front = FakeFront()
    host, service = await host_with(settings, db, front=front)
    try:
        leader = await host.manager.create_session("leader")
        worker = await host.manager.create_session("worker", metadata={"subagent_of": leader.session.id})
        await service.post(Draft("permission", "Worker waits", session_id=worker.session.id, request_ref=f"policy:{worker.session.id}:0123456789ab"))
        assert front.topics == []
        assert front.notified == []
        delivered = {e["title"]: e["delivered"]["telegram"] for e in await entries(service)}
        assert delivered == {"Worker waits": "skipped: kept off Telegram"}
    finally:
        await host.close()


async def test_the_router_writes_in_the_operators_language(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    try:
        await host.manager.presence.report(PresenceReport(client="tab", visible=False, lang="ru", tz="Europe/Moscow"))
        await NotificationRouter(service, host.manager).handle(event("permission.pending", permission_payload("s1"), session_id="s1"))
        [entry] = await entries(service)
        assert entry["title"] == "Bakery ждёт разрешения"
        assert [a["label"] for a in entry["actions"]] == ["Разрешить", "Отклонить", "Открыть"]
    finally:
        await host.close()


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_every_sentence_exists_in_both_languages() -> None:
    assert set(notify_text.TEXT["en"]) == set(notify_text.TEXT["ru"])
    for key in notify_text.TEXT["en"]:
        english, russian = notify_text.TEXT["en"][key], notify_text.TEXT["ru"][key]
        assert placeholders(english) == placeholders(russian), key
    assert notify_text.render("burst", "ru-RU", count=3) == "и ещё 3"
    assert notify_text.render("allow", "de") == "Allow"


# -- the routes ---------------------------------------------------------------------------------


async def test_the_routes(settings: Settings, db: Database) -> None:
    front = FakeFront()
    host, service = await host_with(settings, db, front=front)
    try:
        state = await host.manager.create_session("policy", metadata={"telegram_detached": True})
        sid = state.session.id
        host.manager.config.policy.egress_allow = ["github.com"]
        refused = host.manager.policy_gate(sid, "run-1").decide("Exec", {"command": "curl https://other.example/"})

        async def posted() -> bool:
            return bool(await entries(service))

        await until_await(posted, "the refusal became a notification")
        [entry] = await entries(service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(host, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
            url = f"/api/notifications/{entry['id']}/act"
            assert (await client.post(url, json={"action": "allow"})).status_code == 401
            assert (await client.post(url, json={"action": "explode"}, headers=HEAD)).status_code == 400
            assert (await client.post("/api/notifications/99999/act", json={"action": "allow"}, headers=HEAD)).status_code == 404
            done = await client.post(url, json={"action": "allow"}, headers=HEAD)
            assert done.status_code == 200 and done.json()["resolution"] == "allow" and done.json()["notification"]["resolved"] == "allow"
            assert refused.key in state.metadata["policy_grants"]
            again = await client.post(url, json={"action": "deny"}, headers=HEAD)
            assert again.status_code == 409 and again.json()["resolution"] == "allow" and again.json()["notification"]["id"] == entry["id"]

            prefs = (await client.get("/api/notifications/preferences", headers=HEAD)).json()
            assert prefs["categories"][-2:] == ["spend", "system"] and prefs["preferences"]["matrix"]["spend"]["telegram"] == "on"
            changed = {**prefs["preferences"], "quiet_hours": "23:00-07:00"}
            saved = await client.put("/api/notifications/preferences", json={"preferences": changed, "base_revision": prefs["revision"]}, headers=HEAD)
            assert saved.status_code == 200 and saved.json()["preferences"]["quiet_hours"] == "23:00-07:00"
            assert host.config.notifications.quiet_hours == "23:00-07:00"
            stale = await client.put("/api/notifications/preferences", json={"preferences": changed, "base_revision": prefs["revision"]}, headers=HEAD)
            assert stale.status_code == 409
            bad = await client.put("/api/notifications/preferences", json={"preferences": {"quiet_hours": "never"}, "base_revision": saved.json()["revision"]}, headers=HEAD)
            assert bad.status_code == 400

            tested = (await client.post("/api/notifications/test", headers=HEAD)).json()["delivered"]
            assert tested["telegram"] == "general" and tested["push"] == "skipped: no device" and tested["in_app"] == "toast"
            assert front.notified and front.notified[-1][0].startswith("🔔 Test notification")
    finally:
        await host.close()
