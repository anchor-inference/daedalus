"""SessionManager against a scripted provider: runs, follow-ups, shutdown and resume."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from protocore.contracts.llm import ILLMProvider, LLMRequest, LLMResponse, ProviderDelta, ProviderDeltaKind
from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock, ToolResultBlock, ToolUseBlock
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.config import Settings
from daedalus.extensions.api import build_app, message_view
from daedalus.host.session_runner import SessionManager, clip_title
from daedalus.providers.pricing import ModelPricing
from daedalus.stores.database import Database
from tests.support.models import model_config
from tests.support.waiting import SETTLE, until, until_await


class ScriptedProvider(ILLMProvider):
    """Answers each request with the next script entry: a tool call or a final text."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = list(script)
        self.requests: list[LLMRequest] = []

    class _Endpoint:
        id = "scripted"
        kind = "scripted"
        pricing = {"scripted-model": ModelPricing(input_limit=128_000, limit_source="scripted test provider")}

    endpoint = _Endpoint()

    async def stream_with_tools(self, request: LLMRequest) -> AsyncIterator[ProviderDelta]:
        self.requests.append(request)
        step = self.script.pop(0) if self.script else {"text": "nothing left"}
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0}
        if "tool" in step:
            call_id = f"call_{len(self.requests)}"
            yield ProviderDelta(kind=ProviderDeltaKind.tool_use_start, tool_call_id=call_id, tool_name=step["tool"])
            yield ProviderDelta(
                kind=ProviderDeltaKind.tool_use_input, tool_call_id=call_id, tool_input_delta=json.dumps(step["args"])
            )
            yield ProviderDelta(
                kind=ProviderDeltaKind.tool_use_stop,
                tool_call_id=call_id,
                tool_name=step["tool"],
                tool_input_final=step["args"],
                is_block_end=True,
            )
            yield ProviderDelta(kind=ProviderDeltaKind.usage, usage=usage)
            yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="tool_use")
            return
        yield ProviderDelta(kind=ProviderDeltaKind.text, content=step["text"])
        yield ProviderDelta(kind=ProviderDeltaKind.usage, usage=usage)
        yield ProviderDelta(kind=ProviderDeltaKind.finish, finish_reason="stop")

    async def complete_structured(self, request: LLMRequest, response_schema: dict[str, Any]) -> LLMResponse:
        return LLMResponse(
            message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text='{"summary": "summary"}')]),
            stop_reason=StopReason.end_turn,
        )

    async def complete_text(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="summary")]),
            stop_reason=StopReason.end_turn,
        )

    def count_tokens(self, text: str, model: str | None = None) -> int:
        return len(text) // 4

    async def aclose(self) -> None:
        return None


async def _manager(settings: Settings, db: Database, provider: ScriptedProvider) -> SessionManager:
    manager = SessionManager(settings, model_config(), db=db)
    await manager.start()
    manager.providers.rungs_for = lambda config: [(provider, "scripted-model")]  # type: ignore[method-assign]
    return manager


async def _wait_finished(manager: SessionManager) -> list[tuple[str, str, str]]:
    finished: list[tuple[str, str, str]] = []
    done = asyncio.Event()

    async def on_finished(session_id: str, run_id: str, status: str) -> None:
        finished.append((session_id, run_id, status))
        done.set()

    manager.on_finished(on_finished)
    await asyncio.wait_for(done.wait(), timeout=30)
    return finished


async def test_run_executes_tool_and_persists_history(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo hello-from-tool"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    events: list[TurnEvent] = []

    async def sink(session_id: str, event: TurnEvent) -> None:
        events.append(event)

    manager.add_sink(sink)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "run it")
    finished = await waiter
    assert finished[0][2] == "completed"
    results = [e for e in events if e.type is EventType.TOOL_RESULT]
    assert results and "hello-from-tool" in str(results[0].payload)
    history = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert [m.role.value for m in history] == ["user", "assistant", "tool", "assistant"]
    assert provider.requests[-1].messages[-1].role is MessageRole.tool
    usage = await db.fetchone("SELECT count(*) c FROM usage_events")
    assert usage["c"] == 0  # the scripted provider bypasses the usage sink; the loop still saw usage
    await manager.close()


async def test_tool_event_is_redacted_before_durable_storage_and_replay(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([]))
    state = await manager.create_session("t")
    token = "sk-test-abcdefghijklmnopqrstuvwxyz012345"
    private_path = str(Path.home() / "private" / "report.txt")
    state.engine = SimpleNamespace(history=[Message(
        role=MessageRole.tool,
        content_blocks=[ToolResultBlock(tool_call_id="redaction-call", content=f"Report saved at {private_path}; 3 checks passed. Key: {token}")],
    )])  # type: ignore[assignment]
    event = TurnEvent(
        type=EventType.TOOL_RESULT,
        run_id="redaction-run",
        payload={
            "tool_call_id": "redaction-call",
            "success": True,
            "is_error": False,
            "content_blocks": [{"type": "text", "text": f"Report saved at {private_path}; 3 checks passed. Key: {token}"}],
            "ui_payload": {"note": f"Review {private_path}"},
            "path": private_path,
        },
    )
    await manager._dispatch_event(state, event)
    assert event.payload["path"] == "~/private/report.txt"
    assert event.payload["success"] is True and event.payload["is_error"] is False

    rows = await db.fetchall("SELECT payload FROM events WHERE run_id = ?", ("redaction-run",))
    stream_rows = await db.fetchall("SELECT payload FROM session_events WHERE session_id = ?", (state.session.id,))
    assert len(rows) == len(stream_rows) == 1
    for row in [*rows, *stream_rows]:
        assert token not in row["payload"]
        assert private_path not in row["payload"]
        assert "~/private/report.txt" in row["payload"]
        assert "3 checks passed" in row["payload"]
    replay = await manager.events.session_replay(state.session.id, after=0)
    exported = json.dumps(replay, ensure_ascii=False)
    assert token not in exported and private_path not in exported
    assert "3 checks passed" in exported
    result = await manager.events.session_tool_result(state.session.id, "redaction-call")
    assert result is not None and "3 checks passed" in result[0] and token not in result[0]
    arguments = {"command": f"cat {private_path}", "credential": token}
    live_fragments = []
    for fragment in (private_path[:len(private_path) // 2], private_path[len(private_path) // 2:], token):
        delta = TurnEvent(
            type=EventType.TOOL_USE_INPUT_DELTA, run_id="redaction-run",
            payload={"tool_call_id": "another-call", "partial_input_json": fragment},
        )
        await manager._dispatch_event(state, delta)
        live_fragments.append(delta.payload)
    assert all(payload["partial_input_json"] == "" for payload in live_fragments)
    await manager._dispatch_event(state, TurnEvent(
        type=EventType.TOOL_USE_STOP, run_id="redaction-run",
        payload={"tool_call_id": "another-call", "tool_name": "Exec", "final_input": arguments},
    ))
    stored_arguments = await db.fetchall("SELECT payload FROM events WHERE run_id = ?", ("redaction-run",))
    assert len(stored_arguments) == 2
    assert private_path not in stored_arguments[-1]["payload"] and token not in stored_arguments[-1]["payload"]
    assert "~/private/report.txt" in stored_arguments[-1]["payload"]
    session_rows = await db.fetchall("SELECT payload FROM session_events WHERE session_id = ?", (state.session.id,))
    assert len(session_rows) == 2
    assert all(private_path not in row["payload"] and token not in row["payload"] for row in session_rows)
    replay = await manager.events.session_replay(state.session.id, after=0)
    assert private_path not in json.dumps(replay) and token not in json.dumps(replay)
    state.engine.history.append(Message(
        role=MessageRole.assistant,
        content_blocks=[ToolUseBlock(tool_call_id="another-call", name="Exec", arguments_json=json.dumps(arguments))],
    ))
    await manager.sessions.append_transcript(state.session.id, state.engine.history)
    state.engine = None
    app = SimpleNamespace(settings=settings, config=manager.config, db=db, manager=manager, front=None,
                          extensions={}, guard=None, create_session=manager.create_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(app, "tok")), base_url="http://test") as client:  # type: ignore[arg-type]
        response = await client.get(
            f"/api/sessions/{state.session.id}/tool-results/redaction-call",
            headers={"X-Daedalus-Token": "tok"},
        )
        export = await client.get(
            f"/api/sessions/{state.session.id}/export/download",
            headers={"X-Daedalus-Token": "tok"},
        )
    assert response.status_code == 200
    assert "3 checks passed" in response.text and token not in response.text and private_path not in response.text
    assert export.status_code == 200
    assert "3 checks passed" in export.text and token not in export.text and private_path not in export.text
    assert "~/private/report.txt" in export.text
    await manager.close()


async def test_follow_up_is_queued_while_running_and_placed_next_step(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 1"}}, {"text": "first done"}, {"text": "second"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "start")
    await until(lambda: state.running, "the first run started")
    await manager.submit(state.session.id, "and also this")
    queued = await manager.live.load(state.session.id)
    assert queued["steer"] and queued["steer"][0]["text"] == "and also this"
    await waiter
    texts = [b.text for m in provider.requests[-1].messages for b in m.content_blocks if isinstance(b, TextBlock)]
    assert any("and also this" in t for t in texts)
    await manager.close()


async def test_shutdown_keeps_snapshot_and_resume_continues(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 5"}}, {"text": "after restart"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    await manager.submit(state.session.id, "long job")
    # Far enough in that the turn's snapshot is on disk: that is what the shutdown has to preserve,
    # and "running" is true before it, so waiting on that would race the write rather than order it.
    await until_await(manager.events.unfinished_snapshots, "the turn's snapshot was written")
    await manager.close()  # simulates the process going down mid-tool
    assert await db.fetchone("SELECT count(*) c FROM snapshots") is not None
    unfinished = await manager.events.unfinished_snapshots()
    assert unfinished and unfinished[0]["session_id"] == state.session.id

    provider2 = ScriptedProvider([{"tool": "Exec", "args": {"command": "echo resumed"}}, {"text": "after restart"}])
    manager2 = await _manager(settings, db, provider2)
    waiter = asyncio.create_task(_wait_finished(manager2))
    resumed = await manager2.resume_unfinished()
    assert resumed
    finished = await waiter
    assert finished[0][2] == "completed"
    history = await manager2.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert history[0].role is MessageRole.user and history[-1].role is MessageRole.assistant
    assert history[-1].content_blocks[0].text == "after restart"  # type: ignore[union-attr]
    await manager2.close()


@pytest.mark.parametrize("answer", [[{"selected": ["Blue"]}]])
async def test_ask_user_pause_and_answer(settings: Settings, db: Database, answer: list[dict[str, Any]]) -> None:
    provider = ScriptedProvider(
        [
            {"tool": "AskUser", "args": {"questions": [{"question": "Color?", "options": [{"label": "Red"}, {"label": "Blue"}]}]}},
            {"text": "you chose Blue"},
        ]
    )
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "ask me")
    finished = await waiter
    assert finished[0][2] == "awaiting" and state.pending is not None
    waiter2 = asyncio.create_task(_wait_finished(manager))
    await manager.answer(state.session.id, answer, via="app")
    finished2 = await waiter2
    assert finished2[-1][2] == "completed"
    tool_msg = [m for m in provider.requests[-1].messages if m.role is MessageRole.tool][-1]
    assert "Blue" in tool_msg.content_blocks[0].content  # type: ignore[union-attr]
    await manager.close()


async def test_waiting_session_survives_restart(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider(
        [{"tool": "AskUser", "args": {"questions": [{"question": "Go?", "options": [{"label": "Yes"}]}]}}, {"text": "went"}]
    )
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "ask")
    assert (await waiter)[0][2] == "awaiting"
    await manager.close()

    manager2 = await _manager(settings, db, ScriptedProvider([{"text": "went"}]))
    restored: list[str] = []

    async def on_restored(session_id: str, pending) -> None:  # type: ignore[no-untyped-def]
        restored.append(session_id)

    manager2.on_pending_restored(on_restored)
    assert await manager2.resume_unfinished() == []
    assert restored == [state.session.id]
    state2 = await manager2.get_state(state.session.id)
    assert state2 is not None and state2.pending is not None
    waiter2 = asyncio.create_task(_wait_finished(manager2))
    await manager2.answer(state.session.id, [{"selected": ["Yes"]}], via="app")
    assert (await waiter2)[-1][2] == "completed"
    await manager2.close()


async def test_delete_session_removes_records_and_workspace(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("gone")
    (state.workspace / "inbox" / "f.txt").write_text("x")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello")
    await waiter
    assert await manager.delete_session(state.session.id)
    assert state.workspace.exists(), "deleting a session never deletes its project's files"
    for table in ("sessions", "session_messages", "runs", "live_control"):
        row = await db.fetchone(f"SELECT count(*) c FROM {table}")
        assert row["c"] == 0, table
    assert await manager.get_state(state.session.id) is None
    await manager.close()


async def test_rename_updates_session_and_topic(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db, ScriptedProvider([{"text": "hi"}]))
    state = await manager.create_session("old")
    await manager.db.execute(
        "INSERT INTO topics(chat_id, thread_id, session_id, title, created_at) VALUES (?, ?, ?, ?, ?)",
        (-1, 5, state.session.id, "old", "2026-01-01T00:00:00+00:00"),
    )
    renamed = await manager.rename_session(state.session.id, "  new title  ")
    assert renamed.session.title == "new title"
    row = await manager.db.fetchone("SELECT title FROM topics WHERE session_id = ?", (state.session.id,))
    assert row["title"] == "new title"
    assert (await manager.sessions.get(state.session.id, "daedalus")).title == "new title"
    await manager.close()


SECTIONED = "## Goal\ng\n## Constraints\nc\n## State\ns\n## Discoveries\nd\n## Open\no\n## Next steps\nn\n## Unknowns\nu\n## Identifiers\ni"


def test_summary_sections_are_validated() -> None:
    from daedalus.host.session_runner import validate_summary_sections

    assert validate_summary_sections(SECTIONED) == ""
    assert "appears 0 times" in validate_summary_sections("## Goal\nx")
    assert "appears 2 times" in validate_summary_sections(SECTIONED + "\n## State\nagain")
    assert validate_summary_sections("## Constraints\nc\n## Goal\ng\n## State\ns\n## Discoveries\nd\n## Open\no\n## Next steps\nn\n## Unknowns\nu\n## Identifiers\ni") == "sections are out of order"


def test_transcript_for_summary_clips_tool_results() -> None:
    from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolUseBlock

    from daedalus.host.session_runner import transcript_for_summary

    history = [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="do it")]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Exec", arguments_json='{"command": "ls"}')]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="c1", content="x" * 5000)]),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="done")]),
    ]
    text = transcript_for_summary(history, result_chars=100)
    assert "[user] do it" in text and "[tool call] Exec" in text and "[assistant] done" in text
    assert " … " in text and len(text) < 1000


async def test_compaction_reports_its_progress_to_the_listeners(settings: Settings, db: Database) -> None:
    """While the summary is written the session says so: stage and parts done, then nothing again."""
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("c")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello there")
    await waiter
    seen: list[dict[str, Any] | None] = []

    async def sink(sid: str, event: Any) -> None:
        if event.payload.get("reason") == "compaction_progress":
            seen.append(dict(event.payload["compacting"]) if event.payload["compacting"] else None)

    manager.add_sink(sink)

    async def fake_summarise(provider, model, history, *, language, instructions, observability, progress=None):  # type: ignore[no-untyped-def]
        assert state.compacting is not None and state.compacting["stage"] == "summarising"
        await progress(parts_total=2, parts_done=0)
        await progress(parts_done=1)
        await progress(stage="merging")
        return SECTIONED

    manager._summarise_history = fake_summarise  # type: ignore[method-assign]
    await manager.compact(state.session.id)
    assert state.compacting is None
    stages = [(p["stage"], p["parts_done"], p["parts_total"]) if p else None for p in seen]
    assert stages == [("summarising", 0, 0), ("summarising", 0, 2), ("summarising", 1, 2), ("merging", 1, 2), ("writing", 1, 2), None]
    assert (await manager.list_sessions(limit=5))[0]["status"] == "idle"


async def test_compact_replaces_history_with_summary_and_keeps_a_backup(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("c")
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "hello there")
    await waiter

    async def fake_complete(request):  # type: ignore[no-untyped-def]
        from protocore.contracts.llm import LLMResponse
        from protocore.contracts.types import Message, MessageRole, StopReason, TextBlock

        assert "hello there" in request.messages[0].content_blocks[0].text
        calls.append(1)
        text = "## Goal\nx\n## State\ny" if len(calls) == 1 else SECTIONED
        return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=text)]), stop_reason=StopReason.end_turn)

    calls: list[int] = []
    provider.complete_text = fake_complete  # type: ignore[attr-defined]
    summary = await manager.compact(state.session.id)
    assert len(calls) == 2, "a summary missing sections is rejected and asked for again"
    assert summary.startswith("## Goal") and "## Recent operator messages (verbatim)" in summary and "hello there" in summary
    messages = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert len(messages) == 1 and messages[0].metadata.get("protocore.compaction_summary") is True
    assert "<compacted-turn" in messages[0].content_blocks[0].text
    assert "archived turns seq" in messages[0].content_blocks[0].text and messages[0].metadata["daedalus.archived"]["from_seq"] >= 1
    backups = list(state.workspace.glob(".history-*.jsonl"))
    assert len(backups) == 1 and "hello there" in backups[0].read_text()
    assert state.engine is not None and len(state.engine.history) == 1 and state.engine.last_observed_prompt_tokens == 0
    await manager.close()


async def test_compact_refuses_while_running(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("busy")
    state.task = asyncio.create_task(asyncio.sleep(SETTLE))
    with pytest.raises(RuntimeError):
        await manager.compact(state.session.id)
    state.task.cancel()
    await manager.close()


async def test_steer_sent_while_running_lands_in_the_transcript(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("busy")
    state.task = asyncio.create_task(asyncio.sleep(SETTLE))
    await manager.submit(state.session.id, "change of plan")
    texts = [m.content_blocks[0].text for m in await manager.sessions.list_transcript(state.session.id)]  # type: ignore[union-attr]
    assert texts == ["change of plan"]
    state.task.cancel()
    await manager.close()


async def test_a_steer_the_core_placed_is_not_shown_twice_while_the_run_lasts(settings: Settings, db: Database) -> None:
    """The host records a steer when it is sent; the core later puts the same text into its history
    with a timestamp of its own. The live view must still show the operator's words once."""
    provider = ScriptedProvider([{"text": "hi"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("busy")
    state.task = asyncio.create_task(asyncio.sleep(SETTLE))
    await manager.submit(state.session.id, "change of plan")
    # queue_update tags the received copy, while the submitted copy remains an archive record.
    state.engine = SimpleNamespace(history=[Message(role=MessageRole.user, content_blocks=[TextBlock(text="change of plan")])])  # type: ignore[assignment]
    queued = await manager.queued_steers(state.session.id)
    await manager._dispatch_event(state, TurnEvent(type=EventType.QUEUE_UPDATE, run_id="received-run", payload={"placed": [queued[0]["id"]], "kind": "steer"}))
    shown = await manager.transcript(state.session.id)
    operator_said = [m for m in shown if m.role is MessageRole.user and m.metadata.get("daedalus.origin") == "operator"]
    assert [m.content_blocks[0].text for m in operator_said] == ["change of plan", "change of plan"]  # type: ignore[union-attr]
    # Only the received copy is drawn, in the core history's position after the tool batch.
    views = [message_view(m) for m in shown if m.role is MessageRole.user]
    assert [(v["origin"], v["internal"]) for v in views] == [("operator", True), ("operator", False)]
    state.engine = None
    state.task.cancel()
    await manager.close()


async def test_two_inputs_at_once_start_one_run_and_queue_the_other(settings: Settings, db: Database) -> None:
    """An operator message and a loop tick in the same instant used to pass the running check together."""
    provider = ScriptedProvider([{"tool": "Exec", "args": {"command": "sleep 1"}}, {"text": "done"}])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("t")
    waiter = asyncio.create_task(_wait_finished(manager))
    first, second = await asyncio.gather(manager.submit(state.session.id, "from the operator"), manager.submit(state.session.id, "loop tick", origin="loop"))
    assert first == second
    runs = await db.fetchall("SELECT id FROM runs WHERE session_id = ?", (state.session.id,))
    assert len(runs) == 1
    queued = await manager.live.load(state.session.id)
    assert [q["text"] for q in queued["steer"]] == ["loop tick"]
    await waiter
    await manager.close()


def test_clip_title_keeps_a_short_line_and_cuts_a_long_one_on_a_word() -> None:
    assert clip_title("  hello   there. ") == "hello there"
    assert clip_title("") == ""
    assert clip_title("...") == "..."
    long = "one two three four five six seven eight nine ten eleven twelve thirteen"
    clipped = clip_title(long)
    assert long.startswith(clipped)
    assert len(clipped) <= 48
    assert long[len(clipped)] == " "


async def test_an_auto_created_chat_is_named_from_its_first_message(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("draft title", metadata={"autotitle": True})
    await manager.entitle(state.session.id, "The login form rejects a correct password", "draft title")
    renamed = await manager.get_state(state.session.id)
    assert renamed is not None and renamed.session.title == "summary"
    project = await manager.projects.get(state.project.id)  # type: ignore[union-attr]
    assert project is not None and project.name == "summary"
    kept = await db.fetchone("SELECT json_extract(settings, '$.ephemeral') AS auto FROM projects WHERE id = ?", (project.id,))
    assert kept["auto"] in (1, True)
    await manager.close()


async def test_a_renamed_chat_keeps_the_name_the_operator_gave(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("draft title", metadata={"autotitle": True})
    await manager.rename_session(state.session.id, "Kept")
    await manager.entitle(state.session.id, "The login form rejects a correct password", "draft title")
    renamed = await manager.get_state(state.session.id)
    assert renamed is not None and renamed.session.title == "Kept"
    await manager.close()


async def test_the_first_message_schedules_a_title_and_a_second_does_not(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([{"text": "done"}, {"text": "again"}])
    manager = await _manager(settings, db, provider)
    seen: list[tuple[str, str, str]] = []
    manager._schedule_entitle = lambda session_id, text, provisional: seen.append((session_id, text, provisional))  # type: ignore[method-assign]
    state = await manager.create_session("draft title", metadata={"autotitle": True})
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "The login form rejects a correct password")
    await waiter
    assert seen == [(state.session.id, "The login form rejects a correct password", "draft title")]
    assert "autotitle" not in state.metadata
    assert "autotitle" not in state.session.metadata
    waiter = asyncio.create_task(_wait_finished(manager))
    await manager.submit(state.session.id, "and a follow-up")
    await waiter
    assert len(seen) == 1
    await manager.close()
