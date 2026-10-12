"""Edit's loose matching, MultiEdit, background jobs, plan mode, run budgets, workspace notes and memory ranking."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from protocore.contracts.memory import MemoryScope
from protocore.contracts.tool_registry import policy_admits
from protocore.contracts.tools import ToolContext

from daedalus.config import (
    DEFAULT_MODES,
    ORCHESTRATOR_ONLY_TOOLS,
    STAFF_ONLY_TOOLS,
    SUBAGENT_ONLY_TOOLS,
    VOICE_ONLY_TOOLS,
    LimitsConfig,
    RuntimeConfig,
)
from daedalus.host.services import SessionServices, locator
from daedalus.providers.openai_compat import apply_cache_control
from daedalus.tools.files import EditMiss, apply_edit, nearest_window
from tests.support.waiting import until_await


def test_apply_edit_matches_exactly_then_loosely_and_keeps_the_file_indentation() -> None:
    text = "def f():\n    x = 1  \n    return x\n"
    updated, count, how = apply_edit(text, "    x = 1\n    return x", "    x = 2\n    return x")
    assert (updated, count) == ("def f():\n    x = 2\n    return x\n", 1) and how.startswith("ignoring trailing whitespace at line 2")
    updated, count, how = apply_edit(text, "x = 1\nreturn x", "x = 3\nreturn x + 1")
    assert how.startswith("ignoring indentation at line 2") and updated == "def f():\n    x = 3\n    return x + 1\n"
    with pytest.raises(EditMiss, match="closest lines"):
        apply_edit(text, "    y = 1\n    return y", "z")
    with pytest.raises(EditMiss, match="matches 2 times"):
        apply_edit("a\na\n", "a", "b")
    assert apply_edit("a\na\n", "a", "b", replace_all=True)[0] == "b\nb\n"
    # a loose replace_all edits every hit, each at its own indentation, and reports the true count
    loose = "if a:\n  pass \nelse:\n      pass \n"
    updated, count, how = apply_edit(loose, "        pass", "        continue", replace_all=True)
    assert updated == "if a:\n  continue\nelse:\n      continue\n" and count == 2 and "and 1 more" in how
    assert "return x" in nearest_window(text, "    return y")


async def test_multi_edit_is_atomic_and_write_reports_a_broken_python_file(tmp_path: Path) -> None:
    from daedalus.tools.files import multi_edit, write_file

    locator.register(SessionServices(session_id="w2-files", workspace_dir=tmp_path))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="w2-files", metadata={"tool_call_id": "c"})
    try:
        (tmp_path / "m.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        result = await multi_edit().invoke(ctx, {"path": "m.py", "edits": [{"old_string": "a = 1", "new_string": "a = 10"}, {"old_string": "zzz", "new_string": "y"}]})
        assert result.is_error and "edit 2 of 2 failed" in result.content and (tmp_path / "m.py").read_text() == "a = 1\nb = 2\nc = 3\n"
        result = await multi_edit().invoke(ctx, {"path": "m.py", "edits": [{"old_string": "a = 1", "new_string": "a = 10"}, {"old_string": "c = 3", "new_string": "c = 30"}]})
        assert not result.is_error and (tmp_path / "m.py").read_text() == "a = 10\nb = 2\nc = 30\n"
        result = await write_file().invoke(ctx, {"path": "bad.py", "content": "def broken(:\n    pass\n"})
        assert not result.is_error and "does not compile" in result.content
    finally:
        locator.unregister("w2-files")


async def test_background_jobs_start_report_and_die(tmp_path: Path) -> None:
    from daedalus.tools.shell import exec_command, job_kill, job_list, job_output

    locator.register(SessionServices(session_id="w2-jobs", workspace_dir=tmp_path))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="w2-jobs", metadata={"tool_call_id": "c"})
    try:
        started = await exec_command().invoke(ctx, {"command": "for i in 1 2 3; do echo tick $i; sleep 0.2; done; sleep 30", "background": True})
        assert not started.is_error and "running" in started.content
        job_id = str(started.metadata["job_id"])

        async def ticked() -> bool:
            return "tick 3" in (await job_output().invoke(ctx, {"job_id": job_id, "tail_lines": 2})).content

        # The job's own output is what says it got there, rather than a length of time that is only
        # long enough on an idle host: three ticks two tenths apart is a second and a half of sleep
        # the scheduler does not owe anybody.
        await until_await(ticked, "the background job wrote its third tick")
        out = await job_output().invoke(ctx, {"job_id": job_id, "tail_lines": 2})
        assert "tick 3" in out.content and out.metadata["running"] is True
        listing = await job_list().invoke(ctx, {})
        assert job_id in listing.content
        killed = await job_kill().invoke(ctx, {"job_id": job_id})
        assert not killed.is_error and (await job_output().invoke(ctx, {"job_id": job_id})).metadata["running"] is False
        assert (tmp_path / ".jobs" / f"{job_id}.log").read_text().count("tick") == 3
    finally:
        locator.unregister("w2-jobs")


async def test_exec_keeps_the_tail_and_spills_the_whole_output(tmp_path: Path) -> None:
    from daedalus.tools.shell import exec_command

    locator.register(SessionServices(session_id="w2-spill", workspace_dir=tmp_path, max_tool_output_chars=2000))
    ctx = ToolContext(tenant_id="t", run_id="r", session_id="w2-spill", metadata={"tool_call_id": "call-spill"})
    try:
        result = await exec_command().invoke(ctx, {"command": "for i in $(seq 1 3000); do echo line-$i; done; echo THE-END"})
        assert "THE-END" in result.content and "line-1\n" in result.content and "omitted" in result.content
        spill = tmp_path / ".exec" / "call-spill.log"
        assert spill.is_file() and spill.read_text().count("line-") == 3000 and str(spill) in result.content
    finally:
        locator.unregister("w2-spill")


async def test_plan_mode_is_an_allow_list_that_a_settings_toggle_cannot_disarm(settings, db) -> None:  # type: ignore[no-untyped-def]
    from daedalus.host.session_runner import SessionManager

    plan = DEFAULT_MODES["plan"]
    assert {"Read", "Search", "WebFetch", "AskUser"} <= set(plan.tools_only) and "plan" in RuntimeConfig().modes
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("plan")
        known = {t.name for t in manager.tools.list_all()}
        # A working session is blocked from the concierge's, staff's, orchestrator's and subagents' own tools and from nothing else.
        others = set(VOICE_ONLY_TOOLS) | set(STAFF_ONLY_TOOLS) | (known & set(ORCHESTRATOR_ONLY_TOOLS)) | set(SUBAGENT_ONLY_TOOLS)
        assert manager.blocked_tools_for(state) == others
        await manager.set_mode(state.session.id, "plan")
        blocked = manager.blocked_tools_for(state)
        assert {"Exec", "Verify", "Write", "Edit", "MultiEdit", "SendFile", "SelfPropose", "SubAgent", "AskPeer", "McpEnable", "ServiceStart"} <= blocked
        assert blocked == (known - set(plan.tools_only)) | others
        await manager.set_tools_off(state.session.id, ["WebSearch"])
        assert "Exec" in manager.blocked_tools_for(state) and "WebSearch" in manager.blocked_tools_for(state)
        await manager.set_mode(state.session.id, "")
        assert manager.blocked_tools_for(state) == {"WebSearch"} | others
    finally:
        await manager.close()


async def test_child_effective_policy_tracks_parent_revocation_for_advertisement_and_dispatch(settings, db) -> None:  # type: ignore[no-untyped-def]
    from daedalus.host.session_runner import SessionManager

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        leader = await manager.create_session("leader")
        child = await manager.create_session("child", metadata={"subagent_of": leader.session.id})
        grandchild = await manager.create_session("grandchild", metadata={"subagent_of": child.session.id})
        advertised_before_revoke = manager.tool_policy_for(child)
        assert policy_admits(advertised_before_revoke, "Exec")

        await manager.set_mode(leader.session.id, "plan")
        advertised = manager.tool_policy_for(child)
        assert {"Exec", "Verify", "ServiceStart"} <= advertised.blocked
        assert not any(policy_admits(advertised, name) for name in ("Exec", "Verify", "ServiceStart"))
        assert "Exec" in manager.tool_policy_for(grandchild).blocked
        # The call was valid in the earlier advertisement, but dispatch reads the current policy.
        denied = manager.policy_gate(child.session.id, "run-child").decide("Exec", {"command": "true"})
        assert denied.action == "deny" and denied.rule == "session.capabilities" and denied.key == ""

        # A task-local update may narrow the child, but cannot restore a capability its parent removed.
        await manager.set_tools_off(child.session.id, [])
        assert "Exec" in manager.tool_policy_for(child).blocked
        await manager.set_mode(leader.session.id, "")
        assert "Exec" not in manager.tool_policy_for(child).blocked
        await manager.set_tools_off(child.session.id, ["Exec"])
        assert manager.policy_gate(child.session.id, "run-child").decide("Exec", {"command": "true"}).action == "deny"
    finally:
        await manager.close()


def test_run_budgets_are_configurable_and_off_by_default() -> None:
    limits = LimitsConfig()
    assert limits.max_run_minutes == 0 and limits.max_run_tokens == 0
    assert LimitsConfig(max_run_minutes=30, max_run_tokens=2_000_000).max_run_tokens == 2_000_000


def test_cache_control_lands_on_the_named_messages_only() -> None:
    class BP:
        def __init__(self, i: int) -> None:
            self.message_index = i

    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "hi"}, {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}]
    apply_cache_control(messages, [BP(0), BP(2), BP(9)])
    assert messages[0]["content"] == [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]
    assert messages[1]["content"] == "hi"
    assert messages[2]["content"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_memory_recall_ranks_the_record_that_names_the_thing(db) -> None:  # type: ignore[no-untyped-def]
    from daedalus.stores.persistent import PersistentMemory

    memory = PersistentMemory(db)
    await memory.load()
    await memory.write("t", MemoryScope.global_, "", "The operator prefers replies in Russian and hates narrow layouts; many words about many things and the word deploy appears once.")
    await memory.write("t", MemoryScope.global_, "", "Deploy the bot with docker compose restart daedalus.")
    hits = await memory.recall("t", "how to deploy the bot")
    assert hits and "docker compose" in hits[0].record.text
    assert hits[0].record.access_count == 1
    assert await memory.recall("t", "") and datetime.now(UTC).year >= 2026


async def test_workspace_notes_carry_agents_md_and_open_board_items(settings, db) -> None:  # type: ignore[no-untyped-def]
    from daedalus.host.session_runner import SessionManager

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("notes")
        assert await manager.workspace_notes(state) == ""
        (state.workspace / "AGENTS.md").write_text("# Goal\nShip it.\n", encoding="utf-8")
        await db.execute("INSERT INTO board_tasks(id, title, status, priority, session_id, created_at, updated_at) VALUES ('t1', 'Write the adapter', 'doing', 1, ?, '2026-09-10', '2026-09-10')", (state.session.id,))
        notes = await manager.workspace_notes(state)
        assert "AGENTS.md" in notes and "Ship it." in notes and "[doing] t1: Write the adapter" in notes
        # A CLAUDE.md that differs is read beside it, the memory index too, and on a large window a long
        # file is not cut at 6,000 characters.
        state.context_window = 1_000_000
        (state.workspace / "CLAUDE.md").write_text("# Rules\n" + "Deliver by format-patch. " * 600, encoding="utf-8")
        (state.workspace / ".agents" / "memory").mkdir(parents=True)
        (state.workspace / ".agents" / "memory" / "MEMORY.md").write_text("- [Stands](stands.md): the client stand is work-sandbox\n", encoding="utf-8")
        notes = await manager.workspace_notes(state)
        assert notes.count("Deliver by format-patch.") == 600 and "the client stand is work-sandbox" in notes and ".agents/memory/MEMORY.md" in notes
        (state.workspace / "CLAUDE.md").write_text("# Goal\nShip it.\n", encoding="utf-8")
        assert (await manager.workspace_notes(state)).count("Ship it.") == 1, "a CLAUDE.md that repeats AGENTS.md is read once"
    finally:
        await manager.close()
