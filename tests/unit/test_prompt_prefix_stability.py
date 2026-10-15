"""What a request carries of the conversation stays the request before it, plus what was added.

The provider's prompt cache serves a request up to the first byte that differs from one it has seen,
so every rewrite of an earlier message costs the rest of the conversation written into the cache
again. These tests replay the shapes that used to rewrite history in the middle of a run.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from protocore.contracts.turn_policy import TurnContext, TurnCoordinate
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    Message,
    MessageRole,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from protocore.prompts import bundled_prompt_provider
from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType
from protocore.runtime.stale_result_trim import trim_stale_results
from protocore.runtime.turn_policies import TurnPolicyRegistry

from daedalus.config import RuntimeConfig, Settings
from daedalus.host.engine_factory import runtime_constants, stale_trim_batch_chars
from daedalus.stores.database import Database
from tests.support.waiting import until
from tests.unit.test_session_runner import ScriptedProvider, _manager

PROMPTS = bundled_prompt_provider()

READS = (42_195, 14_292, 19_443, 2_738, 12_590, 13_455, 12_562, 5_003, 9_160, 26_733, 33_163, 39_944, 7_200, 13_154, 4_090, 16_126, 25_986, 8_757, 3_588, 23_405)
"""Result sizes, in characters, of the documents a reviewer read one after another on 2026-10-15
(Claude Opus, 980k window): the run in which the request shrank seven times in 68 calls."""


def _reviewer_run(calls: int) -> list[list[Message]]:
    """The history as each model call of such a run saw it: one more call and its result each time."""
    history = [
        Message(role=MessageRole.system, content_blocks=[TextBlock(text="system")]),
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="Check the three documents against the checklist.")]),
    ]
    snapshots: list[list[Message]] = []
    for index in range(calls):
        call_id = f"call_{index}"
        history.append(Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id=call_id, name="Exec", arguments_json="{}")]))
        history.append(Message(role=MessageRole.user, content_blocks=[ToolResultBlock(tool_call_id=call_id, content="д" * READS[index % len(READS)])]))
        snapshots.append(list(history))
    return snapshots


def _rewrites(config: RuntimeConfig, *, window: int, calls: int = 68) -> int:
    """How many requests of the replayed run changed a message an earlier request had already sent."""
    rc = runtime_constants(config, context_window=window, max_output_tokens=64_000, thinking=True)
    trimmed: frozenset[str] = frozenset()
    previous: list[str] | None = None
    rewrites = 0
    for history in _reviewer_run(calls):
        view, trimmed = trim_stale_results(history, rc, PROMPTS, already_trimmed=trimmed)
        sent = [message.model_dump_json() for message in view]
        if previous is not None and sent[: len(previous)] != previous:
            rewrites += 1
        previous = sent
    return rewrites


def test_a_long_window_run_of_long_reads_keeps_its_history_append_only() -> None:
    # The share-of-the-view rule alone trimmed whenever the stale excess grew back to a quarter of a
    # small view; on a 980k window that was every few calls, and each trim was a cache miss on the
    # whole conversation after the first result it cut.
    legacy = RuntimeConfig()
    legacy.tools.results.trim_window_share = 0.0
    assert _rewrites(legacy, window=980_000) >= 10
    # Scaled to the window, the run trims twice, each time in one large step, and every other
    # request carries the one before it byte for byte.
    assert _rewrites(RuntimeConfig(), window=980_000) <= 2
    assert _rewrites(RuntimeConfig(), window=980_000, calls=20) == 0


def test_the_trim_threshold_follows_the_window_and_never_falls_below_the_batch() -> None:
    config = RuntimeConfig()
    assert stale_trim_batch_chars(config, 980_000) == 294_000
    assert stale_trim_batch_chars(config, 128_000) == config.tools.results.trim_batch_chars
    rc = runtime_constants(config, context_window=980_000, max_output_tokens=64_000, thinking=True)
    assert rc.tool_result_stale_trim_batch_chars == 294_000
    config.tools.results.trim_window_share = 0.0
    assert stale_trim_batch_chars(config, 980_000) == config.tools.results.trim_batch_chars


def _sent(message: Message) -> str:
    """What of a message reaches the provider: a request builds its messages afresh, so their
    creation time differs from one request to the next and is never sent."""
    return message.model_dump_json(include={"role", "content_blocks", "reasoning_content"})


class _CompactOnce:
    """Stands in for the core's compaction gate: at the second gap between a batch and the next
    request, the oldest round becomes a summary, as a compaction in the middle of a run leaves it.
    That round was never handed over to the host: the core does that at the start of a run."""

    name = "per_iteration_compaction"
    coordinates = frozenset({TurnCoordinate.iteration_end})

    def __init__(self) -> None:
        self.seen = 0

    async def apply(self, turn: TurnContext) -> AsyncIterator[TurnEvent]:
        self.seen += 1
        if self.seen != 2:
            return
        yield TurnEvent(type=EventType.COMPACTION_STARTED, run_id="r", payload={"reason": "proactive_per_iteration"})
        history = turn.engine.history
        summary = Message(
            role=MessageRole.user,
            content_blocks=[TextBlock(text="<compacted-turn id='auto'>the first command printed one</compacted-turn>")],
            metadata={COMPACTION_SUMMARY_METADATA_KEY: True},
        )
        history[1:3] = [summary]
        yield TurnEvent(type=EventType.COMPACTION_COMPLETED, run_id="r", payload={"reason": "proactive_per_iteration", "tokens_before": 100, "tokens_after": 50})


async def test_the_summary_of_a_compaction_in_a_run_is_labelled_before_the_next_request(settings: Settings, db: Database) -> None:
    provider = ScriptedProvider([
        {"tool": "Exec", "args": {"command": "echo one"}},
        {"tool": "Exec", "args": {"command": "echo two"}},
        {"tool": "Exec", "args": {"command": "echo three"}},
        {"text": "done"},
    ])
    manager = await _manager(settings, db, provider)
    state = await manager.create_session("compacting")
    build = manager._build_engine

    async def with_the_gate(*args: Any, **kwargs: Any) -> Any:
        engine = await build(*args, **kwargs)
        engine.turn_policies = TurnPolicyRegistry([_CompactOnce()])
        return engine

    manager._build_engine = with_the_gate  # type: ignore[method-assign]
    finished = asyncio.Event()

    async def on_finished(session_id: str, run_id: str, status: str) -> None:
        finished.set()

    manager.on_finished(on_finished)
    await manager.submit(state.session.id, "run three commands")
    await asyncio.wait_for(finished.wait(), timeout=30)
    await until(lambda: not manager._settling(state), "the run to settle")

    def summary_of(request: Any) -> str:
        texts = [block.text for message in request.messages if message.metadata.get(COMPACTION_SUMMARY_METADATA_KEY) for block in message.content_blocks if isinstance(block, TextBlock)]
        assert len(texts) == 1
        return texts[0]

    after = provider.requests[2:]
    assert len(after) == 2
    # The first request after the compaction already names the archived turns; the label used to
    # arrive a request later and rewrite the head of the conversation a second time.
    assert "[archived turns seq" in summary_of(after[0])
    first = [_sent(message) for message in after[0].messages]
    second = [_sent(message) for message in after[1].messages]
    assert second[: len(first)] == first
    stored = await manager.sessions.list_messages(state.session.id, "daedalus", limit=50)
    labelled = [m for m in stored if m.metadata.get(COMPACTION_SUMMARY_METADATA_KEY)]
    assert len(labelled) == 1 and labelled[0].content_blocks[0].text.count("[archived turns") == 1  # type: ignore[union-attr]
    # The round the summary replaced is in the transcript, where HistoryExpand finds it.
    archived = labelled[0].metadata["daedalus.archived"]
    expanded = await manager.sessions.expand_transcript(state.session.id, archived["from_seq"], archived["to_seq"])
    assert [message.role for _, message in expanded] == [MessageRole.assistant, MessageRole.tool]
    assert "call_1" in expanded[0][1].model_dump_json()
    await manager.close()
