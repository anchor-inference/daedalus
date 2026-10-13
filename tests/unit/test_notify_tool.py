"""The ``Notify`` tool: what it accepts, its budget, what it posts, what it answers, and who has it."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from daedalus.config import NotificationsConfig, Settings
from daedalus.extensions.notifications import (
    NOTIFY_BODY_MAX,
    NOTIFY_TITLE_MAX,
    AgentNotifier,
    NotificationService,
    NotificationView,
    NotifyBudget,
    NotifyFacts,
    NotifyRefused,
    check_notify,
    notify_outcome,
)
from daedalus.host import prompts
from daedalus.host.events import EventFilter
from daedalus.host.presence import PresenceReport
from daedalus.stores.database import Database
from tests.unit.test_notification_router import host_with
from tests.unit.test_session_runner import _wait_finished


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def notifier_for(service: NotificationService, sessions: dict[str, NotifyFacts], clock: Clock | None = None) -> AgentNotifier:
    async def facts(session_id: str) -> NotifyFacts | None:
        return sessions.get(session_id)

    return AgentNotifier(service, facts, clock=clock or Clock())


async def rows(service: NotificationService) -> list[NotificationView]:
    return (await service.list("all"))["entries"]


# -- what it accepts -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "says"),
    [
        ({"title": "  "}, "title is required"),
        ({"title": "x" * (NOTIFY_TITLE_MAX + 1)}, f"at most {NOTIFY_TITLE_MAX}"),
        ({"body": "x" * (NOTIFY_BODY_MAX + 1)}, f"at most {NOTIFY_BODY_MAX}"),
        ({"level": "loud"}, "not one of: quiet, normal, urgent"),
        ({"link": "http://example.org/"}, "an app path starting with /app/, or an https:// URL"),
        ({"link": "javascript:alert(1)"}, "an https:// URL"),
        ({"link": "agents/abc"}, "/app/"),
        ({"link": "/app/agents/a b"}, "/app/"),
        ({"key": "has space"}, "letters, digits"),
        ({"key": "k" * 81}, "at most 80"),
    ],
)
def test_wrong_arguments_are_refused_with_what_is_accepted(arguments: dict[str, str], says: str) -> None:
    call = {"title": "Build finished", "body": "", "level": "normal", "link": "", "key": ""} | arguments
    with pytest.raises(NotifyRefused) as refused:
        check_notify(**call)
    assert says in str(refused.value)


def test_accepted_arguments_come_back_cleaned() -> None:
    assert check_notify(" Build finished ", " all green ", "urgent", "/app/agents/abc", "build") == ("Build finished", "all green", "urgent", "/app/agents/abc", "build")
    assert check_notify("t", "", "quiet", "https://ci.example.org/run/1", "ci:run/1.x_y-z")[3] == "https://ci.example.org/run/1"


# -- the budget ----------------------------------------------------------------------------------


def test_the_budget_counts_per_session_and_frees_up_as_the_window_moves() -> None:
    clock = Clock()
    budget = NotifyBudget(clock)
    prefs = NotificationsConfig()
    for _ in range(5):
        assert budget.take("s1", False, prefs) == (None, "", False)
        clock.advance(minutes=1)
    next_at, limit, urgent_only = budget.take("s1", False, prefs)
    assert limit == "5 notifications in 10 minutes is the limit" and not urgent_only
    assert next_at == datetime(2026, 9, 24, 12, 10, tzinfo=UTC)  # the first of the five leaves the window
    assert budget.take("s2", False, prefs)[1] == ""  # another session has its own
    clock.now = datetime(2026, 9, 24, 12, 10, tzinfo=UTC)
    assert budget.take("s1", False, prefs)[1] == ""
    assert budget.take("s1", False, prefs)[1] != ""  # 12:01 is still inside


def test_urgent_has_its_own_hourly_budget_and_a_normal_one_still_goes() -> None:
    clock = Clock()
    budget = NotifyBudget(clock)
    prefs = NotificationsConfig(notify_tool_per_session=50)
    assert budget.take("s1", True, prefs)[1] == ""
    clock.advance(minutes=20)
    assert budget.take("s1", True, prefs)[1] == ""
    clock.advance(minutes=20)
    next_at, limit, urgent_only = budget.take("s1", True, prefs)
    assert (limit, urgent_only, next_at) == ("2 urgent notifications an hour is the limit", True, datetime(2026, 9, 24, 13, 0, tzinfo=UTC))
    assert budget.take("s1", False, prefs)[1] == ""
    clock.now = datetime(2026, 9, 24, 13, 0, tzinfo=UTC)
    assert budget.take("s1", True, prefs)[1] == ""
    off = NotificationsConfig(notify_tool_urgent_per_hour=0)
    assert budget.take("s9", True, off) == (None, "urgent notifications from agents are switched off", True)


def test_a_refused_urgent_call_does_not_spend_the_ordinary_budget_and_a_refund_gives_one_back() -> None:
    budget = NotifyBudget(Clock())
    prefs = NotificationsConfig(notify_tool_per_session=3, notify_tool_urgent_per_hour=1)
    budget.take("s1", True, prefs)
    for _ in range(4):
        budget.take("s1", True, prefs)  # refused by the urgent limit, costs nothing
    assert budget.take("s1", False, prefs)[1] == ""
    assert budget.take("s1", False, prefs)[1] == ""
    assert budget.take("s1", False, prefs)[1] != ""
    budget.refund("s1", False)
    assert budget.take("s1", False, prefs)[1] == ""


# -- what it posts and answers ---------------------------------------------------------------------


async def test_a_notification_is_posted_as_the_agents_own_and_announced(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    notifier = notifier_for(service, {"s1": NotifyFacts({}, project_id="p1")})
    try:
        said = await notifier.notify(session_id="s1", title="Backup finished", body="42 GB in 18 minutes", level="normal")
        [entry] = await rows(service)
        assert (entry["category"], entry["kind"], entry["level"], entry["tone"]) == ("agent_notify", "agent", "normal", "info")
        assert (entry["source"], entry["session_id"], entry["project_id"], entry["link"]) == ("agent:s1", "s1", "p1", "/app/agents/s1")
        assert (entry["title"], entry["body"], entry["dedupe_key"]) == ("Backup finished", "42 GB in 18 minutes", None)
        assert said == "Sent: in the app."
        [announced] = await host.manager.bus.replay(0, EventFilter(types=("notify",)), limit=10)
        assert announced.payload["notification"]["id"] == entry["id"] and announced.payload["toast"] is True
        await notifier.notify(session_id="s1", title="See the report", link="https://ci.example.org/run/7")
        assert (await rows(service))[0]["link"] == "https://ci.example.org/run/7"
    finally:
        await host.close()


async def test_the_key_updates_within_a_session_and_never_across_sessions(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    notifier = notifier_for(service, {"s1": NotifyFacts({}), "s2": NotifyFacts({})})
    try:
        await notifier.notify(session_id="s1", title="Deploy: building", key="deploy")
        said = await notifier.notify(session_id="s1", title="Deploy: done", key="deploy")
        await notifier.notify(session_id="s2", title="Deploy: building", key="deploy")
        by_session = {e["session_id"]: e for e in await rows(service)}
        assert (by_session["s1"]["count"], by_session["s1"]["title"], by_session["s1"]["dedupe_key"]) == (2, "Deploy: done", "agent:s1:deploy")
        assert (by_session["s2"]["count"], by_session["s2"]["dedupe_key"]) == (1, "agent:s2:deploy")
        assert said.startswith("Updated")
    finally:
        await host.close()


async def test_the_budget_is_enforced_in_the_service_and_says_when_the_next_is_possible(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    clock = Clock()
    notifier = notifier_for(service, {"s1": NotifyFacts({})}, clock)
    try:
        await host.manager.presence.report(PresenceReport(client="tab", visible=False, tz="Europe/Berlin"))
        for n in range(5):
            await notifier.notify(session_id="s1", title=f"step {n}")
        with pytest.raises(NotifyRefused) as refused:
            await notifier.notify(session_id="s1", title="one too many")
        # 12:00 UTC plus the ten-minute window, on the operator's clock (CEST, UTC+2).
        assert str(refused.value) == "Not sent: 5 notifications in 10 minutes is the limit; the next is possible at 14:10."
        assert len(await rows(service)) == 5
        clock.advance(minutes=10)
        await notifier.notify(session_id="s1", title="urgent one", level="urgent")
        await notifier.notify(session_id="s1", title="urgent two", level="urgent")
        with pytest.raises(NotifyRefused) as refused:
            await notifier.notify(session_id="s1", title="urgent three", level="urgent")
        assert "2 urgent notifications an hour is the limit; the next is possible at 15:10." in str(refused.value)
        assert "level 'normal' can go now" in str(refused.value)
    finally:
        await host.close()


async def test_a_post_that_fails_gives_its_unit_of_budget_back(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    notifier = notifier_for(service, {"s1": NotifyFacts({})})
    try:
        original = service.post

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("the database is locked")

        service.post = broken  # type: ignore[method-assign]
        for _ in range(6):
            with pytest.raises(RuntimeError):
                await notifier.notify(session_id="s1", title="x")
        service.post = original  # type: ignore[method-assign]
        await notifier.notify(session_id="s1", title="now it works")
    finally:
        await host.close()


async def test_the_answer_says_it_was_only_recorded_when_the_operator_is_looking(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    notifier = notifier_for(service, {"s1": NotifyFacts({}), "s2": NotifyFacts({})})
    try:
        await host.manager.presence.report(PresenceReport(client="tab", visible=True, focused=True, sessions=("s1",)))
        said = await notifier.notify(session_id="s1", title="Done")
        assert said == "Recorded: the operator is looking at this session, so nothing was sent; they have seen it."
        assert (await rows(service))[0]["seen"] is True
        said = await notifier.notify(session_id="s2", title="Done elsewhere")
        assert said == "Sent: in the app. Not pushed or on the desktop: the operator has the app open."
        assert await notifier.notify(session_id="s2", title="Just for the record", level="quiet") == "Recorded quietly in the notifications: no sound, no badge."
    finally:
        await host.close()


def test_the_answer_names_every_channel_that_carried_it_and_why_others_did_not() -> None:
    view: Any = {
        "count": 1,
        "delivered": {"in_app": "toast", "push": {"sent": 2}, "desktop": "sent", "telegram": "skipped: quiet hours"},
    }
    assert notify_outcome(view) == "Sent: in the app, pushed to 2 devices and on the desktop. Not to Telegram: quiet hours."
    view = {"count": 1, "delivered": {"in_app": "toast", "push": {"queued": 1}}}
    assert notify_outcome(view) == "Sent: in the app and pushed to 1 device."
    view = {"count": 3, "delivered": {"in_app": "seen: off", "push": "skipped: off", "desktop": "skipped: rate limit", "telegram": "session"}}
    assert notify_outcome(view) == "Updated: to Telegram. Not on the desktop: the push limit."
    view = {"count": 1, "delivered": {"in_app": "seen: off", "push": "skipped: no device", "desktop": "skipped: no launcher", "telegram": "skipped: no bot"}}
    assert notify_outcome(view) == "Sent in the notifications without a sound."


async def test_subagents_and_staff_are_refused_by_the_service_as_well(settings: Settings, db: Database) -> None:
    host, service = await host_with(settings, db)
    notifier = notifier_for(service, {
        "child": NotifyFacts({"subagent_of": "leader"}),
        "staff": NotifyFacts({"staff_session_id": "ss1", "staff_id": "st1"}),
    })
    try:
        for sid in ("child", "staff"):
            with pytest.raises(NotifyRefused, match="reaches the operator through the agent that gave it to you"):
                await notifier.notify(session_id=sid, title="I am done")
        with pytest.raises(NotifyRefused, match="not running here"):
            await notifier.notify(session_id="gone", title="x")
        assert await rows(service) == []
    finally:
        await host.close()


# -- who has it --------------------------------------------------------------------------------------


async def test_the_tool_and_its_prompt_are_for_leaders_only(settings: Settings, db: Database) -> None:
    host, _service = await host_with(settings, db)
    manager = host.manager
    try:
        ordinary = await manager.create_session("ordinary")
        beat = await manager.create_session("beat", metadata={"heartbeat": True})
        child = await manager.create_session("child", metadata={"subagent_of": ordinary.session.id})
        grandchild = await manager.create_session("grandchild", metadata={"subagent_of": child.session.id})
        staff = await manager.create_session("staff", metadata={"staff_session_id": "ss1", "staff_id": "st1"})
        companion = await manager.create_session("companion", metadata={"companion": True})
        for state, allowed in ((ordinary, True), (beat, True), (child, False), (grandchild, False), (staff, False)):
            assert ("Notify" not in manager.blocked_tools_for(state)) is allowed, state.session.title
            engine = await manager._build_engine(state, f"run-{state.session.title}")
            assert (prompts.NOTIFY in engine.config.system_prompt_sections) is allowed, state.session.title
        # The companion is a leader and may notify, but its own brief says when; the agents' section is not its.
        assert "Notify" not in manager.blocked_tools_for(companion)
        engine = await manager._build_engine(companion, "run-companion")
        assert prompts.NOTIFY not in engine.config.system_prompt_sections
        assert "Notify reaches the operator" in engine.config.system_prompt_sections[0]
        # A project's orchestrator keeps it within its allowlist, and its own brief says when to use it.
        orchestrator = await manager.create_session("orchestrator", metadata={"orchestrator_of": "p1"})
        assert "Notify" not in manager.blocked_tools_for(orchestrator)
        engine = await manager._build_engine(orchestrator, "run-orchestrator")
        assert prompts.NOTIFY not in engine.config.system_prompt_sections
        assert "Notify only for what cannot wait" in engine.config.system_prompt_sections[0]
        # A leader's tools_off reaches the prompt as well: the section never names a tool the session lacks.
        ordinary.metadata["tools_off"] = ["Notify"]
        engine = await manager._build_engine(ordinary, "run-off")
        assert prompts.NOTIFY not in engine.config.system_prompt_sections
    finally:
        await host.close()


async def test_an_agent_calls_notify_and_reads_what_happened(settings: Settings, db: Database) -> None:
    script = [
        {"tool": "Notify", "args": {"title": "The file is written", "body": "report.pdf, 12 pages", "key": "report"}},
        {"tool": "Notify", "args": {"title": "Wrong", "level": "loud"}},
        {"text": "done"},
    ]
    host, service = await host_with(settings, db, script)
    manager = host.manager
    try:
        state = await manager.create_session("writer")
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(state.session.id, "write the report and notify me when the file is written")
        await waiter
        [entry] = await rows(service)
        assert (entry["title"], entry["source"], entry["dedupe_key"]) == ("The file is written", f"agent:{state.session.id}", f"agent:{state.session.id}:report")
        provider = manager.providers.rungs_for(manager.config)[0][0]  # type: ignore[index]
        results = [b for r in provider.requests for m in r.messages if m.role.value == "tool" for b in m.content_blocks]
        texts = [str(getattr(b, "content", "") or getattr(b, "text", "")) for b in results]
        assert any("Sent: in the app." in t for t in texts), texts
        assert any("not one of: quiet, normal, urgent" in t for t in texts), texts
    finally:
        await host.close()


async def test_without_the_notifications_extension_the_tool_says_so(settings: Settings, db: Database) -> None:
    host, _service = await host_with(settings, db, [{"tool": "Notify", "args": {"title": "x"}}, {"text": "done"}])
    manager = host.manager
    try:
        del manager.service_hooks["notify"]
        state = await manager.create_session("s")
        waiter = asyncio.create_task(_wait_finished(manager))
        await manager.submit(state.session.id, "go")
        await waiter
        provider = manager.providers.rungs_for(manager.config)[0][0]  # type: ignore[index]
        results = [str(getattr(b, "content", "") or getattr(b, "text", "")) for r in provider.requests for m in r.messages if m.role.value == "tool" for b in m.content_blocks]
        assert any("notifications are not available here" in t for t in results), results
    finally:
        await host.close()
