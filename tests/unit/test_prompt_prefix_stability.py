"""What a request carries of the conversation stays the request before it, plus what was added.

The provider's prompt cache serves a request up to the first byte that differs from one it has seen,
so every rewrite of an earlier message costs the rest of the conversation written into the cache
again. These tests replay the shapes that used to rewrite history in the middle of a run.
"""

from __future__ import annotations

from protocore.contracts.types import Message, MessageRole, TextBlock, ToolResultBlock, ToolUseBlock
from protocore.prompts import bundled_prompt_provider
from protocore.runtime.stale_result_trim import trim_stale_results

from daedalus.config import RuntimeConfig
from daedalus.host.engine_factory import runtime_constants, stale_trim_batch_chars

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
