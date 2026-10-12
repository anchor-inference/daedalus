"""What a session keeps across a compaction, an import and a model switch.

The summariser sees the whole history and builds on the summary before it; the summary keeps every
open thread and the operator's standing instructions; the trigger follows the window; a failed
summary call is asked again; a smaller window compacts at once; subagents ask no peers, and a peer
answers only its own project.
"""

from __future__ import annotations

from typing import Any

import pytest
from protocore.contracts.llm import LLMProviderError, LLMRateLimitError, LLMResponse
from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    Message,
    MessageRole,
    StopReason,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from daedalus.config import (
    SUBAGENT_BLOCKED_TOOLS,
    CompactionConfig,
    ModelPresetConfig,
    RuntimeConfig,
    Settings,
    _migrate,
    compaction_ratio,
    core_compaction_trigger,
)
from daedalus.host import session_runner, standing, summaries
from daedalus.host.session_runner import CompactionFailed, SessionManager
from daedalus.providers.openai_compat import UsageRecord
from daedalus.stores.database import Database
from tests.support.models import DEFAULT_PRESET, model_config

SECTIONED = "## Standing instructions\ns\n## Stable facts\nf\n## Threads\n### t\nx\n## Closed threads\nc\n## Unknowns\nu\n## Identifiers\ni"


def _op(text: str) -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": "operator"})


def _say(text: str) -> Message:
    return Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=text)])


def _summary(text: str) -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=f"<compacted-turn id='auto'>{text}</compacted-turn>")], metadata={COMPACTION_SUMMARY_METADATA_KEY: True})


def _answer(text: str = SECTIONED) -> LLMResponse:
    return LLMResponse(message=Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=text)]), stop_reason=StopReason.end_turn)


# -- what the summariser is given -------------------------------------------------------------


def test_the_previous_summary_goes_apart_and_whole_and_nothing_is_cut_from_the_front() -> None:
    base = "## Threads\n### LANGPT-1278\n" + "known fact. " * 6000  # 72k characters, past the old 4,000 cut
    explanation = "The Jira PAT is on line 193 of CREDENTIALS.md. " * 600  # an operator explanation of 28k characters
    filler = [_say(f"step {i} " + "y" * 3000) for i in range(300)]  # over the old 200,000-character window
    history = [_summary(base), _op(explanation), *filler, _op("latest ask")]
    rendered = summaries.summary_input(history)
    assert rendered.previous == base.strip()
    assert f"[operator] {explanation.strip()}" in rendered.transcript, "an operator's message is never cut"
    assert rendered.transcript.index("[operator] The Jira PAT") < rendered.transcript.index("step 0 "), "the front of the history is kept"
    assert rendered.transcript.rstrip().endswith("[operator] latest ask")
    assert "<compacted-turn" not in rendered.previous


def test_reads_of_memory_files_keep_far_more_than_other_results() -> None:
    note = "client stand work-sandbox; deliver by format-patch. " * 200
    history = [
        _op("go"),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="m", name="Read", arguments_json='{"path": "/home/someone/.claude/projects/p/memory/stands.md"}')]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="m", content=note)]),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="l", name="Exec", arguments_json='{"command": "ls -R /"}')]),
        Message(role=MessageRole.tool, content_blocks=[ToolResultBlock(tool_call_id="l", content="z" * 10_000)]),
    ]
    text = summaries.summary_input(history).transcript
    memory_line = next(line for line in text.splitlines() if "work-sandbox" in line)
    other_line = next(line for line in text.splitlines() if line.startswith("[tool result] z"))
    assert len(memory_line) > 10_000 > summaries.RESULT_CHARS + 100 > len(other_line)


def test_a_summary_in_the_middle_is_read_in_its_place_uncut() -> None:
    middle = "core summary " * 1000
    history = [_op("first"), _summary(middle), _op("after")]
    rendered = summaries.summary_input(history)
    assert rendered.previous == ""
    assert f"[summary of earlier turns] {middle.strip()}" in rendered.transcript


def test_the_summary_has_a_section_per_thread_and_carries_instructions_forward() -> None:
    prompt = summaries.compact_prompt(language="English", max_words=4000, has_base=True)
    for heading in ("## Standing instructions", "## Stable facts", "## Threads", "### <short name>", "## Closed threads", "<previous-summary>"):
        assert heading in prompt
    assert "<previous-summary>" not in summaries.compact_prompt(language="English", max_words=4000, has_base=False)
    assert summaries.validate_summary_sections(SECTIONED) == ""
    assert summaries.summary_words(1200, 1_000_000) == 8000 and summaries.summary_words(1200, 400_000) == 4000 and summaries.summary_words(1200, 64_000) == 1200
    assert summaries.summary_max_tokens(8000) == 25_000


def _manager_with(settings: Settings, db: Database, config: RuntimeConfig | None = None) -> SessionManager:
    return SessionManager(settings, config or model_config(), db=db)


async def test_a_long_history_is_summarised_in_parts_merged_onto_the_previous_summary(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    config = model_config()
    config.compaction.chunk_tokens = 5_000  # 20,000 characters a call
    manager = _manager_with(settings, db, config)
    await manager.start()
    provider = manager.providers.get(manager.providers.available()[0])
    bodies: list[str] = []

    async def complete(request: Any) -> LLMResponse:
        text = request.messages[0].content_blocks[0].text
        bodies.append(text)
        return _answer(SECTIONED + "\n" + "note " * 2500)  # long notes: they have to be merged in rounds

    provider.complete_text = complete  # type: ignore[method-assign]
    base = "### LANGPT-1278\nsearch_2_books holds availability by subscriber_id"
    history = [_summary(base), *[_op(f"part {i} " + "w" * 9000) for i in range(12)]]
    summary = await manager._summarise_history(provider, "m", history, language="English", instructions="", observability=None, window=1_000_000)  # type: ignore[arg-type]
    assert summary.startswith("## Standing instructions")
    parts = [b for b in bodies if "ONE PART" in b]
    rounds = [b for b in bodies if b.startswith("Below are notes on consecutive parts of one conversation")]
    final = bodies[-1]
    assert len(parts) >= 6 and all("<previous-summary>" not in b for b in parts)
    assert rounds, "notes too long for one call are merged in groups first"
    assert "<previous-summary>\n" + base in final and "worst mistake" in final
    assert all(any(f"part {i} " in b for b in parts) for i in range(12)), "every part of the history reached the summariser"
    await manager.close()


async def test_a_failed_summary_call_is_asked_again_and_a_rate_limit_is_not(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_runner, "SUMMARY_RETRY_PAUSES", (0.0, 0.0))
    manager = _manager_with(settings, db)
    await manager.start()
    provider = manager.providers.get(manager.providers.available()[0])
    calls = 0

    async def flaky(request: Any) -> LLMResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise LLMProviderError("HTTP 500 from the provider")
        return _answer()

    provider.complete_text = flaky  # type: ignore[method-assign]
    assert await manager._summary_call(provider, "m", "p", "b", None) == SECTIONED  # type: ignore[arg-type]
    assert calls == 3

    async def limited(request: Any) -> LLMResponse:
        nonlocal calls
        calls += 1
        raise LLMRateLimitError("usage limit exceeded")

    calls = 0
    provider.complete_text = limited  # type: ignore[method-assign]
    with pytest.raises(LLMRateLimitError):
        await manager._summary_call(provider, "m", "p", "b", None)  # type: ignore[arg-type]
    assert calls == 1
    await manager.close()


async def test_a_compaction_that_keeps_failing_says_so_and_keeps_the_history(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_runner, "SUMMARY_RETRY_PAUSES", (0.0, 0.0))
    config = model_config()
    config.model.chain = []
    manager = _manager_with(settings, db, config)
    await manager.start()
    state = await manager.create_session("s")
    history = [_op("Never deploy on Fridays."), _say("noted"), _op("next"), _say("done")]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    for provider_id in manager.providers.available():
        provider = manager.providers.get(provider_id)

        async def broken(request: Any) -> LLMResponse:
            raise LLMProviderError("HTTP 500 from the provider")

        provider.complete_text = broken  # type: ignore[method-assign]
    notices: list[dict[str, Any]] = []

    async def post(session_id: str | None, payload: dict[str, Any]) -> None:
        notices.append(payload)

    manager.service_hooks["setting_notice"] = post
    with pytest.raises(CompactionFailed, match="HTTP 500"):
        await manager.compact(state.session.id)
    assert [n["outcome"] for n in notices] == ["failed"]
    kept = await manager.sessions.list_messages(state.session.id, "daedalus", limit=10)
    assert [m.content_blocks[0].text for m in kept] == ["Never deploy on Fridays.", "noted", "next", "done"]  # type: ignore[union-attr]
    await manager.close()


# -- when it compacts -------------------------------------------------------------------------


def test_the_trigger_follows_the_window() -> None:
    cfg = CompactionConfig()
    assert cfg.auto_ratio == 0.8
    assert compaction_ratio(cfg, 1_000_000, 64_000) == 0.8
    assert compaction_ratio(cfg, 400_000, 42_000) == 0.8
    assert compaction_ratio(cfg, 128_000, 32_000) == 0.5
    assert compaction_ratio(cfg, 256_000, 65_536) == pytest.approx(0.66)  # the core triggers at 0.69 here; the host stays 0.03 under it
    for window, cap in ((128_000, 32_000), (256_000, 65_536), (400_000, 42_000), (500_000, 64_000), (980_000, 64_000)):
        assert compaction_ratio(cfg, window, cap) < core_compaction_trigger(cfg, window, cap), "a run boundary compacts before the core does"
    assert compaction_ratio(CompactionConfig(auto_ratio=0.0), 400_000) == 0.0


def test_the_old_default_ratio_is_migrated_once() -> None:
    raw: dict[str, Any] = {"compaction": {"auto_ratio": 0.5}}
    _migrate(raw)
    assert raw["compaction"]["auto_ratio"] == 0.8 and "compaction-ratio-by-window" in raw["seeded"]
    raw["compaction"]["auto_ratio"] = 0.5  # chosen afterwards
    _migrate(raw)
    assert raw["compaction"]["auto_ratio"] == 0.5
    chosen: dict[str, Any] = {"compaction": {"auto_ratio": 0.6}}
    _migrate(chosen)
    assert chosen["compaction"]["auto_ratio"] == 0.6


async def test_moving_to_a_smaller_window_compacts_at_once(settings: Settings, db: Database) -> None:
    config = model_config()
    config.presets[DEFAULT_PRESET].context_window = 1_000_000
    config.presets["small"] = ModelPresetConfig(provider="deepseek", model="deepseek-small", context_window=128_000)
    config.compaction.keep_recent_messages = 2
    config.compaction.min_messages = 4
    manager = _manager_with(settings, db, config)
    await manager.start()
    state = await manager.create_session("big")
    history = [_op("Rule: deliver by format-patch."), _say("ok"), _op("second"), _say("done"), _op("third"), _say("fine")]
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    await manager.sessions.append_transcript(state.session.id, history)
    provider = manager.providers.get("deepseek")
    asked: list[str] = []

    async def complete(request: Any) -> LLMResponse:
        asked.append(request.model)
        return _answer()

    provider.complete_text = complete  # type: ignore[method-assign]
    await manager.usage.record(UsageRecord(provider_id="deepseek", model="deepseek-flash", purpose="stream", raw={}, normalized={"input_tokens": 300_000}, cost_usd=0.0, duration_ms=1, run_id="r1", session_id=state.session.id))
    await manager._maybe_auto_compact(state)
    assert not asked, "300k of a 1M window is no reason to compact"
    await manager.set_model(state.session.id, preset="small")
    assert state.auto_compaction is not None
    await state.auto_compaction
    messages = await manager.sessions.list_messages(state.session.id, "daedalus", limit=20)
    assert asked == ["deepseek-small"] and messages[0].metadata.get(COMPACTION_SUMMARY_METADATA_KEY) is True and len(messages) == 3
    await manager.close()


# -- what an imported session carries ---------------------------------------------------------


def test_carried_files_ride_whole_while_they_fit_and_are_named_when_they_do_not() -> None:
    files = [
        {"path": "/home/someone/.claude/CLAUDE.md", "kind": "user", "content": "Answer in Russian.", "bytes": 18},
        {"path": "/home/someone/proj/CLAUDE.md", "kind": "project", "content": "read live by the workspace notes", "bytes": 32},
        {"path": "/home/someone/.claude/projects/p/memory/MEMORY.md", "kind": "memory_index", "content": "- [stands](stands.md) work-sandbox", "bytes": 34},
        {"path": "/home/someone/.claude/projects/p/memory/huge.md", "kind": "memory", "content": "x" * 50_000, "bytes": 50_000},
        {"path": "/home/someone/.claude/projects/p/memory/late.md", "kind": "memory", "content": "", "bytes": 9000, "omitted": True},
    ]
    text = standing.render(files, source="Claude Code", window=128_000, workspace="/home/someone/proj")
    assert "never ask the operator again" in text and "Answer in Russian." in text and "work-sandbox" in text
    assert "read live by the workspace notes" not in text, "the session folder's own CLAUDE.md is the workspace notes' to read"
    assert "x" * 1000 not in text and "- /home/someone/.claude/projects/p/memory/huge.md (memory, 49 KB)" in text
    assert "late.md" in text
    assert "x" * 50_000 in standing.render(files, source="Claude Code", window=1_000_000)


async def test_an_older_daemon_is_a_reason_not_a_failure(settings: Settings, db: Database) -> None:
    from daedalus.terminals.bridge import HostDaemonOutdated

    manager = _manager_with(settings, db)

    class Bridge:
        async def sessions_instructions(self, harness: str, session: str) -> dict[str, Any]:
            raise HostDaemonOutdated("no such method")

    manager.host_bridge = Bridge()  # type: ignore[assignment]
    read = await standing.fetch(manager, "claude", "s1")
    assert read == {"files": [], "problem": standing.OUTDATED}


# -- subagents and peers ----------------------------------------------------------------------


def test_a_subagent_asks_nobody_and_starts_nobody() -> None:
    assert {"AskPeer", "PeerList", "AskUser", "SubAgent", "SpawnAgent", "Notify"} <= set(SUBAGENT_BLOCKED_TOOLS)


async def test_a_subagent_has_the_blocked_tools_withheld(settings: Settings, db: Database) -> None:
    manager = _manager_with(settings, db)
    await manager.start()
    leader = await manager.create_session("leader")
    child = await manager.create_session("child", metadata={"subagent_of": leader.session.id})
    blocked = manager.blocked_tools_for(child)
    assert {"AskPeer", "PeerList", "AskUser", "SubAgent"} <= blocked
    assert "AskPeer" not in manager.blocked_tools_for(leader)
    await manager.close()
