"""Personas for subagents, skills as recipes, skill drafts, the board's PLAN.md and the session export."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig
from daedalus.host.services import SessionServices, locator
from daedalus.host.skills import DirectorySkillStore
from tests.support.authorized_results import accept_branchless_result, reopen_accepted_result, submit_manual_result

REPO = Path(__file__).resolve().parents[2]


def test_every_persona_reads_as_a_stance_and_is_public_clean() -> None:
    files = sorted((REPO / "personas").glob("*.md"))
    assert {f.stem for f in files} >= {"critic", "skeptic", "simplifier", "security", "researcher"}
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert text.startswith("# ") and len(text) > 200 and "/home/" not in text and "192.168" not in text


async def test_subagent_persona_is_prepended_and_unknown_is_refused(settings, db) -> None:  # type: ignore[no-untyped-def]
    from daedalus.app import Application
    from daedalus.extensions.subagents import Subagents
    from daedalus.host.session_runner import SessionManager

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        app = Application.__new__(Application)
        app.settings, app.config, app.manager, app.db = settings, RuntimeConfig(), manager, db
        subagents = Subagents(app)
        assert "critic" in subagents.personas() and subagents.persona("../etc/passwd") is None and subagents.persona("nope") is None
        assert subagents.persona("critic").startswith("# Critic")
        leader = await manager.create_session("lead")
        submitted: list[str] = []

        async def fake_submit(session_id: str, text: str, **kwargs):  # type: ignore[no-untyped-def]
            submitted.append(text)
            return "run-x"

        manager.submit = fake_submit  # type: ignore[method-assign]
        result = await subagents.spawn(leader_id=leader.session.id, task="Review the plan", persona="critic")
        assert submitted and "[persona: critic]" in submitted[0] and "# Critic" in submitted[0] and "Review the plan" in submitted[0]
        try:
            await subagents.spawn(leader_id=leader.session.id, task="x", persona="wizard")
            raise AssertionError("an unknown persona must be refused")
        except ValueError as exc:
            assert "unknown persona" in str(exc)
        assert result["name"]
    finally:
        await manager.close()


async def test_skill_parameters_are_filled_from_args_and_missing_ones_are_named(tmp_path: Path) -> None:
    from daedalus.tools.skill import load_skill

    (tmp_path / "deploy-svc").mkdir()
    (tmp_path / "deploy-svc" / "SKILL.md").write_text("---\nname: deploy-svc\ndescription: Deploy a service by name to a host.\nparams: service, host\n---\nRun `deploy {{service}} to {{host}}` and check {{host}}/health.\n", encoding="utf-8")
    store = DirectorySkillStore(tmp_path)
    assert store.params_of("deploy-svc") == ["service", "host"]
    locator.register(SessionServices(session_id="w4-skill", workspace_dir=tmp_path, extra={"skill_store": store}))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="w4-skill", metadata={"tool_call_id": "c"})
    try:
        result = await load_skill().invoke(ctx, {"skill": "deploy-svc", "args": {"service": "api", "host": "h1.example"}})
        assert "deploy api to h1.example" in result.content and "{{" not in result.content
        result = await load_skill().invoke(ctx, {"skill": "deploy-svc", "args": {"service": "api"}})
        assert "Not given: host" in result.content and "{{host}}" in result.content
    finally:
        locator.unregister("w4-skill")


async def test_skill_draft_is_saved_under_the_state_directory(settings, db) -> None:  # type: ignore[no-untyped-def]
    from daedalus.host.session_runner import SessionManager
    from daedalus.tools.skill import skill_draft

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("draft")
        ctx = ToolContext(tenant_id="t", run_id="r", session_id=state.session.id, metadata={"tool_call_id": "c"})
        result = await skill_draft().invoke(ctx, {"name": "Release Notes!", "description": "Write release notes from a git range in the house style.", "body": "1. git log --oneline A..B\n2. group by area\n3. one line per change, verb first\n4. mention breaking changes first\n5. run the checker before publishing", "params": "range"})
        assert not result.is_error
        draft = settings.state_dir / "skill-drafts" / "release-notes" / "SKILL.md"
        assert draft.is_file() and draft.read_text().startswith("---\nname: release-notes\n") and "params: range" in draft.read_text()
        # A numbered list without the sections a skill needs is saved, and the gap is said at once.
        assert "Before it can become a skill" in result.content and "usage, procedure and checks" in result.content
        assert (await skill_draft().invoke(ctx, {"name": "x", "description": "short", "body": "tiny"})).is_error
    finally:
        await manager.close()


async def test_the_board_writes_plan_md_into_the_sessions_workspace(settings, db) -> None:  # type: ignore[no-untyped-def]
    from daedalus.app import Application
    from daedalus.extensions.board import Board
    from daedalus.host.session_runner import SessionManager

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        app = Application.__new__(Application)
        app.settings, app.config, app.manager, app.db, app.extensions = settings, RuntimeConfig(), manager, db, {}
        board = Board(app)
        state = await manager.create_session("plan")
        task = await board.add(title="Write the adapter", acceptance="harbor run passes hello-world", checklist=["scaffold", "smoke"], session_id=state.session.id)
        plan = (state.workspace / "PLAN.md").read_text()
        assert "Write the adapter" in plan and "- [ ] scaffold" in plan and "done when: harbor run passes hello-world" in plan
        await board.update(task["id"], check=[0], session_id=state.session.id)
        assert "- [x] scaffold" in (state.workspace / "PLAN.md").read_text()
        await board.update(task["id"], check=[1], session_id=state.session.id)
        await board.update(task["id"], status="review", session_id=state.session.id)
        await submit_manual_result(app, task["id"], note="The adapter scaffold and smoke check are complete")
        await accept_branchless_result(SimpleNamespace(app=app), task["id"])
        plan = (state.workspace / "PLAN.md").read_text()
        assert f"- [x] **{task['id']}** Write the adapter — done" in plan  # finishing a task releases it but keeps it on the plan
        await reopen_accepted_result(app, task["id"], reason="The smoke check needs another run")
        assert f"- [ ] **{task['id']}** Write the adapter — todo" in (state.workspace / "PLAN.md").read_text()
        with pytest.raises(ValueError, match="audited results"):
            await board.delete(task["id"])
        spare = await board.add(title="Unused note", session_id=state.session.id)
        assert await board.delete(spare["id"])
        assert spare["id"] not in (state.workspace / "PLAN.md").read_text()
    finally:
        await manager.close()
