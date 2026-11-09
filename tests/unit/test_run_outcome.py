"""A run that ended without an answer closes its turn with why, and where it gave out."""

from __future__ import annotations

from protocore.contracts.types import MessageRole

from daedalus.host.run_outcome import OUTCOME_METADATA_KEY, outcome_message, outcome_note, run_outcome
from daedalus.host.transcript_view import message_view


def test_a_run_that_answered_has_no_closing_line() -> None:
    assert run_outcome("completed") is None
    assert run_outcome("awaiting") is None


def test_the_cause_is_named_for_each_way_a_run_can_die() -> None:
    assert run_outcome("failed", error_kind="llm_context_window_exceeded")["cause"] == "context"  # type: ignore[index]
    assert run_outcome("failed", error_kind="compaction_exhausted")["cause"] == "compaction"  # type: ignore[index]
    assert run_outcome("failed", error_kind="llm_timeout")["cause"] == "provider"  # type: ignore[index]
    assert run_outcome("failed", error_kind="hook_denied")["cause"] == "error"  # type: ignore[index]
    assert run_outcome("cancelled")["cause"] == "cancelled"  # type: ignore[index]


def test_the_line_says_there_is_no_answer_why_and_which_tier_gave_out() -> None:
    outcome = run_outcome(
        "failed",
        error_kind="llm_context_window_exceeded",
        error_message="reactive force_compaction exhausted retries",
        compaction={"outcome": "at_floor", "tier2_failures": {"transport": 12}, "tier3_failures": {"timeout": 1}, "floor_dropped": 4},
        steps=176,
        last_tool="Write",
    )
    assert outcome is not None
    note = outcome_note(outcome)
    assert note.startswith("The run ended without an answer: the conversation no longer fitted")
    assert "reactive force_compaction exhausted retries." in note
    assert "The last compaction pass ended at_floor; summariser failures timeout 1, transport 12; the floor removed 4 messages." in note
    assert note.endswith("It had taken 176 steps, the last one Write.")


def test_a_provider_stop_keeps_the_engine_class_of_the_failure() -> None:
    # The line's technical details name the provider's failure by the class the engine gave it, beside
    # the kind: "llm_provider_error" alone does not tell an unreachable host from a refused key.
    outcome = run_outcome("failed", error_kind="llm_provider_error", error_reason="connection", error_message="transport error")
    assert outcome is not None and outcome["reason"] == "connection" and outcome["cause"] == "provider"
    assert run_outcome("failed", error_kind="hook_denied")["reason"] == ""  # type: ignore[index]


def test_the_closing_line_is_a_transcript_row_the_app_can_draw() -> None:
    outcome = run_outcome("failed", error_kind="llm_rate_limit", error_message="429 from the provider")
    assert outcome is not None
    message = outcome_message(outcome, "run-9")
    assert message.role is MessageRole.system
    view = message_view(message)
    assert view["outcome"] == outcome and view["run_id"] == "run-9"
    assert message.metadata[OUTCOME_METADATA_KEY]["answered"] is False


async def test_a_failed_run_writes_its_closing_line_to_the_transcript_only(settings, db) -> None:  # type: ignore[no-untyped-def]
    from daedalus.config import RuntimeConfig
    from daedalus.host.session_runner import SessionManager

    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    try:
        state = await manager.create_session("loop")
        state.last_error_kind = "llm_context_window_exceeded"
        state.last_error_message = "reactive force_compaction exhausted retries"
        state.last_compaction = {"outcome": "at_floor", "tier2_failures": {"transport": 2}}
        state.last_error_reason = "context_overflow"
        await manager._close_without_answer(state, "tick-189", "failed")
        rows = await manager.sessions.list_transcript(state.session.id)
        closing = [row for row in rows if row.metadata.get(OUTCOME_METADATA_KEY)]
        assert len(closing) == 1 and closing[0].metadata["daedalus.run_id"] == "tick-189"
        assert state.last_outcome is not None and state.last_outcome["cause"] == "context"
        assert closing[0].metadata[OUTCOME_METADATA_KEY]["reason"] == "context_overflow"
        # Not in the working history the model reads.
        history = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
        assert not [m for m in history if m.metadata.get(OUTCOME_METADATA_KEY)]
        # A run that answered writes nothing and clears the outcome.
        await manager._close_without_answer(state, "tick-190", "completed")
        assert state.last_outcome is None
        assert len([r for r in await manager.sessions.list_transcript(state.session.id) if r.metadata.get(OUTCOME_METADATA_KEY)]) == 1
    finally:
        await manager.close()
