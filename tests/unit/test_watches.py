"""Watches: what each kind waits for, what bounds them, and the three things that fire them — the event
bus, the ticker (silences and new commits) and a wait on a terminal's output."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from daedalus.config import Settings, WebhookConfig
from daedalus.extensions.api import build_app
from daedalus.extensions.inbound import Inbound, webhook_facts
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.watches import (
    WHEN_EVENTS,
    Watch,
    Watches,
    WatchRefused,
    check_regex,
    event_matches,
    webhook_matches,
)
from daedalus.host.events import AppEvent
from daedalus.staff_runtime import ReadPage
from daedalus.stores.database import Database
from daedalus.stores.projects import Project
from daedalus.terminals.model import InvalidRequest
from daedalus.tools.orchestrator import WATCH_EVENTS
from tests.support.authorized_launch import operator_assignment
from tests.support.waiting import until_await
from tests.unit.test_orchestrator import Rig, _idle, events, events_messages, rig
from tests.unit.test_staff_runtime import Capacity, ObservedFakeStaffRuntime, board_task, close_team
from tests.unit.test_wakeme import Notes


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeTerminals:
    """The terminals service as a watch uses it: one wait on a terminal's output at a time."""

    def __init__(self) -> None:
        self.answers: asyncio.Queue[Any] = asyncio.Queue()
        self.calls: list[dict[str, Any]] = []

    async def wait_for(self, terminal_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"terminal_id": terminal_id, **kwargs})
        answer = await self.answers.get()
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[no-any-return]


class WatchNotes(Notes):
    """A notification fake whose return value has a matching durable receipt."""

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    async def post(self, draft: Any) -> dict[str, int]:
        self.posted.append(draft)
        at = datetime.now(UTC).isoformat()
        async with self.db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO notifications(at,updated_at,kind,category,title,body,project_id,dedupe_key,event_seq)"
                " VALUES (?,?,?,'orchestrator_report',?,?,?,?,1)",
                (at, at, draft.kind, draft.title, draft.body, draft.project_id, draft.dedupe_key),
            )
            notification_id = int(cursor.lastrowid)
            await cursor.close()
        return {"id": notification_id}


async def with_watches(r: Rig, clock: Clock | None = None) -> Watches:
    app = r.team.app
    from daedalus.extensions.watch_commands import WatchCommands
    from daedalus.stores.control import ControlStore, Entity, Principal, Scope
    app.notifications = WatchNotes(r.manager.db)
    r.team._capacity = Capacity()
    keeper = Watches(app)
    if clock is not None:
        keeper.clock = clock
        keeper.nap = _nap
    app.extensions["watches"] = keeper
    await keeper.start()
    grants: dict[str, Principal] = {}
    operator = Principal.operator({"via": "token", "user_id": 1})
    command_number = 0

    def next_command() -> str:
        nonlocal command_number
        command_number += 1
        return f"fixture-watch-{command_number}"

    async def test_principal(project_id: str, by: str) -> Principal:
        if by == "operator":
            return operator
        if project_id not in grants:
            grant = await ControlStore(r.manager.db).issue_grant(
                operator, Principal(f"agent:test-watch:{project_id}", "agent"), Scope("project", project_id),
                operations=["watch.create", "watch.change", "watch.remove", "watch.deliver"],
                effects=["watch.wake", "watch.tell", "watch.notify"],
                expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            )
            grants[project_id] = Principal(f"agent:test-watch:{project_id}", "agent",
                                           grant["grant_id"], grant["generation"])
        return grants[project_id]

    async def create_with_approval(project: Project, **kwargs: Any) -> Watch:
        principal = await test_principal(project.id, kwargs.pop("by", "orchestrator"))
        fresh = await r.manager.projects.get(project.id)
        assert fresh is not None
        result = await WatchCommands(keeper).create(
            principal, fresh, expected_collection_revision=await ControlStore(r.manager.db).revision(
                Scope("project", project.id), Entity("collection", project.id)),
            client_operation_id=next_command(), **kwargs,
        )
        made = keeper.get(project.id, result["id"])
        assert made is not None
        return made

    async def update_with_approval(project_id: str, watch_id: str, **kwargs: Any) -> Watch:
        before = keeper.get(project_id, watch_id)
        assert before is not None
        principal = await test_principal(project_id, kwargs.pop("by", "operator"))
        project = await r.manager.projects.get(project_id)
        assert project is not None
        await WatchCommands(keeper).change(
            principal, project, watch_id,
            expected_entity_revision=await ControlStore(r.manager.db).revision(
                Scope("project", project_id), Entity("project", project_id)),
            expected_condition_revision=before.condition_revision,
            client_operation_id=next_command(), **kwargs,
        )
        changed = keeper.get(project_id, watch_id)
        assert changed is not None
        return changed

    async def remove_with_approval(project_id: str, watch_id: str, **kwargs: Any) -> bool:
        before = keeper.get(project_id, watch_id)
        if before is None:
            return False
        principal = await test_principal(project_id, kwargs.pop("by", "operator"))
        await WatchCommands(keeper).remove(
            principal, project_id, watch_id,
            expected_entity_revision=await ControlStore(r.manager.db).revision(
                Scope("project", project_id), Entity("project", project_id)),
            expected_condition_revision=before.condition_revision,
            client_operation_id=next_command(),
        )
        return True

    async def approved_tool(session_id: str, operation: str, task_id: str | None = None) -> tuple[Principal, Scope]:
        assert task_id is None and operation in {"watch.create", "watch.change", "watch.remove"}
        project, _ = await r.orch.current(session_id)
        return await test_principal(project.id, "orchestrator"), Scope("project", project.id)

    keeper.create = create_with_approval  # type: ignore[method-assign]
    keeper.update = update_with_approval  # type: ignore[method-assign]
    keeper.remove = remove_with_approval  # type: ignore[method-assign]
    app.extensions["board_tool_authority"] = approved_tool
    return keeper


async def _nap(seconds: float) -> None:
    """The clock is moved by the test, so a cooldown is waited out by looking again soon."""
    await asyncio.sleep(0.01)


def watch(event: str, **pattern: Any) -> Watch:
    return Watch(id="w1", project_id="p1", pattern={"event": event, **pattern}, action={"action": "wake"}, cooldown_s=600, once=False, note="", created_by="orchestrator", created_at="", last_fired_at=None, fire_count=0, enabled=True)


def app_event(event_type: str, payload: dict[str, Any], **ids: Any) -> AppEvent:
    return AppEvent(seq=1, at="", type=event_type, payload=payload, project_id="p1", **ids)


async def fired(r: Rig) -> list[AppEvent]:
    return await events(r.manager, "watch.fired")


async def test_delivery_cursor_deduplicates_after_cooldown_and_restart(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    clock = Clock()
    keeper = await with_watches(r, clock)
    try:
        project = await r.orch.enable(r.project.id)
        item = await keeper.create(project, when={"event": "staff_finished"}, then={"action": "wake"})
        assert await keeper.fire(item, "finished", source_cursor="bus:123")
        clock.advance(minutes=11)
        assert not await keeper.fire(item, "finished", source_cursor="bus:123")
        await keeper.load()
        assert not await keeper.fire(keeper.get(project.id, item.id), "finished", source_cursor="bus:123")
        rows = await db.fetchall("SELECT status, source_cursor FROM watch_deliveries WHERE watch_id = ?", (item.id,))
        assert [(row["status"], row["source_cursor"]) for row in rows] == [("delivered", "bus:123")]
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


# -- what each kind waits for ----------------------------------------------------------------------------


def test_each_matcher_waits_for_its_own_event_and_never_for_the_orchestrators_doing() -> None:
    ada = {"staff": "Ada", "staff_id": "s-ada"}
    finished = app_event("staff.status", {"status": "turn_done_unseen", "previous": "working"}, staff_id="s-ada")
    assert event_matches(watch("staff_finished", **ada), finished) == "Ada finished a turn"
    assert event_matches(watch("staff_finished"), finished) is not None, "no staff named: anyone"
    assert event_matches(watch("staff_finished", staff="Bo", staff_id="s-bo"), finished) is None
    assert event_matches(watch("staff_finished", **ada), app_event("staff.status", {"status": "idle", "previous": "working"}, staff_id="s-ada")) is None
    assert event_matches(watch("staff_question", **ada), app_event("ask.pending", {"request_id": "q"}, staff_id="s-ada")) == "Ada asks a question"
    permission = app_event("permission.pending", {"text": "Exec: rm -rf build"}, staff_id="s-ada")
    assert "rm -rf build" in (event_matches(watch("staff_permission", **ada), permission) or "")
    crash = watch("staff_crashed", **ada)
    assert "error" in (event_matches(crash, app_event("staff.status", {"status": "error", "previous": "working", "detail": "OOM"}, staff_id="s-ada")) or "")
    assert event_matches(crash, app_event("staff.status", {"status": "exited", "previous": "working"}, staff_id="s-ada")) is not None
    assert event_matches(crash, app_event("staff.status", {"status": "exited", "previous": "working", "actor": "operator"}, staff_id="s-ada")) is None, "a release is not a crash"
    moved = app_event("task.moved", {"task_id": "t1", "title": "Menu", "from": "doing", "to": "review", "actor": "staff"})
    assert event_matches(watch("task_moved", task_id="t1", to="review"), moved) is not None
    assert event_matches(watch("task_moved", to="done"), moved) is None
    own = app_event("task.moved", {"task_id": "t1", "title": "Menu", "from": "doing", "to": "review", "actor": "orchestrator"})
    assert event_matches(watch("task_moved"), own) is None, "its own doing never fires a watch"
    assert WATCH_EVENTS == WHEN_EVENTS, "the tool's schema offers what the extension knows"


def test_pull_requests_and_ci_are_read_from_github_payloads() -> None:
    merged = webhook_facts("pull_request", {"action": "closed", "repository": {"full_name": "shop/web"}, "pull_request": {"merged": True, "title": "Menu", "head": {"ref": "menu"}}})
    assert merged == {"repo": "shop/web", "action": "closed", "conclusion": "merged", "branch": "menu", "title": "Menu"}
    failed = webhook_facts("workflow_run", {"action": "completed", "repository": {"full_name": "shop/web"}, "workflow_run": {"conclusion": "failure", "head_branch": "main", "name": "tests"}})
    assert failed["conclusion"] == "failure" and failed["branch"] == "main"
    running = webhook_facts("check_suite", {"action": "requested", "repository": {"full_name": "shop/web"}, "check_suite": {"conclusion": None}})
    assert "conclusion" not in running

    pr = watch("pr", provider="github", repo="web", conclusion="merged")
    assert webhook_matches(pr, {"provider": "github", "event": "pull_request", **merged}) is not None, "the short repository name will do"
    assert webhook_matches(pr, {"provider": "gitlab", "event": "pull_request", **merged}) is None
    ci = watch("ci", provider="github", conclusion="failure")
    assert "CI failure in shop/web on main" == webhook_matches(ci, {"provider": "github", "event": "workflow_run", **failed})
    assert webhook_matches(ci, {"provider": "github", "event": "check_suite", **running}) is None, "a run still going has no conclusion"
    assert webhook_matches(ci, {"provider": "github", "event": "check_run", "conclusion": "failure"}) is None, "one per job would repeat the failure"


def test_patterns_are_bounded_before_they_are_stored() -> None:
    assert check_regex(r"FAIL(ED)?: \w+", 200) == r"FAIL(ED)?: \w+"
    for pattern, reason in [("(a+)+$", "nests a quantifier"), ("(", "does not compile"), ("x" * 201, "at most 200"), ("(?=x)y", "lookaround"), (r"(a)\1", "back-reference"), ("", "empty")]:
        with pytest.raises(WatchRefused, match=reason):
            check_regex(pattern, 200)


# -- setting one -----------------------------------------------------------------------------------------


async def test_setting_a_watch_checks_every_part_and_the_project_limit(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        project = await r.orch.enable(r.project.id)
        await r.manager.staff.hire(r.project.id, name="Ada")
        refusals = [
            ({"event": "nope"}, {"action": "wake"}, "when.event is one of"),
            ({"event": "staff_finished", "staff": "Zed"}, {"action": "wake"}, "nobody called 'Zed'"),
            ({"event": "staff_silent", "staff": "Ada"}, {"action": "wake"}, "needs minutes"),
            ({"event": "task_moved", "task": "t404"}, {"action": "wake"}, "no task t404"),
            ({"event": "terminal_output", "terminal": "x", "regex": "ok"}, {"action": "wake"}, "terminals service"),
            ({"event": "terminal_output", "terminal": "x", "regex": "(a*)*"}, {"action": "wake"}, "nests a quantifier"),
            ({"event": "ci", "provider": "github"}, {"action": "wake"}, "no webhook provider 'github'"),
            ({"event": "staff_finished"}, {"action": "shout"}, "then.action is one of"),
            ({"event": "staff_finished"}, {"action": "tell", "staff": "Ada"}, "needs a text"),
            ({"event": "staff_finished"}, {"action": "tell", "staff": "Ada", "text": "x", "when": "queue"}, "then.when is one of now, after_turn, interrupt"),
            ({"event": "staff_finished"}, {"action": "notify", "title": "x", "level": "loud"}, "level is one of"),
        ]
        for when, then, reason in refusals:
            with pytest.raises(WatchRefused, match=reason):
                await keeper.create(project, when=when, then=then)
        with pytest.raises(WatchRefused, match="cooldown is between 1 and"):
            await keeper.create(project, when={"event": "staff_finished"}, then={"action": "wake"}, cooldown_minutes=0.5)
        r.manager.config.watches.max_per_project = 2
        await keeper.create(project, when={"event": "staff_finished", "staff": "ada"}, then={"action": "wake"})
        second = await keeper.create(project, when={"event": "staff_question"}, then={"action": "notify", "title": "A question"}, by="operator")
        with pytest.raises(WatchRefused, match="already has 2 watches"):
            await keeper.create(project, when={"event": "staff_crashed"}, then={"action": "wake"})
        # Switched off, it no longer counts; switching it on again is held to the same limit.
        await keeper.update(r.project.id, second.id, enabled=False)
        await keeper.create(project, when={"event": "staff_crashed"}, then={"action": "wake"})
        with pytest.raises(WatchRefused, match="already has 2 watches"):
            await keeper.update(r.project.id, second.id, enabled=True)
        first = keeper.of_project(r.project.id)[0]
        assert first.pattern == {"event": "staff_finished", "staff": "Ada", "staff_id": first.pattern["staff_id"]}
        assert first.view()["describe"] == "when Ada finishes a turn → wake the orchestrator"
        # The orchestrator's state lists what it waits for.
        state = await r.orch.project_state(await r.refreshed(), session_id=project.settings.orchestrator.session_id)
        assert f"[{first.id}] when Ada finishes a turn" in state
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


# -- the bounds ------------------------------------------------------------------------------------------


async def test_cooldown_once_and_the_hourly_budget(settings: Settings, db: Database, tmp_path: Path) -> None:
    clock = Clock()
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r, clock)
    try:
        project = await r.orch.enable(r.project.id)
        ada = await r.manager.staff.hire(r.project.id, name="Ada")
        every = await keeper.create(project, when={"event": "staff_finished", "staff": "Ada"}, then={"action": "notify", "title": "Ada is done"}, cooldown_minutes=5)
        once = await keeper.create(project, when={"event": "staff_finished"}, then={"action": "wake"}, once=True)

        async def finish() -> None:
            await keeper.on_event(await r.manager.bus.publish("staff.status", {"status": "turn_done_unseen", "previous": "working"}, project_id=r.project.id, staff_id=ada.id))

        await finish()
        assert [(e.payload["watch_id"], e.payload["action"]) for e in await fired(r)] == [(every.id, "notify"), (once.id, "wake")]
        notes = r.team.app.notifications.posted
        assert notes[-1].title == "Bakery: Ada is done" and "Ada finished a turn" in notes[-1].body and notes[-1].source == f"watch:{every.id}"
        assert not keeper.get(r.project.id, once.id).enabled  # type: ignore[union-attr]
        await finish()
        assert len(await fired(r)) == 2, "within the cooldown, and the one-off is spent"
        clock.advance(minutes=5, seconds=1)
        await finish()
        assert len(await fired(r)) == 3

        # A tell that makes its member finish again goes round until the hourly budget stops it.
        r.team.runtimes["daedalus"] = runtime = ObservedFakeStaffRuntime(kind="daedalus", page=ReadPage("", None, False))
        r.team.runtimes["daedalus"].manager = r.manager
        task_id = await board_task(r.manager, r.project, "Menu")
        await operator_assignment(r.team, ada, task_id)
        loop = await keeper.create(project, when={"event": "staff_finished", "staff": "Ada"}, then={"action": "tell", "staff": "Ada", "text": "Check it once more"}, cooldown_minutes=1)
        told = 0
        for _ in range(20):
            clock.advance(seconds=61)
            await finish()
            told = sum(1 for _, message in runtime.sent if message.text == "Check it once more")
        assert told == r.manager.config.watches.max_fires_per_hour == 12
        stopped = keeper.get(r.project.id, loop.id)
        assert stopped is not None and not stopped.enabled and stopped.view()["stopped"] == "budget" and "12 times within an hour" in stopped.view()["last_error"]
        entry = next(e for e in await r.manager.projects.journal(r.project.id) if e.kind == "watch")
        assert loop.id in entry.text and "switched itself off" in entry.text
        assert all(message.origin == "orchestrator" for _, message in runtime.sent), "a watch the orchestrator set speaks as it"
        assert {message.mode for _, message in runtime.sent} == {"now"}, "a watch's tell goes into the turn unless it says otherwise"
        later = await keeper.create(project, when={"event": "staff_finished", "staff": "Ada"}, then={"action": "tell", "staff": "Ada", "text": "Next, the prices", "when": "after_turn"})
        assert later.action["when"] == "after_turn"
        clock.advance(minutes=61)
        await finish()
        assert [m.mode for _, m in runtime.sent if m.text == "Next, the prices"] == ["after_turn"]
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


# -- the ticker ------------------------------------------------------------------------------------------


async def test_a_silence_fires_once_until_the_member_is_heard_again(settings: Settings, db: Database, tmp_path: Path) -> None:
    clock = Clock()
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r, clock)
    try:
        project = await r.orch.enable(r.project.id)
        r.team.runtimes["daedalus"] = ObservedFakeStaffRuntime(kind="daedalus")
        r.team.runtimes["daedalus"].manager = r.manager
        ada = await r.manager.staff.hire(r.project.id, name="Ada", isolation="shared")
        await operator_assignment(r.team, ada, await board_task(r.manager, r.project, "Menu"))
        live = await r.team.live_of(ada)
        assert live is not None
        await r.team.ingress.status(live, "working")
        await db.execute("UPDATE staff_sessions SET last_signal_at = ? WHERE id = ?", (clock.now.isoformat(), live.id))
        silent = await keeper.create(project, when={"event": "staff_silent", "staff": "Ada", "minutes": 5}, then={"action": "wake"}, cooldown_minutes=1)
        clock.advance(minutes=4)
        await keeper.tick()
        assert await fired(r) == []
        clock.advance(minutes=2)
        await keeper.tick()
        [one] = await fired(r)
        assert one.payload["watch_id"] == silent.id and "Ada has been silent since" in one.payload["detail"] and one.staff_id == ada.id
        clock.advance(minutes=30)
        await keeper.tick()
        assert len(await fired(r)) == 1, "one silence, one fire"
        await db.execute("UPDATE staff_sessions SET last_signal_at = ? WHERE id = ?", (clock.now.isoformat(), live.id))
        clock.advance(minutes=6)
        await keeper.tick()
        assert len(await fired(r)) == 2
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


async def test_a_new_commit_is_seen_on_the_next_look_and_not_before(settings: Settings, db: Database, tmp_path: Path) -> None:
    clock = Clock()
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r, clock)
    try:
        project = await r.orch.enable(r.project.id)
        on_main = await keeper.create(project, when={"event": "git_commit", "branch": "main"}, then={"action": "wake"}, cooldown_minutes=1)
        anywhere = await keeper.create(project, when={"event": "git_commit", "folder": r.project.primary.id}, then={"action": "wake"}, cooldown_minutes=1)
        await keeper.tick()
        assert await fired(r) == [], "the first look is the baseline"
        git(r.repo, "checkout", "-q", "-b", "menu")
        (r.repo / "menu.md").write_text("menu\n")
        git(r.repo, "add", "-A")
        git(r.repo, "commit", "-qm", "Add the menu")
        await keeper.tick()
        assert await fired(r) == [], "not due: the folder was read less than a poll ago"
        clock.advance(seconds=r.manager.config.watches.git_poll_seconds)
        await keeper.tick()
        [seen] = await fired(r)
        assert seen.payload["watch_id"] == anywhere.id and "menu:" in seen.payload["detail"] and "Add the menu" in seen.payload["detail"]
        git(r.repo, "checkout", "-q", "main")
        git(r.repo, "merge", "-q", "--no-ff", "-m", "Merge the menu", "menu")
        clock.advance(seconds=r.manager.config.watches.git_poll_seconds)
        await keeper.tick()
        assert {e.payload["watch_id"] for e in (await fired(r))[1:]} == {on_main.id, anywhere.id}
        with pytest.raises(WatchRefused, match="no folder"):
            await keeper.create(project, when={"event": "git_commit", "folder": "elsewhere"}, then={"action": "wake"})
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


# -- webhooks ------------------------------------------------------------------------------------------


async def test_webhooks_fire_pr_ci_and_pattern_watches_and_can_skip_the_session(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        r.manager.config.webhooks["github"] = WebhookConfig(secret="s3cret", scheme="github", deliver="events")
        project = await r.orch.enable(r.project.id)
        merged = await keeper.create(project, when={"event": "pr", "provider": "github", "repo": "shop/web", "conclusion": "merged"}, then={"action": "wake"})
        red = await keeper.create(project, when={"event": "ci", "provider": "github", "conclusion": "failure"}, then={"action": "notify", "title": "CI is red", "level": "urgent"})
        deploy = await keeper.create(project, when={"event": "webhook", "provider": "github", "regex": "deploy(ed)? to prod"}, then={"action": "wake"})

        app = SimpleNamespace(settings=settings, config=r.manager.config, db=db, manager=r.manager, front=None, extensions=r.team.app.extensions, guard=None, notifications=None)
        app.extensions["inbound"] = Inbound(app)
        session_count = (await db.fetchone("SELECT count(*) FROM sessions"))[0]
        api = build_app(app, "tok")  # type: ignore[arg-type]

        async def post(event: str, payload: dict[str, Any], delivery: str) -> httpx.Response:
            raw = json.dumps(payload).encode()
            signature = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
            headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": signature, "Content-Type": "application/json"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
                return await client.post("/webhooks/github", content=raw, headers=headers)

        answer = await post("pull_request", {"action": "closed", "repository": {"full_name": "shop/web"}, "pull_request": {"merged": True, "title": "Menu page"}}, "d1")
        assert answer.status_code == 200 and answer.json()["delivered"] == "events", answer.text
        await post("workflow_run", {"action": "completed", "repository": {"full_name": "shop/web"}, "workflow_run": {"conclusion": "failure", "head_branch": "main"}}, "d2")
        await post("deployment_status", {"deployment_status": {"description": "deployed to prod"}, "repository": {"full_name": "shop/web"}}, "d3")
        await post("workflow_run", {"action": "completed", "repository": {"full_name": "shop/web"}, "workflow_run": {"conclusion": "success"}}, "d4")
        received = await events(r.manager, "webhook.received")
        assert [e.payload["event"] for e in received] == ["pull_request", "workflow_run", "deployment_status", "workflow_run"]
        assert received[0].payload["conclusion"] == "merged" and received[0].project_id is None
        for event in received:
            await keeper.on_event(event)
        assert [e.payload["watch_id"] for e in await fired(r)] == [merged.id, red.id, deploy.id]
        assert r.team.app.notifications.posted[-1].level == "urgent"
        assert (await db.fetchone("SELECT count(*) FROM sessions"))[0] == session_count
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


# -- terminals -----------------------------------------------------------------------------------------


async def test_terminal_output_is_waited_for_on_the_daemon_and_the_orchestrators_echo_is_skipped(settings: Settings, db: Database, tmp_path: Path) -> None:
    clock = Clock()
    r = await rig(settings, db, tmp_path)
    terminals = FakeTerminals()
    r.team.app.extensions["terminals"] = terminals
    keeper = await with_watches(r, clock)
    try:
        project = await r.orch.enable(r.project.id)
        sid = project.settings.orchestrator.session_id
        r.team.runtimes["claude"] = ObservedFakeStaffRuntime(kind="claude")
        r.team.runtimes["claude"].manager = r.manager
        max_ = await r.manager.staff.hire(r.project.id, name="Max", harness="claude", isolation="worktree")
        await operator_assignment(r.team, max_, await board_task(r.manager, r.project, "Tests"))
        live = await r.team.live_of(max_)
        assert live is not None and live.session.terminal_id
        await db.execute(
            "UPDATE terminals SET title = 'claude · Max', cwd = '/tmp', created_at = ? WHERE id = ?",
            (clock.now.isoformat(), live.session.terminal_id),
        )
        with pytest.raises(WatchRefused, match="works without a terminal"):
            await keeper.create(project, when={"event": "terminal_output", "staff": (await r.manager.staff.hire(r.project.id, name="Ada")).name, "regex": "x"}, then={"action": "wake"})

        said = await r.call(sid, "watch", when={"event": "terminal_output", "staff": "Max", "regex": "FAILED \\w+"}, then={"action": "wake"}, cooldown_minutes=2, client_operation_id="terminal-watch-tool")
        watch_id = said.split()[1]
        await until_await(lambda: _asked(terminals, 1), "the watch waits on Max's terminal")
        assert terminals.calls[0] == {"terminal_id": live.session.terminal_id, "regex": "FAILED \\w+", "scope": "output", "since_seq": None, "timeout": 1500.0}

        # The orchestrator typed the words itself a moment ago: the terminal shows them, and that is not news.
        await r.call(sid, "tell", staff="Max", text="Look at FAILED test_menu first")
        await terminals.answers.put({"matched": "regex", "seq": 40, "match": "FAILED test_menu"})
        await until_await(lambda: _asked(terminals, 2), "the echo was skipped and the wait went on")
        assert terminals.calls[1]["since_seq"] == 40 and await fired(r) == []

        clock.advance(minutes=3)
        await terminals.answers.put({"matched": "timeout", "seq": 55})
        await until_await(lambda: _asked(terminals, 3), "a timeout waits again from where it stopped")
        assert terminals.calls[2]["since_seq"] == 55
        await terminals.answers.put({"matched": "regex", "seq": 60, "match": "FAILED test_prices"})

        async def one_fire() -> bool:
            return len(await fired(r)) == 1

        await until_await(one_fire, "the terminal match fired the watch")
        [event] = await fired(r)
        assert event.payload["watch_id"] == watch_id and "FAILED test_prices" in event.payload["detail"] and event.staff_id == max_.id

        # The daemon refuses a pattern the host let through: the watch stops and the journal says why.
        clock.advance(minutes=3)
        await until_await(lambda: _asked(terminals, 4), "re-armed once the cooldown was over")
        assert terminals.calls[3]["since_seq"] is None, "what was printed during the cooldown is not waited for"
        await terminals.answers.put(InvalidRequest("regex: missing closing )"))

        async def stopped() -> bool:
            found = keeper.get(r.project.id, watch_id)
            return found is not None and not found.enabled

        await until_await(stopped, "the refused pattern stopped the watch")
        view = keeper.get(r.project.id, watch_id).view()  # type: ignore[union-attr]
        assert view["stopped"] == "pattern" and "refused its pattern" in view["last_error"]
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


async def _asked(terminals: FakeTerminals, n: int) -> bool:
    await asyncio.sleep(0)
    return len(terminals.calls) >= n


# -- the office ------------------------------------------------------------------------------------------


async def test_a_watch_wakes_the_orchestrator_once_per_cooldown_and_outlives_a_replacement(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The acceptance: ``Watch({"event": "staff_finished", "staff": "Max"}, {"action": "wake"})`` wakes the
    orchestrator when Max's turn ends, once per cooldown."""
    script = [
        {"tool": "Watch", "args": {"when": {"event": "staff_finished", "staff": "Max"}, "then": {"action": "wake", "note": "review Max's work"}, "cooldown_minutes": 10}},
        {"text": "I will look when Max is done."},
        {"text": "Max is done; reading his reply."},
    ]
    clock = Clock()
    r = await rig(settings, db, tmp_path, script)
    keeper = await with_watches(r, clock)
    try:
        r.manager.config.orchestrator.batch_seconds = 600
        r.team.runtimes["daedalus"] = ObservedFakeStaffRuntime(kind="daedalus", page=ReadPage("Menu done.", None, False))
        r.team.runtimes["daedalus"].manager = r.manager
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        max_ = await r.manager.staff.hire(r.project.id, name="Max", isolation="shared")
        await r.manager.submit(sid, "Tell me when Max is done")
        await until_await(lambda: _idle(r.manager, sid), "the orchestrator set its watch")
        [made] = keeper.of_project(r.project.id)
        assert made.created_by == "orchestrator" and made.action == {"action": "wake", "note": "review Max's work"}

        await operator_assignment(r.team, max_, await board_task(r.manager, r.project, "Menu"))
        live = await r.team.live_of(max_)
        assert live is not None

        async def finish() -> None:
            await r.team.ingress.status(live, "working")
            await r.team.ingress.status(live, "turn_done_unseen")

        await finish()
        await until_await(lambda: _fired_count(r, 1), "the watch fired")

        async def woken() -> bool:
            return bool(await events_messages(r.manager, sid)) and await _idle(r.manager, sid)

        await until_await(woken, "the watch woke the orchestrator")
        [batch] = await events_messages(r.manager, sid)
        assert f"watch [{made.id}] fired: Max finished a turn — review Max's work" in batch
        await finish()
        await asyncio.sleep(0.05)
        assert len(await fired(r)) == 1, "within the cooldown it does not fire again"

        new = (await r.orch.replace(r.project.id, "fresh context")).settings.orchestrator.session_id
        assert keeper.get(r.project.id, made.id).enabled  # type: ignore[union-attr]
        clock.advance(minutes=11)
        await finish()
        await until_await(lambda: _fired_count(r, 2), "it fired again after the cooldown")
        wake = await r.orch.classify(r.project.id, (await fired(r))[-1])
        assert wake is not None and wake.urgent and new != sid
        with pytest.raises(Refused, match="has no wake-up or watch"):
            await r.call(new, "unwatch", id="w0000000", client_operation_id="remove-missing-watch")
        assert await r.call(new, "unwatch", id=made.id, client_operation_id="remove-existing-watch") == f"watch {made.id} removed"
        assert keeper.of_project(r.project.id) == []
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


async def _fired_count(r: Rig, n: int) -> bool:
    return len(await fired(r)) >= n


# -- the routes ------------------------------------------------------------------------------------------


async def test_the_watch_routes(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r)
    try:
        await r.manager.staff.hire(r.project.id, name="Ada")
        app = SimpleNamespace(settings=settings, config=r.manager.config, db=db, manager=r.manager, front=None, extensions=r.team.app.extensions, guard=None, notifications=None)
        api = build_app(app, "tok")  # type: ignore[arg-type]
        headers = {"X-Daedalus-Token": "tok"}
        base = f"/api/projects/{r.project.id}/watches"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            # Without an orchestrator there is nobody to wake, but the operator can still be told.
            initial = (await client.get(base, headers=headers)).json()
            woken = await client.post(base, json={"when": {"event": "staff_finished"}, "then": {"action": "wake"},
                                                  "expected_collection_revision": initial["collection_revision"],
                                                  "client_operation_id": "watch-no-coordinator"}, headers=headers)
            assert woken.status_code == 400 and "no orchestrator" in woken.json()["detail"]
            made = await client.post(base, json={"when": {"event": "staff_crashed", "staff": "Ada"}, "then": {"action": "notify", "title": "Ada crashed"}, "cooldown_minutes": 30, "note": "tell me",
                                                  "expected_collection_revision": initial["collection_revision"],
                                                  "client_operation_id": "watch-notify-create"}, headers=headers)
            assert made.status_code == 200, made.text
            body = made.json()
            assert (body["created_by"], body["cooldown_minutes"], body["enabled"], body["note"]) == ("operator", 30, True, "tell me")
            listed = (await client.get(base, headers=headers)).json()
            assert [w["id"] for w in listed["watches"]] == [body["id"]] and listed["max"] == 50 and listed["min_cooldown_minutes"] == 1
            off = await client.patch(f"{base}/{body['id']}", json={"enabled": False,
                                "expected_entity_revision": listed["project_entity_revision"],
                                "expected_condition_revision": body["condition_revision"],
                                "client_operation_id": "watch-disable"}, headers=headers)
            assert off.status_code == 200 and off.json()["enabled"] is False
            assert (await client.patch(f"{base}/nope", json={"enabled": False,
                                "expected_entity_revision": off.json()["entity_revision"],
                                "expected_condition_revision": 1,
                                "client_operation_id": "watch-missing"}, headers=headers)).status_code == 404
            assert (await client.patch(f"{base}/{body['id']}", json={"cooldown_minutes": 0,
                                "expected_entity_revision": off.json()["entity_revision"],
                                "expected_condition_revision": off.json()["condition_revision"],
                                "client_operation_id": "watch-invalid-cooldown"}, headers=headers)).status_code == 400
            deleted = await client.request("DELETE", f"{base}/{body['id']}", json={
                "expected_entity_revision": off.json()["entity_revision"],
                "expected_condition_revision": off.json()["condition_revision"],
                "client_operation_id": "watch-delete"}, headers=headers)
            assert deleted.json()["deleted"] is True
            assert (await client.request("DELETE", f"{base}/{body['id']}", json={
                "expected_entity_revision": deleted.json()["entity_revision"],
                "expected_condition_revision": off.json()["condition_revision"],
                "client_operation_id": "watch-delete-again"}, headers=headers)).status_code == 404
        changes = [e.payload for e in await events(r.manager, "project.changed") if e.payload.get("change") == "watches"]
        assert [c["actor"] for c in changes] == ["operator", "operator", "operator"]
        assert keeper.of_project(r.project.id) == []
    finally:
        await keeper.close()
        await close_team(r.manager)
        await r.manager.close()


async def test_a_restart_replays_what_it_missed_without_firing_twice(settings: Settings, db: Database, tmp_path: Path) -> None:
    clock = Clock()
    r = await rig(settings, db, tmp_path)
    keeper = await with_watches(r, clock)
    try:
        project = await r.orch.enable(r.project.id)
        ada = await r.manager.staff.hire(r.project.id, name="Ada")
        made = await keeper.create(project, when={"event": "staff_finished", "staff": "Ada"}, then={"action": "notify", "title": "done"})
        first = await r.manager.bus.publish("staff.status", {"status": "turn_done_unseen", "previous": "working"}, project_id=r.project.id, staff_id=ada.id)
        await until_await(lambda: _fired_count(r, 1), "the live event fired the watch")
        await keeper.tick()
        cursor = await db.kv_get("watches_cursor")
        assert isinstance(cursor, int) and cursor >= first.seq
        await keeper.close()
        await r.manager.bus.publish("staff.status", {"status": "turn_done_unseen", "previous": "working"}, project_id=r.project.id, staff_id=ada.id)
        clock.advance(minutes=11)
        again = Watches(r.team.app)
        again.clock = clock
        await again.start()
        try:
            await until_await(lambda: _fired_count(r, 2), "the missed event fired it after the restart")
            await asyncio.sleep(0.05)
            assert len(await fired(r)) == 2, "the event it had already handled was not handled again"
            assert again.get(r.project.id, made.id).fire_count == 2  # type: ignore[union-attr]
        finally:
            await again.close()
    finally:
        await close_team(r.manager)
        await r.manager.close()
