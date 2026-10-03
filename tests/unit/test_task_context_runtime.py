"""A launched worker receives pinned obligations through both context channels."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from protocore.contracts.llm import LLMResponse
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

from daedalus.config import Settings
from daedalus.extensions.staff import Team
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.stores.runtime_release import no_entry_in
from daedalus.stores.staff_context import staff_context_packet
from tests.support.authorized_launch import operator_assignment
from tests.support.waiting import until_await
from tests.unit.test_session_runner import ScriptedProvider, _manager
from tests.unit.test_staff_runtime import BRIEF, board_task, close_team, fake_team, project_with, repository, team_for


async def test_changed_source_after_pin_refuses_runtime_entry(settings: Settings, db: Database,
                                                              tmp_path: Path, monkeypatch: Any) -> None:
    manager, team, runtime, project = await fake_team(settings, db, tmp_path)
    try:
        member = await manager.staff.hire(project.id, name="Worker", role="Menu reviewer", isolation="readonly")
        task_id = await board_task(manager, project, "Menu")
        import daedalus.extensions.staff as staff_module

        original_pin = staff_module.pin_staff_context

        async def change_after_pin(database: Database, staff_session_id: str, packet: dict[str, Any], *,
                                   role_hint: str) -> None:
            await original_pin(database, staff_session_id, packet, role_hint=role_hint)
            await database.execute("UPDATE board_tasks SET title = 'Changed task' WHERE id = ?", (task_id,))

        monkeypatch.setattr(staff_module, "pin_staff_context", change_after_pin)
        queued = await operator_assignment(team, member, task_id, wait_for_admission=False)

        async def refused() -> bool:
            row = await db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (queued["effect_id"],))
            return row is not None and row["state"] in ("failed", "unknown")

        await until_await(refused, "the stale context prevented physical launch", timeout=10)
        assert runtime.started == []
        row = await db.fetchone("SELECT status,current_attempt_id FROM board_tasks WHERE id = ?", (task_id,))
        assert row["status"] == "todo"
        assert (await db.fetchone("SELECT COUNT(*) AS n FROM staff_context_packets WHERE task_id = ?",
                                 (task_id,)))["n"] == 1
        async with db.transaction() as conn:
            assert await no_entry_in(conn, row["current_attempt_id"])
        assert not await db.fetchall("SELECT * FROM runtime_exit_observations")
        monkeypatch.setattr(staff_module, "pin_staff_context", original_pin)
        dispatcher = team.app.extensions["effects"]
        await dispatcher.reconcile()
        assert (await db.fetchone("SELECT state FROM effect_outbox WHERE id = ?", (queued["effect_id"],)))[0] == "failed"
        await operator_assignment(team, member, task_id)
        assert len(runtime.started) == 1
        attempts = await db.fetchall("SELECT id,state,runtime_entered_at FROM execution_attempts WHERE task_id = ?", (task_id,))
        assert len(attempts) == 2
        assert sum(attempt["runtime_entered_at"] is not None for attempt in attempts) == 1
    finally:
        await close_team(manager)
        await manager.close()


async def test_native_packet_stays_in_system_brief_after_real_history_compaction(
        settings: Settings, db: Database, tmp_path: Path) -> None:
    provider = ScriptedProvider([{"text": "Task understood"}])
    manager: SessionManager = await _manager(settings, db, provider)
    team: Team | None = None
    try:
        team = await team_for(settings, manager)
        project = await project_with(manager, repository(tmp_path))
        member = await manager.staff.hire(project.id, name="Worker", role="Menu reviewer", isolation="readonly")
        task_id = await board_task(manager, project, "Menu", brief=BRIEF)
        await operator_assignment(team, member, task_id)

        async def finished() -> bool:
            live = await manager.staff.live(member.id)
            return live is not None and live.status == "turn_done_unseen"

        await until_await(finished, "the native member finished its first model turn")
        staff_session = await manager.staff.live(member.id)
        assert staff_session is not None and staff_session.session_id is not None
        packet = await staff_context_packet(db, staff_session.id)
        assert packet is not None
        state = await manager.get_state(staff_session.session_id)
        assert state is not None
        assert packet["packet_hash"] in state.metadata["brief"]
        assert "Touch nothing else" in manager.notes_for(state)

        original = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
        _, preset = manager.config.preset()
        summariser = manager.providers.get(preset.provider)

        async def summary(_request: Any) -> LLMResponse:
            return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(
                text="## Goal\nMenu\n## Constraints\nSee pinned session brief\n## State\nWorking\n"
                     "## Discoveries\nNone\n## Open\nNone\n## Next steps\nReview\n## Unknowns\nNone\n"
                     "## Identifiers\nNone")]), stop_reason=StopReason.end_turn)

        summariser.complete_text = summary  # type: ignore[method-assign]
        await manager.compact(state.session.id)
        compacted = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
        assert len(compacted) < len(original)
        assert packet["packet_hash"] in manager.notes_for(state)
        assert "Never change" in manager.notes_for(state) or "Touch nothing else" in manager.notes_for(state)
    finally:
        if team is not None:
            await close_team(manager)
        await manager.close()
