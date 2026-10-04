"""A project's orchestrator: its office, what it may call, what it sees, what wakes it, and its project tools."""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.tool_registry import policy_admits
from protocore.contracts.types import MessageRole, TextBlock

from daedalus.config import ORCHESTRATOR_ONLY_TOOLS, ORCHESTRATOR_TOOLS, ModelPresetConfig, Settings
from daedalus.extensions.api import build_app
from daedalus.extensions.board import Board
from daedalus.extensions.coordinator_authority import approve_authority, install_authority
from daedalus.extensions.orchestrator import NotCurrent, Orchestrators, fit
from daedalus.extensions.orchestrator_domain import apply_goal_revision
from daedalus.extensions.orchestrator_ops import Refused
from daedalus.extensions.staff import Team
from daedalus.host import prompts
from daedalus.host.events import AppEvent, EventFilter
from daedalus.host.session_runner import SessionManager
from daedalus.providers.openai_compat import ProviderEndpoint
from daedalus.staff_runtime import ReadPage
from daedalus.stores.control import ControlStore, Entity, Principal, Scope
from daedalus.stores.coordinator_handoff_schema import MIGRATION as COORDINATOR_HANDOFF_MIGRATION
from daedalus.stores.database import Database
from daedalus.stores.outbox import OutboxStore
from daedalus.stores.projects import Project, ProjectError
from tests.support.authorized_launch import operator_assignment
from tests.support.models import DEFAULT_PRESET, FALLBACK_PRESET, VISION_PRESET
from tests.support.waiting import until_await
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import (
    BRIEF,
    ObservedFakeStaffRuntime,
    board_task,
    close_team,
    project_with,
    repository,
    team_for,
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@dataclass
class Rig:
    manager: SessionManager
    provider: ScriptedProvider
    team: Team
    board: Board
    orch: Orchestrators
    project: Project
    repo: Path

    async def refreshed(self) -> Project:
        found = await self.manager.projects.get(self.project.id)
        assert found is not None
        return found

    async def call(self, session_id: str, operation: str, /, *, wait_for_admission: bool = True, **kwargs: Any) -> Any:
        if operation in ("tasks", "assign", "require") and (operation in ("assign", "require") or kwargs.get("op") in
                                                   ("create", "update", "move")):
            kwargs.setdefault("client_operation_id", f"fixture:{uuid.uuid4().hex}")
            task_id = kwargs.get("task_id")
            scope = Scope("project", self.project.id)
            entity = Entity("task", task_id) if task_id and kwargs.get("op") != "create" else Entity("collection", self.project.id)
            field = "expected_entity_revision" if entity.kind == "task" else "expected_collection_revision"
            try:
                revision = await ControlStore(self.manager.db).revision(scope, entity)
            except KeyError:
                revision = 1  # The production command still reports the unknown task.
            kwargs.setdefault(field, revision)
        response = await self.orch.service(operation, session_id=session_id, **kwargs)
        if operation == "assign" and wait_for_admission:
            action = await self.manager.db.fetchone(
                "SELECT e.id FROM effect_outbox e JOIN operation_receipts r ON r.id = e.receipt_id"
                " WHERE r.client_operation_id = ? AND r.scope_id = ? AND r.actor_id = ?",
                (kwargs["client_operation_id"] + ":launch", self.project.id, f"orchestrator:{session_id}"),
            )
            assert action is not None, "assignment must leave its durable launch command"

            async def settled() -> bool:
                view = await OutboxStore(self.manager.db).view(action["id"])
                if view["state"] in ("failed", "unknown", "cancelled"):
                    raise AssertionError(f"fixture assignment was not admitted: {view}")
                return view["state"] == "completed" or view.get("wait_reason") is not None

            await until_await(settled, "the assignment was admitted or deferred for an observed reason")
        return response


async def rig(settings: Settings, db: Database, tmp_path: Path, script: list[dict[str, Any]] | None = None) -> Rig:
    if await db.fetchone("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'coordinator_handoffs'") is None:
        await db.conn.executescript(COORDINATOR_HANDOFF_MIGRATION)
    provider = ScriptedProvider(script or [])
    provider.endpoint = ProviderEndpoint(id="scripted", kind="llamacpp", base_url="http://127.0.0.1:1")
    manager = await _manager(settings, db, provider)
    # An orchestrator always runs a preset, so the scripted model answers for any of them.
    manager.providers.rungs_for = lambda config, preset=None: [(provider, "scripted-model")]  # type: ignore[method-assign]
    team = await team_for(settings, manager)
    app = team.app
    app.config = manager.config
    board = Board(app)
    app.extensions["board"] = board
    install_authority(app)
    manager.service_hooks["board"] = board.service
    orch = Orchestrators(app)
    app._config_lock = asyncio.Lock()

    async def listed(_base_url: str, _api_key: str | None) -> dict[str, Any]:
        return {"base_url": "http://127.0.0.1:1/v1", "models": [preset.model for preset in manager.config.presets.values()]}

    orch.catalogue_lookup = listed
    app.extensions["orchestrator"] = orch
    orch.attach()
    repo = repository(tmp_path)
    project = await project_with(manager, repo, orchestrator=False)
    return Rig(manager, provider, team, board, orch, project, repo)


async def events(manager: SessionManager, *types: str, **ids: str) -> list[AppEvent]:
    return await manager.bus.replay(0, EventFilter(types=types, **ids), limit=5000)


async def events_messages(manager: SessionManager, session_id: str) -> list[str]:
    out = []
    for message in await manager.sessions.list_transcript(session_id):
        if message.role is MessageRole.user and message.metadata.get("daedalus.origin") == "events":
            out.append(prompts.without_turn_context("".join(b.text for b in message.content_blocks if isinstance(b, TextBlock))))
    return out


async def set_goal(r: Rig, body: str) -> None:
    """Revise the goal through the operator's project CAS and durable scope record."""
    scope = Scope("project", r.project.id)
    control = ControlStore(r.manager.db)
    principal = Principal.operator({"via": "token", "user_id": 1})
    revision = await control.revision(scope, Entity("project", r.project.id))
    row = await r.manager.db.fetchone("SELECT goal_revision FROM projects WHERE id = ?", (r.project.id,))
    assert row is not None

    async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
        return await apply_goal_revision(conn, project_id=r.project.id,
                                         expected_goal_revision=row["goal_revision"], body=body,
                                         root_task_ids=[], origin_kind="operator", origin_ref="fixture",
                                         control=control, principal=principal)

    await control.mutate(principal, scope, "goal.revise", f"fixture-goal:{uuid.uuid4().hex}",
                         revision, Entity("project", r.project.id), {"body": body,
                         "expected_goal_revision": row["goal_revision"], "root_task_ids": []}, effect)


async def approve_coordinator(r: Rig, session_id: str) -> None:
    """Issue the fixture coordinator narrow grants from the operator."""
    operator = Principal.operator({"via": "token", "user_id": 1})
    scope = Scope("project", r.project.id)
    expires_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    for bundle_id in ("planning", "execution_project", "review"):
        revision = await ControlStore(r.manager.db).revision(scope, Entity("project", r.project.id))
        await approve_authority(r.team.app, r.project.id, operator,
                                client_operation_id=f"fixture-grant:{uuid.uuid4().hex}",
                                expected_entity_revision=revision,
                                expected_coordinator_session_id=session_id, bundle_id=bundle_id,
                                expires_at=expires_at)


def last_user_text(provider: ScriptedProvider, index: int = -1) -> str:
    request = provider.requests[index]
    user = [m for m in request.messages if m.role is MessageRole.user][-1]
    return "".join(b.text for b in user.content_blocks if isinstance(b, TextBlock))


# -- the office --------------------------------------------------------------------------------------


async def test_the_orchestrator_is_given_its_allowlist_and_nobody_else_its_tools(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "Nothing to do yet."}])
    try:
        project = await r.orch.enable(r.project.id)
        sid = project.settings.orchestrator.session_id
        state = await r.manager.get_state(sid)
        assert state is not None and r.manager.is_orchestrator(state)
        known = {t.name for t in r.manager.tools.list_all()}
        policy = r.manager.tool_policy_for(state)
        assert {name for name in known if policy_admits(policy, name)} == known & set(ORCHESTRATOR_TOOLS)
        assert {"Exec", "Write", "Read", "SubAgent", "AskUser", "BoardAdd"} <= r.manager.blocked_tools_for(state)
        ordinary = await r.manager.create_session("work", project_id=r.project.id)
        assert known & set(ORCHESTRATOR_ONLY_TOOLS) <= r.manager.blocked_tools_for(ordinary)
        with pytest.raises(NotCurrent, match="not a project's orchestrator"):
            await r.call(ordinary.session.id, "brief")

        await r.manager.submit(sid, "Anything for me?")
        await until_await(lambda: _idle(r.manager, sid), "the orchestrator's turn ended")
        request = r.provider.requests[0]
        assert sorted(t.name for t in request.tools or []) == sorted(known & set(ORCHESTRATOR_TOOLS))
        system = "".join(b.text for m in request.messages if m.role is MessageRole.system for b in m.content_blocks if isinstance(b, TextBlock))
        assert "You are the orchestrator of one project" in system and "You are Daedalus" not in system
        assert r.project.name not in system, "the static prompt names no project, so every orchestrator shares its cache"
        # Its turn is bounded by its own limits, without a mode anyone had to set.
        mode = r.manager.mode_for(state)
        assert mode is not None and mode.max_iterations == r.manager.config.orchestrator.max_iterations
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def _idle(manager: SessionManager, session_id: str) -> bool:
    """The session answered and its run is over: the reply is written and nothing runs."""
    state = manager.live_state(session_id)
    if state is None or state.running or not state.settled.is_set():
        return False
    return any(m.role is MessageRole.assistant for m in await manager.sessions.list_transcript(session_id))


async def test_two_enables_at_once_make_one_orchestrator_from_the_installation_defaults(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        r.manager.config.orchestrator.default_concurrency = 4
        r.manager.config.orchestrator.default_concurrency_cap = 8
        first, second = await asyncio.gather(r.orch.enable(r.project.id), r.orch.enable(r.project.id))
        assert first.settings.orchestrator.session_id == second.settings.orchestrator.session_id != ""
        rows = await db.fetchall("SELECT id FROM sessions WHERE json_extract(metadata, '$.orchestrator_of') = ?", (r.project.id,))
        assert [row["id"] for row in rows] == [first.settings.orchestrator.session_id]
        settings_now = (await r.refreshed()).settings.orchestrator
        assert (settings_now.enabled, settings_now.concurrency, settings_now.concurrency_cap) == (True, 4, 8)
        assert [e.kind for e in await r.manager.projects.journal(r.project.id)] == ["orchestrator"]
        [changed] = await events(r.manager, "project.changed")
        assert changed.payload == {"change": "orchestrator.enabled", "actor": "operator"}
        with pytest.raises(ProjectError, match="between 1 and 32"):
            await r.orch.update(r.project.id, concurrency_cap=33)
        # A staff project that is a chat's scratch cannot have one.
        scratch = await r.manager.create_session("chat")
        assert scratch.project is not None
        with pytest.raises(ProjectError, match="scratch"):
            await r.orch.enable(scratch.project.id)
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_replacement_links_both_ways_retargets_wake_ups_and_retires_the_old_tools(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        old = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO schedules(id, name, prompt, workspace, created_at, target_session, enabled, next_run_at, kind) VALUES ('w1', 'wake', 'check the webhooks', '', ?, ?, 1, ?, 'wake')",
            (now, old, now),
        )
        new = (await r.orch.replace(r.project.id, "it stopped answering")).settings.orchestrator.session_id
        assert new and new != old
        old_state, new_state = await r.manager.get_state(old), await r.manager.get_state(new)
        assert old_state is not None and new_state is not None
        assert old_state.metadata["orchestrator_retired_of"] == r.project.id and old_state.metadata["successor"] == new
        assert "orchestrator_of" not in old_state.metadata
        assert new_state.metadata["predecessor"] == old
        row = await db.fetchone("SELECT target_session FROM schedules WHERE id = 'w1'")
        assert row is not None and row["target_session"] == new
        with pytest.raises(NotCurrent, match=f"replaced by {new}"):
            await r.call(old, "journal", text="still here")
        replacement = next(e for e in await r.manager.projects.journal(r.project.id) if e.kind == "replacement")
        assert old in replacement.text and new in replacement.text and "it stopped answering" in replacement.text
        # The successor hears of its predecessor in its first state block, and only there.
        first = await r.orch.turn_notes(new_state)
        assert first is not None and f"You replace the orchestrator session {old}: it stopped answering" in first
        again = await r.orch.turn_notes(new_state)
        assert again is not None and "You replace" not in again
        retired = await r.orch.turn_notes(old_state)
        assert retired is not None and f"replaced by {new}" in retired and "Project:" not in retired
        # The state lists the wake-up that moved with the office.
        assert "[w1]" in (await r.orch.project_state(await r.refreshed(), session_id=new))

        # Switched off: its tools refuse, and what staff asked it goes to the operator.
        ada = await r.manager.staff.hire(r.project.id, name="Ada")
        ask = await r.manager.asks.open(r.project.id, origin="staff", kind="question", text="Which colour?", routed_to="orchestrator", staff_id=ada.id)
        await r.orch.disable(r.project.id)
        with pytest.raises(NotCurrent, match="switched off"):
            await r.call(new, "brief")
        moved = await r.manager.asks.get(ask.id)
        assert moved is not None and moved.routed_to == "operator"
        assert r.orch.queues == {}
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- what it sees --------------------------------------------------------------------------------------


async def test_the_state_block_shows_the_project_and_stays_within_its_bound(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        project = await r.orch.enable(r.project.id, autonomy="normal")
        await set_goal(r, "A bakery site with online orders")
        await r.manager.projects.set_brief(r.project.id, "allowed_without_operator", "npm install of listed packages", "operator")
        for name in ("Ira", "Max", "Naya"):
            await r.manager.staff.hire(r.project.id, name=name, role=f"{name} work")
        for n in range(25):
            await board_task(r.manager, project, f"Task number {n}", priority=1 + n % 5)
        for n in range(8):
            await r.manager.projects.record(r.project.id, "orchestrator", "decision", f"decision {n}")
        full = await r.orch.project_state(await r.refreshed())
        assert full.startswith("Project: Bakery · default env container · autonomy normal · concurrency 1 of cap 10 · 0 working")
        assert "goals: A bakery site with online orders" in full and "allowed without the operator: npm install" in full
        assert all(name in full for name in ("Ira", "Max", "Naya"))
        assert "Board: doing 0 · review 0 · todo 25" in full and "more (Tasks)" in full
        assert "Journal (latest):" in full and "decision 7" in full and "decision 2" not in full
        assert "Spend today: orchestrator $0.00" in full
        r.manager.config.orchestrator.state_max_chars = 1200
        small = await r.orch.project_state(await r.refreshed())
        assert len(small) <= 1200 and small.startswith("Project: Bakery") and "more (" in small
    finally:
        await close_team(r.manager)
        await r.manager.close()


def test_fitting_cuts_the_longest_list_first_and_counts_what_it_cut() -> None:
    sections = [(["Project: x"], ""), (["Team:", *(f"  - member {n}" for n in range(40))], "Team"), (["Journal:", "  - one", "  - two"], "Journal")]
    text = fit(sections, 300)
    assert len(text) <= 300 and "Project: x" in text and "  - one" in text
    cut = next(line for line in text.splitlines() if line.startswith("  - … "))
    shown = sum(1 for line in text.splitlines() if line.startswith("  - member"))
    assert int(cut.split()[2]) == 40 - shown and cut.endswith("(Team)")
    assert len(fit([(["x" * 500], "")], 300)) <= 300, "what cannot be cut by items is clipped within the bound"


async def test_compaction_of_an_orchestrator_is_told_what_to_keep(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "one"}, {"text": "two"}])
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        ordinary = await r.manager.create_session("work", project_id=r.project.id)
        told: dict[str, str] = {}

        async def capture(state: Any, history: Any, tail: Any, instructions: str, reason: str, *, own_task_ok: bool) -> str:
            told[state.session.id] = instructions
            return "summary"

        r.manager._compact_progressing = capture  # type: ignore[method-assign]
        for session_id in (sid, ordinary.session.id):
            await r.manager.submit(session_id, "hello")
            await until_await(lambda s=session_id: _idle(r.manager, s), "the turn ended")
            await r.manager.compact(session_id)
        assert told == {sid: prompts.ORCHESTRATOR_COMPACTION, ordinary.session.id: ""}
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- its model ------------------------------------------------------------------------------------------


async def test_the_model_is_the_projects_then_the_settings_default_then_the_strongest(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        config = r.manager.config
        assert config.strongest_preset() == DEFAULT_PRESET, "thinking, effort and window tie; the default preset wins the tie"
        config.presets["deep"] = ModelPresetConfig(provider="openrouter", model="some/large", reasoning_effort="xhigh")
        assert config.strongest_preset() == "deep"
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        state = await r.manager.get_state(sid)
        assert state is not None
        assert r.orch.preset_for(state) == "deep" and (await r.manager.live.load(sid))["preset"] == "deep"
        config.orchestrator.preset = VISION_PRESET
        assert r.orch.preset_for(state) == VISION_PRESET
        # The chip on the orchestrator's chat writes the project's setting, which then wins.
        assert await r.orch.model_chosen(sid, FALLBACK_PRESET)
        assert (await r.refreshed()).settings.orchestrator.model == FALLBACK_PRESET and r.orch.preset_for(state) == FALLBACK_PRESET
        assert await r.orch.model_chosen(sid, None, clear=True)
        assert (await r.refreshed()).settings.orchestrator.model == "" and r.orch.preset_for(state) == VISION_PRESET
        assert (await r.manager.live.load(sid))["preset"] == VISION_PRESET
        with pytest.raises(ProjectError, match="no model preset"):
            await r.orch.update(r.project.id, model="nope")
        ordinary = await r.manager.create_session("work", project_id=r.project.id)
        assert r.orch.preset_for(ordinary) is None and not await r.orch.model_chosen(ordinary.session.id, FALLBACK_PRESET)
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- its tools ------------------------------------------------------------------------------------------


async def test_brief_keeps_the_allowances_the_operators_and_a_host_folder_needs_the_operator(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        with pytest.raises(Refused, match="scope revision"):
            await r.call(sid, "brief", section="goals", body="Online orders")
        await set_goal(r, "Online orders")
        assert "Online orders" in await r.call(sid, "brief")
        with pytest.raises(Refused, match="operator's alone"):
            await r.call(sid, "brief", section="allowed_without_operator", body="everything")

        host = tmp_path / "host-side"
        answer = await r.call(sid, "folders", op="add", path=str(host), env="host", label="bot")
        assert "needs the operator's confirmation" in answer
        [ask] = await r.manager.asks.open_for(r.project.id, routed_to="operator")
        assert (ask.origin, ask.kind, ask.detail["path"]) == ("orchestrator", "folder", str(host))
        [pending] = await events(r.manager, "ask.pending")
        assert pending.session_id == sid and pending.payload["request_ref"] == f"orchestrator:{r.project.id}:{ask.id}"
        assert len((await r.refreshed()).folders) == 1, "nothing is added before the operator says so"
        result = await r.team.answer(ask.short_id, selected=["Add"], by="operator")
        assert result["delivered"] is True
        folders = (await r.refreshed()).folders
        assert [(str(f.path), f.env) for f in folders][1] == (str(host), "host")
        assert any("approved" in e.text for e in await r.manager.projects.journal(r.project.id))
        [answered] = await events(r.manager, "ask.answered")
        assert answered.payload["request_ref"] == pending.payload["request_ref"]

        docs = tmp_path / "docs"
        docs.mkdir()
        assert "added" in await r.call(sid, "folders", op="add", path=str(docs), readonly=True, label="docs")
        with pytest.raises(Refused, match="only the operator makes a read-only folder writable"):
            await r.call(sid, "folders", op="update", folder="docs", readonly=False)
        with pytest.raises(Refused, match="work in"):
            await r.call(sid, "folders", op="remove", folder=r.project.primary.id)
        assert "files are untouched" in await r.call(sid, "folders", op="remove", folder="docs")
        assert docs.is_dir()
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_peek_reads_inside_the_walls_bounded_and_never_writes(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        (r.repo / "big.txt").write_text("".join(f"line {n}\n" for n in range(5000)))
        assert "bakery" in await r.call(sid, "peek", op="read", path="README.md")
        page = await r.call(sid, "peek", op="read", path="big.txt", limit=1000)
        assert page.count("\n") <= 401 and "continue with offset=401" in page
        with pytest.raises(Refused, match="outside the project's folders"):
            await r.call(sid, "peek", op="read", path="../outside.txt")
        with pytest.raises(Refused, match="outside the project's folders"):
            await r.call(sid, "peek", op="read", path="/etc/hostname")
        assert "README.md" in await r.call(sid, "peek", op="ls")
        assert "big.txt" in await r.call(sid, "peek", op="find", pattern="*.txt")
        assert "start" in await r.call(sid, "peek", op="git_log")
        with pytest.raises(Refused, match="not a revision"):
            await r.call(sid, "peek", op="git_diff", ref="--output=/tmp/x")
        status = await r.call(sid, "peek", op="git_status")
        assert "big.txt" in status
        assert not (r.repo / ".git" / "index.lock").exists()
        with pytest.raises(Refused, match="no folder"):
            await r.call(sid, "peek", op="ls", folder="nowhere")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_tasks_journal_team_and_report_act_for_the_project(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        runtime = ObservedFakeStaffRuntime(kind="daedalus")
        runtime.manager = r.manager
        r.team.runtimes["daedalus"] = runtime
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        await approve_coordinator(r, sid)
        await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="shared")
        created = await r.call(sid, "tasks", op="create", title="Menu page", assignee="Ada", **BRIEF)
        assert "Menu page (Ada)" in created
        task_id = created.split()[0]
        assert "objective: Add a menu page" in await r.call(sid, "tasks", op="get", task_id=task_id)
        [assigned] = [e for e in await events(r.manager, "task.created") if e.payload["task_id"] == task_id]
        assert assigned.payload["actor"] == "agent"
        assert assigned.payload["actor_id"] == f"orchestrator:{sid}"
        assert "Ada" in await r.call(sid, "team")
        assert await r.team.live_of(await r.manager.staff.by_name(r.project.id, "Ada")) is None
        assert "concurrency is now 3 of 10" in await r.call(sid, "team", concurrency=3)
        with pytest.raises(Refused, match="cap of 10"):
            await r.call(sid, "team", concurrency=11)
        assert "#" in await r.call(sid, "journal", text="Ada takes the menu", why="she knows the prices")
        assert "Why: she knows the prices" in await r.call(sid, "journal", op="read")
        assert "reported" in await r.call(sid, "project_report", text="The menu is under way", kind="progress")
        draft = r.team.app.notifications.posted[-1]
        assert (draft.category, draft.source, draft.project_id, draft.session_id) == ("orchestrator_report", "project_report", r.project.id, sid)
        policy = await r.orch.notification_policy(r.project.id)
        assert policy.orchestrated and policy.orchestrator_session_id == sid and policy.hold_seconds == r.manager.config.notifications.orchestrator_hold_seconds
        await r.orch.update(r.project.id, autonomy="ask")
        assert (await r.orch.notification_policy(r.project.id)).hold_seconds == 0
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_the_tools_answer_from_a_real_turn(settings: Settings, db: Database, tmp_path: Path) -> None:
    script = [
        {"tool": "Folders", "args": {"op": "list"}},
        {"tool": "Peek", "args": {"op": "ls"}},
        {"tool": "AskOperator", "args": {"title": "Ship on Friday?", "text": "Ship on Friday?", "options": ["yes", "no"], "context": "the menu is ready"}},
        {"tool": "Journal", "args": {"op": "write", "text": "Asked about Friday", "why": "the release date is theirs"}},
        {"text": "Asked; nothing else to do."},
    ]
    r = await rig(settings, db, tmp_path, script)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        await r.manager.submit(sid, "Can we ship?")
        await until_await(lambda: _idle(r.manager, sid), "the turn ended")
        texts = [str(b.content) for m in r.provider.requests[-1].messages if m.role is MessageRole.tool for b in m.content_blocks if hasattr(b, "content")]
        assert len(texts) == 4 and not any('"error"' in t or "gets multiple values" in t for t in texts), texts
        assert r.project.primary.path.name in texts[0] and "README.md" in texts[1]
        assert texts[2].startswith("asked the operator as [q") and "journal entry" in texts[3]
        [ask] = await r.manager.asks.open_for(r.project.id)
        assert "Context: the menu is ready" in ask.text
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- wake-ups ---------------------------------------------------------------------------------------------


async def test_what_wakes_the_orchestrator_and_what_never_does(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        pid = r.project.id
        ada = await r.manager.staff.hire(pid, name="Ada")
        bus = r.manager.bus

        async def wake(event_type: str, payload: dict[str, Any], **ids: Any) -> Any:
            return await r.orch.classify(pid, await bus.publish(event_type, payload, project_id=pid, **ids))

        task = {"task_id": "t1", "title": "Menu"}
        assert await wake("task.created", {**task, "actor": "agent", "actor_id": f"orchestrator:{sid}"}) is None, "its own doing is not news"
        assert await wake("task.moved", {**task, "from": "todo", "to": "doing", "actor": "operator"}) is not None
        assert (await wake("task.merge_failed", {**task, "actor": "system"})).urgent
        assert await wake("staff.status", {"status": "idle", "previous": "working"}, staff_id=ada.id) is None
        assert not (await wake("staff.status", {"status": "turn_done_unseen", "previous": "working"}, staff_id=ada.id)).urgent
        assert (await wake("staff.status", {"status": "error", "previous": "working"}, staff_id=ada.id)).urgent
        assert (await wake("staff.report", {"kind": "stuck", "text": "no key"}, staff_id=ada.id)).urgent
        assert not (await wake("staff.report", {"kind": "checkpoint", "text": "half"}, staff_id=ada.id)).urgent
        common = {"request_id": "x", "request_ref": "staff:s:x", "title": "Ada", "telegram": False, "tool": "Exec", "text": "npm i", "risk": "routine", "quick": True, "kind": "staff"}
        assert (await wake("permission.pending", {**common, "routed_to": "orchestrator"}, staff_id=ada.id)).urgent
        assert not (await wake("permission.pending", {**common, "request_ref": "staff:s:y", "routed_to": "operator"}, staff_id=ada.id)).urgent
        assert await wake("run.started", {"run_id": "r", "origin": "operator", "title": "Ada"}, staff_id=ada.id) is not None
        assert await wake("run.started", {"run_id": "r", "origin": "operator", "title": "orch"}, session_id=sid) is None
        assert (await wake("schedule.fired", {"schedule_id": "w1", "name": "check", "kind": "wake"})).urgent
        assert await wake("schedule.fired", {"schedule_id": "w2", "name": "report", "kind": "agent"}) is None
        # A staff question the orchestrator escalated wakes it when the operator answers; one that
        # went straight to the operator does not.
        escalated = await r.manager.asks.open(pid, origin="staff", kind="question", text="Colour?", routed_to="orchestrator", staff_id=ada.id, detail={"event_ref": "ask:s:c1"})
        await r.team.escalate(escalated)
        await r.manager.asks.resolve(escalated.id, "operator", {"text": "blue"})
        assert (await wake("ask.answered", {"request_id": "c1", "request_ref": "ask:s:c1", "via": "app"}, staff_id=ada.id)).urgent
        direct = await r.manager.asks.open(pid, origin="staff", kind="question", text="Size?", routed_to="operator", staff_id=ada.id, detail={"event_ref": "ask:s:c2"})
        await r.manager.asks.resolve(direct.id, "operator", {"text": "big"})
        assert await wake("ask.answered", {"request_id": "c2", "request_ref": "ask:s:c2", "via": "app"}, staff_id=ada.id) is None
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_completed_acceptance_batch_does_not_start_an_empty_turn(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "I have new work."}])
    try:
        tick = [100.0]
        r.orch.clock = lambda: tick[0]
        r.manager.config.orchestrator.batch_seconds = 20
        task_id = await board_task(r.manager, r.project, "One answer")
        await r.orch.enable(r.project.id)
        queue = r.orch.queues[r.project.id]

        def event(kind: str, payload: dict[str, Any], *, staff_id: str | None = None) -> AppEvent:
            return AppEvent(0, datetime.now(UTC).isoformat(), kind, payload, project_id=r.project.id, staff_id=staff_id)

        # An earlier move may still be queued when acceptance finishes the only card.
        await queue.offer(event("task.moved", {"task_id": task_id, "from": "doing", "to": "review"}))
        await db.execute("UPDATE board_tasks SET status = 'done', acceptance_state = 'operator_approved' WHERE id = ?", (task_id,))
        await queue.offer(event("task.accepted", {"task_id": task_id, "actor": "operator"}))
        await queue.offer(event("staff.status", {"status": "exited", "detail": "its task was done"}, staff_id="one-off"))
        tick[0] += 21
        assert not await queue.pump()
        assert not queue.items and not r.provider.requests

        # A later task, answer, or error in the same window makes the whole batch actionable.
        for kind, payload in (
            ("task.created", {"task_id": "new", "title": "Next"}),
            ("ask.answered", {"request_id": "answer"}),
            ("staff.status", {"status": "error", "detail": "failed"}),
        ):
            batch = (event("task.accepted", {"task_id": task_id}), event(kind, payload, staff_id="worker"))
            from daedalus.host.wake_queue import Batch, Pending, Wake
            assert not await r.orch.completion_is_final(r.project.id, Batch(tuple(Pending(item, Wake(str(i)), tick[0]) for i, item in enumerate(batch)), urgent=False))

        await board_task(r.manager, r.project, "Next answer")
        await queue.offer(event("task.accepted", {"task_id": task_id}))
        tick[0] += 21
        assert await queue.pump(), "unfinished work keeps acceptance responsive"

        async def answered() -> bool:
            return bool(r.provider.requests) and await _idle(r.manager, (await r.refreshed()).settings.orchestrator.session_id)

        await until_await(answered, "the coordinator received the pending work")
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_a_finished_staff_turn_wakes_the_orchestrator_once_with_the_project_state(settings: Settings, db: Database, tmp_path: Path) -> None:
    """The acceptance: the operator enables an orchestrator, a staff member's turn finishes, and within the
    window the orchestrator receives one ``events`` message whose run's turn context holds the project state."""
    r = await rig(settings, db, tmp_path, [{"text": "Ada is done; I will look at her work."}])
    try:
        r.manager.config.orchestrator.batch_seconds = 1
        runtime = ObservedFakeStaffRuntime(kind="daedalus", page=ReadPage("The menu page is committed on the branch.", None, False))
        runtime.manager = r.manager
        r.team.runtimes["daedalus"] = runtime
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        await approve_coordinator(r, sid)
        ada = await r.manager.staff.hire(r.project.id, name="Ada", role="Menu", isolation="shared")
        # Its own board work is not news to it, so this does not become part of the batch.
        await r.call(sid, "tasks", op="create", title="Photos", objective="o" * 10, deliverable="d" * 10, boundaries="b" * 10, done_when="w" * 10)
        task_id = await board_task(r.manager, r.project, "Menu page")
        await operator_assignment(r.team, ada, task_id)
        live = await r.team.live_of(ada)
        assert live is not None
        await r.team.ingress.status(live, "no_signal")
        await r.team.ingress.status(live, "turn_done_unseen")

        async def woken() -> bool:
            return bool(await events_messages(r.manager, sid)) and await _idle(r.manager, sid)

        await until_await(woken, "the orchestrator was woken")
        [batch] = await events_messages(r.manager, sid)
        assert batch.startswith("[events · Bakery · ")
        assert 'Ada finished a turn on "Menu page"' in batch and "The menu page is committed" in batch and 'ReadStaff("Ada")' in batch
        assert "has gone silent" not in batch, "the member's latest status replaced the earlier one"
        assert "Photos" not in batch
        sent = last_user_text(r.provider, 0)
        assert "Project: Bakery" in sent and "Ada — Menu" in sent and "Board:" in sent
        cursor = await db.kv_get(f"orchestrator_cursor:{r.project.id}")
        assert isinstance(cursor, int) and cursor > 0
    finally:
        await close_team(r.manager)
        await r.manager.close()


async def test_ask_operator_returns_at_once_and_its_answer_arrives_as_an_event(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path, [{"text": "Postgres it is."}])
    try:
        sid = (await r.orch.enable(r.project.id)).settings.orchestrator.session_id
        said = await r.call(sid, "ask_operator", title="Postgres or SQLite?", text="Postgres or SQLite?", options=["Postgres", "SQLite"], context="orders need concurrency")
        assert said.startswith("asked the operator as [q") and "do not wait" in said
        [ask] = await r.manager.asks.open_for(r.project.id, routed_to="operator")
        assert ask.origin == "orchestrator" and ask.detail["options"] == ["Postgres", "SQLite"] and "Context: orders need concurrency" in ask.text
        assert (await r.manager.get_state(sid)).pending is None, "the orchestrator is not paused"  # type: ignore[union-attr]
        await r.team.answer(ask.id, selected=["Postgres"], by="operator", via="app")

        async def woken() -> bool:
            return bool(await events_messages(r.manager, sid))

        await until_await(woken, "the answer woke the orchestrator")
        [batch] = await events_messages(r.manager, sid)
        assert f"the operator answered your request [{ask.short_id}]" in batch and "Postgres" in batch
    finally:
        await close_team(r.manager)
        await r.manager.close()


# -- the routes ---------------------------------------------------------------------------------------------


async def test_the_orchestrator_routes_and_the_model_chip(settings: Settings, db: Database, tmp_path: Path) -> None:
    r = await rig(settings, db, tmp_path)
    try:
        app = SimpleNamespace(settings=settings, config=r.manager.config, db=db, manager=r.manager, front=None, extensions=r.team.app.extensions, guard=None, notifications=None)

        async def save_config(config: Any, expected_revision: str | None = None) -> None:
            app.config = r.manager.config = config

        app.save_config = save_config
        api = build_app(app, "tok")  # type: ignore[arg-type]
        headers = {"X-Daedalus-Token": "tok"}
        base = f"/api/projects/{r.project.id}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:  # type: ignore[arg-type]
            preview = await client.post(f"{base}/orchestrator/preflight", json={"model": "", "concurrency_cap": 12}, headers=headers)
            assert preview.status_code == 200 and preview.json() == {"effective_model": DEFAULT_PRESET}
            assert not (await r.refreshed()).settings.orchestrator.enabled
            invalid = await client.post(f"{base}/orchestrator/preflight", json={"model": "nope"}, headers=headers)
            assert invalid.status_code == 400 and not (await r.refreshed()).settings.orchestrator.enabled
            on = await client.post(f"{base}/orchestrator", json={"autonomy": "full", "concurrency_cap": 12}, headers=headers)
            assert on.status_code == 200, on.text
            body = on.json()
            assert (body["enabled"], body["autonomy"], body["concurrency_cap"], body["effective_model"]) == (True, "full", 12, DEFAULT_PRESET)
            sid = body["session_id"]
            assert (await client.post(f"{base}/orchestrator", json={"model": "nope"}, headers=headers)).status_code == 400
            patched = await client.patch(f"{base}/orchestrator", json={"concurrency": 5}, headers=headers)
            assert patched.status_code == 200 and patched.json()["concurrency"] == 5
            state = await client.get(f"{base}/state", headers=headers)
            assert state.status_code == 200 and state.json()["text"].startswith("Project: Bakery") and state.json()["session_id"] == sid
            chip = await client.post(f"/api/sessions/{sid}/model", json={"preset": FALLBACK_PRESET}, headers=headers)
            assert chip.status_code == 200
            assert (await r.refreshed()).settings.orchestrator.model == FALLBACK_PRESET
            before_replace = await r.refreshed()
            replaced = await client.post(f"{base}/orchestrator/replace", json={
                "reason": "fresh start", "client_operation_id": "route-replacement",
                "expected_entity_revision": before_replace.entity_revision,
                "expected_coordinator_session_id": sid,
            }, headers=headers)
            assert replaced.status_code == 200 and replaced.json()["session_id"] not in ("", sid)
            off = await client.delete(f"{base}/orchestrator", headers=headers)
            assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["session_id"] == ""
            disabled = await r.refreshed()
            assert (await client.post(f"{base}/orchestrator/replace", json={
                "reason": "after disable", "client_operation_id": "route-disabled",
                "expected_entity_revision": disabled.entity_revision,
                "expected_coordinator_session_id": replaced.json()["session_id"],
            }, headers=headers)).status_code == 409
            settings_view = await client.get("/api/settings", headers=headers)
            assert settings_view.status_code == 200
            assert settings_view.json()["orchestrator"]["strongest"] == DEFAULT_PRESET
            saved = await client.put("/api/settings", json={"orchestrator": {"preset": VISION_PRESET}, "base_revision": settings_view.json()["revision"]}, headers=headers)
            assert saved.status_code == 200, saved.text
            assert saved.json()["orchestrator"]["preset"] == VISION_PRESET
    finally:
        await close_team(r.manager)
        await r.manager.close()
