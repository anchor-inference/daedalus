"""Other programs' sessions as Daedalus messages: the tool mapping, thinking kept out of the history,
images, secrets, the strictly increasing times the transcript's identity needs, and which part of a
long session the working history keeps. The turns are synthetic, in the normalised shape the host
terminal daemon hands over for Claude Code and for Codex."""

from __future__ import annotations

import base64
import json
from typing import Any

from protocore.contracts.types import (
    COMPACTION_SUMMARY_METADATA_KEY,
    MessageRole,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from daedalus.config import ModelPresetConfig
from daedalus.host.foreign_sessions import (
    IMAGE_MAX_BYTES,
    IMPORTED_KEY,
    TAIL_MIN_USER_TURNS,
    Converter,
    large,
    match_preset,
    native_call,
    parse_patch,
    plan_history,
)
from daedalus.security.redact import MASK, Redactor
from daedalus.stores.sqlite import SqliteSessionStore

AT = "2026-10-01T10:00:00.123Z"
"""Claude Code writes one record per block, many with the same millisecond."""


def turn(seq: int, role: str, *parts: dict[str, Any], at: str = AT, sidechain: str = "", model: str = "") -> dict[str, Any]:
    return {"seq": seq, "ext_id": f"rec-{seq}", "parent": f"rec-{seq - 1}" if seq else "", "role": role, "at": at,
            "sidechain": sidechain, "model": model, "parts": list(parts)}


def text(value: str) -> dict[str, Any]:
    return {"kind": "text", "text": value}


def call(call_id: str, name: str, payload: Any, tool_kind: str = "native", server: str = "") -> dict[str, Any]:
    part: dict[str, Any] = {"kind": "tool_call", "call_id": call_id, "name": name, "input": payload, "tool_kind": tool_kind}
    if server:
        part["server"] = server
    return part


def result(call_id: str, output: str, *, error: bool = False) -> dict[str, Any]:
    return {"kind": "tool_result", "call_id": call_id, "output": output, "is_error": error, "truncated": False, "images": []}


def claude_session() -> list[dict[str, Any]]:
    return [
        turn(0, "user", text("fix the flaky door sensor test")),
        turn(1, "assistant", {"kind": "thinking", "text": "the test sleeps instead of waiting"},
             call("toolu_1", "Bash", {"command": "pytest -q tests/test_door.py", "description": "run the tests", "timeout": 120000}), model="claude-opus-4-1-20250805"),
        turn(2, "user", result("toolu_1", "1 failed, 12 passed")),
        turn(3, "assistant", call("toolu_2", "Read", {"file_path": "/home/someone/door/tests/test_door.py", "offset": 10, "limit": 40})),
        turn(4, "user", result("toolu_2", "def test_open():\n    time.sleep(0.05)\n")),
        turn(5, "assistant", call("toolu_3", "Edit", {"file_path": "/home/someone/door/tests/test_door.py", "old_string": "time.sleep(0.05)", "new_string": "wait_for(door.open)"})),
        turn(6, "user", result("toolu_3", "edited")),
        turn(7, "assistant", call("toolu_4", "TodoWrite", {"todos": [{"content": "find the cause", "status": "completed"}, {"content": "fix it", "status": "in_progress"}]})),
        turn(8, "user", result("toolu_4", "Todos have been modified successfully")),
        turn(9, "assistant", call("toolu_5", "TodoWrite", {"todos": [{"content": "find the cause", "status": "completed"}, {"content": "fix it", "status": "completed"}]})),
        turn(10, "user", result("toolu_5", "Todos have been modified successfully")),
        turn(11, "assistant", call("toolu_6", "mcp__notes__append", {"text": "door test fixed"}, "mcp", "notes"), text("The test waits for the door now.")),
        turn(12, "user", result("toolu_6", "appended")),
    ]


def codex_session() -> list[dict[str, Any]]:
    patch_one = "*** Begin Patch\n*** Update File: main.c\n@@\n int pin = 4;\n-int delay = 10;\n+int delay = 20;\n*** End Patch\n"
    patch_two = "*** Begin Patch\n*** Add File: board.h\n+#define PIN 4\n*** Update File: main.c\n@@\n-old\n+new\n*** End Patch\n"
    return [
        turn(0, "user", text("<environment_context>\n  <cwd>/home/someone/fw</cwd>\n</environment_context>")),
        turn(1, "user", text("build it for the C3 board")),
        turn(2, "assistant", {"kind": "thinking", "encrypted": True},
             {"kind": "tool_call", "call_id": "call_1", "name": "shell", "input": json.dumps({"command": ["bash", "-lc", "idf.py build"], "workdir": "/home/someone/fw", "timeout_ms": 600000})}),
        turn(3, "assistant", result("call_1", "Project build complete.")),
        turn(4, "assistant", {"kind": "tool_call", "call_id": "call_2", "name": "apply_patch", "input": patch_one, "tool_kind": "custom"}),
        turn(5, "assistant", result("call_2", "Success. Updated the following files:\nM main.c")),
        turn(6, "assistant", {"kind": "tool_call", "call_id": "call_3", "name": "apply_patch", "input": patch_two}),
        turn(7, "assistant", result("call_3", "Success.")),
        turn(8, "assistant", {"kind": "tool_call", "call_id": "call_4", "name": "update_plan", "input": {"plan": [{"step": "build", "status": "completed"}, {"step": "flash", "status": "pending"}]}}),
        turn(9, "assistant", result("call_4", "Plan updated")),
        turn(10, "assistant", {"kind": "tool_call", "call_id": "call_5", "name": "exec_command", "input": {"cmd": "idf.py flash", "workdir": "/home/someone/fw"}}),
        turn(11, "assistant", result("call_5", "No serial port found", error=True)),
        turn(12, "assistant", text("The build passes; flashing needs the board plugged in.")),
    ]


def history(conversion: Any) -> list[Any]:
    return [e.history for e in conversion.entries if e.history is not None]


def test_a_claude_session_keeps_its_tools_as_ours_and_its_thinking_out_of_the_history() -> None:
    conversion = Converter("claude").convert(claude_session())
    messages = history(conversion)
    assert messages[0].role is MessageRole.user and messages[0].metadata["daedalus.origin"] == "operator"
    calls = [b for m in messages for b in m.content_blocks if isinstance(b, ToolUseBlock)]
    assert [c.name for c in calls] == ["Exec", "Read", "Edit"], "Bash, Read and Edit have exact equivalents"
    assert json.loads(calls[0].arguments_json) == {"command": "pytest -q tests/test_door.py", "timeout_seconds": 120}
    assert json.loads(calls[1].arguments_json) == {"path": "/home/someone/door/tests/test_door.py", "offset": 10, "limit": 40}
    assert json.loads(calls[2].arguments_json)["old_string"] == "time.sleep(0.05)"
    assert not any(isinstance(b, ThinkingBlock) for m in messages for b in m.content_blocks), "a foreign signature cannot be sent back"
    shown = conversion.entries[1].transcript
    assert isinstance(shown.content_blocks[0], ThinkingBlock), "the transcript keeps the thinking"
    assert shown.metadata[IMPORTED_KEY]["tools"] == {"toolu_1": "Bash"}, "the step is labelled with the program's own name"
    assert shown.metadata[IMPORTED_KEY]["model"] == "claude-opus-4-1-20250805"

    # Every call the model sees is answered right after it.
    for index, message in enumerate(messages):
        for block in message.content_blocks:
            if isinstance(block, ToolUseBlock):
                following = messages[index + 1]
                assert following.role is MessageRole.tool and following.content_blocks[0].tool_call_id == block.tool_call_id

    texts = [b.text for m in messages for b in m.content_blocks if isinstance(b, TextBlock) and m.role is MessageRole.assistant]
    plans = [t for t in texts if "TodoWrite" in t]
    assert plans == ["[Claude Code · TodoWrite] Plan: [x] find the cause  [x] fix it"], "only the latest plan stays in the history"
    assert sum("TodoWrite" in m.transcript.text for m in conversion.entries) == 2, "the transcript keeps every plan"
    assert any(t.startswith("[Claude Code · mcp__notes__append] {\"text\":\"door test fixed\"} → appended") for t in texts)
    assert "mcp__notes__append" not in [c.name for c in calls], "a tool the model does not have is never a call it could repeat"
    assert conversion.counts["thinking"] == 1 and conversion.counts["native_calls"] == 3 and conversion.counts["text_calls"] == 3


def test_every_message_has_its_own_time_so_the_transcript_keeps_them_all() -> None:
    conversion = Converter("claude").convert(claude_session())
    times = [e.transcript.created_at for e in conversion.entries]
    assert all(later > earlier for earlier, later in zip(times, times[1:], strict=False)), "the same millisecond everywhere, strictly increasing anyway"
    keys = [SqliteSessionStore.transcript_key(e.transcript) for e in conversion.entries]
    assert len(set(keys)) == len(keys)
    for entry in conversion.entries:
        if entry.history is not None:
            assert SqliteSessionStore.transcript_key(entry.history) == SqliteSessionStore.transcript_key(entry.transcript)
    later = Converter("claude", after=conversion.last_at).convert([turn(13, "user", text("one more thing"))])
    assert later.entries[0].transcript.created_at > times[-1], "a refresh continues after the last imported message"


def test_a_codex_session_unwraps_its_shell_and_reads_its_patches() -> None:
    conversion = Converter("codex").convert(codex_session())
    messages = history(conversion)
    assert messages[0].text == "build it for the C3 board", "the environment block is the program's, not the operator's"
    calls = {b.tool_call_id: b for m in messages for b in m.content_blocks if isinstance(b, ToolUseBlock)}
    assert json.loads(calls["call_1"].arguments_json) == {"command": "idf.py build", "cwd": "/home/someone/fw", "timeout_seconds": 600}
    assert calls["call_2"].name == "Edit" and json.loads(calls["call_2"].arguments_json) == {
        "path": "main.c", "old_string": "int pin = 4;\nint delay = 10;", "new_string": "int pin = 4;\nint delay = 20;"}
    assert "call_3" not in calls, "a patch over two files has no single equivalent"
    patched = next(b.text for m in messages for b in m.content_blocks if isinstance(b, TextBlock) and "apply_patch" in b.text)
    assert patched.startswith("[Codex · apply_patch] changed: board.h (new), main.c (+1 −1)")
    assert json.loads(calls["call_5"].arguments_json) == {"command": "idf.py flash", "cwd": "/home/someone/fw"}
    failed = next(m for m in messages if m.role is MessageRole.tool and m.content_blocks[0].tool_call_id == "call_5")
    assert failed.content_blocks[0].is_error
    hidden = conversion.entries[1].transcript.content_blocks[0]
    assert isinstance(hidden, ThinkingBlock) and hidden.text == "[reasoning hidden by Codex]"
    assert any("[Codex · update_plan] Plan: [x] build  [ ] flash" in m.text for m in messages)


def test_secrets_are_masked_before_anything_is_built_from_them() -> None:
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    known = "operator-known-value-0042"
    turns = [
        turn(0, "user", text(f"use {token} and {known}")),
        turn(1, "assistant", call("t1", "Bash", {"command": f"curl -H 'Authorization: Bearer {known}' https://example.invalid"})),
        turn(2, "user", result("t1", f"GITHUB_TOKEN={token}")),
    ]
    conversion = Converter("claude", redactor=Redactor([known])).convert(turns)
    dumped = "".join(e.transcript.model_dump_json() for e in conversion.entries)
    assert token not in dumped and known not in dumped and MASK in dumped
    assert conversion.masked >= 3


def test_an_image_is_kept_by_its_reference_and_a_missing_one_is_said() -> None:
    picture = b"\x89PNG\r\n\x1a\nsynthetic"
    turns = [
        turn(0, "user", text("what is on this board?"), {"kind": "image", "mime": "image/png", "data_b64": base64.b64encode(picture).decode(), "bytes": len(picture)}),
        turn(1, "user", text("and this one"), {"kind": "image", "mime": "image/png", "path": "/home/someone/gone.png", "bytes": IMAGE_MAX_BYTES + 1}),
    ]
    conversion = Converter("claude").convert(turns)
    first, second = (e.history for e in conversion.entries)
    assert first is not None and second is not None
    ref = first.metadata["image_refs"][0]["ref"]
    assert conversion.blobs[ref] == (picture, "image/png")
    assert "[image: /home/someone/gone.png, unavailable]" in second.text and "image_refs" not in second.metadata


def test_a_call_left_without_a_result_is_said_rather_than_left_hanging() -> None:
    turns = [turn(0, "user", text("run it")), turn(1, "assistant", call("t9", "Bash", {"command": "make"}))]
    conversion = Converter("claude").convert(turns)
    messages = history(conversion)
    assert not any(isinstance(b, ToolUseBlock) for m in messages for b in m.content_blocks)
    assert "no result was recorded (the call was interrupted)" in messages[-1].text


def test_a_sub_agent_is_text_in_the_history_and_its_turns_follow_in_the_transcript() -> None:
    turns = [
        turn(0, "user", text("look into the logs")),
        turn(1, "assistant", call("task_1", "Task", {"description": "read logs", "prompt": "find the error in /var/log/app.log"})),
        turn(2, "user", text("find the error in /var/log/app.log"), sidechain="task_1"),
        turn(3, "assistant", text("The error is a timeout."), sidechain="task_1"),
        turn(4, "user", result("task_1", "The error is a timeout.")),
        turn(5, "assistant", text("It is a timeout.")),
    ]
    conversion = Converter("claude").convert(turns)
    agent = conversion.entries[1]
    assert agent.history is not None and agent.history.text.startswith("[Claude Code · sub-agent «read logs»] task: find the error")
    chained = [e for e in conversion.entries if e.sidechain]
    assert [e.transcript.text for e in chained] == ["find the error in /var/log/app.log", "The error is a timeout."]
    assert all(e.history is None for e in chained), "a sub-agent's own turns are in the transcript and the search, not in the history"
    assert conversion.entries.index(chained[0]) == 2


def test_the_full_history_starts_at_the_programs_last_compaction() -> None:
    turns = [
        turn(0, "user", text("old question")), turn(1, "assistant", text("old answer")),
        turn(2, "system_note", {"kind": "compaction", "summary": "We fixed the door test.", "covers": [0, 1], "auto": True}),
        turn(3, "user", text("new question")), turn(4, "assistant", text("new answer")),
    ]
    conversion = Converter("claude").convert(turns)
    plan = plan_history(conversion, mode="full", window=128_000)
    kept = [conversion.entries[i].history for i in plan.kept]
    assert kept[0] is not None and kept[0].metadata[COMPACTION_SUMMARY_METADATA_KEY]
    assert "<compacted-turn id='foreign:claude'>We fixed the door test.</compacted-turn>" == kept[0].text
    assert [m.text for m in kept[1:] if m is not None] == ["new question", "new answer"]
    assert plan.archived == [0, 1], "what the summary stands for is what HistoryExpand returns"


def long_session(pairs: int, *, output: int = 200) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = [turn(0, "system_note", {"kind": "compaction", "summary": "Earlier: the firmware was ported.", "auto": True})]
    seq = 1
    for n in range(pairs):
        turns.append(turn(seq, "user", text(f"step {n}")))
        turns.append(turn(seq + 1, "assistant", call(f"c{n}", "Bash", {"command": f"make step{n}"})))
        turns.append(turn(seq + 2, "user", result(f"c{n}", "x" * output)))
        turns.append(turn(seq + 3, "assistant", text(f"done {n}")))
        seq += 4
    return turns


def test_a_long_session_keeps_a_tail_that_starts_with_the_operator_and_breaks_no_call() -> None:
    conversion = Converter("claude").convert(long_session(600))
    assert large(conversion, 128_000), "more than two thousand messages is too many to start from"
    plan = plan_history(conversion, mode="tail", window=128_000)
    assert plan.mode == "tail" and plan.summary == 0
    entries = conversion.entries
    first = entries[plan.kept[0]]
    assert first.operator, "the tail opens with something the operator said"
    assert sum(1 for i in plan.kept if entries[i].operator) >= TAIL_MIN_USER_TURNS
    assert conversion.tokens([entries[i] for i in plan.kept], tail=True) <= 0.25 * 128_000 + 500
    assert plan.gap and plan.gap[0] == 1 and plan.gap[-1] == plan.kept[0] - 1
    assert plan.archived == list(range(plan.kept[0]))
    calls = {b.tool_call_id for i in plan.kept for b in (entries[i].history.content_blocks if entries[i].history else ()) if isinstance(b, ToolUseBlock)}
    answers = {b.tool_call_id for i in plan.kept for b in (entries[i].history.content_blocks if entries[i].history else ()) if isinstance(b, ToolResultBlock)}
    assert calls == answers


def test_the_tail_cuts_long_outputs_the_transcript_keeps() -> None:
    conversion = Converter("claude").convert(long_session(4, output=50_000))
    result_entry = next(e for e in conversion.entries if e.transcript.role is MessageRole.tool)
    assert len(result_entry.transcript.content_blocks[0].content) == 50_000
    assert result_entry.history is not None and len(result_entry.history.content_blocks[0].content) == 50_000
    assert result_entry.tail_history is not None and len(result_entry.tail_history.content_blocks[0].content) < 4_200


def test_a_short_session_asked_for_a_tail_stays_whole() -> None:
    conversion = Converter("claude").convert(claude_session())
    assert not large(conversion, 128_000)
    plan = plan_history(conversion, mode="tail", window=128_000)
    assert plan.mode == "full" and not plan.archived


def test_the_same_model_is_found_whatever_its_spelling() -> None:
    presets = {"ds": ModelPresetConfig(provider="deepseek", model="deepseek-chat"),
               "opus": ModelPresetConfig(provider="openrouter", model="anthropic/claude-opus-4.1")}
    assert match_preset(presets, "claude-opus-4-1-20250805") == "opus"
    assert match_preset(presets, "gpt-5-codex") is None
    assert match_preset(presets, "") is None


def test_native_mapping_refuses_what_ours_would_do_differently() -> None:
    assert native_call("Bash", {"command": ""}) is None
    assert native_call("mcp__fs__read", {"path": "/x"}, "mcp") is None
    assert native_call("read", {"filePath": "/x"}) == ("Read", {"path": "/x"}), "opencode's spelling"
    assert native_call("Grep", {"pattern": "TODO", "-i": True, "glob": "*.py"}) == ("Search", {"pattern": "TODO", "glob": "*.py", "case_insensitive": True})
    assert native_call("LS", {"path": "/home/someone/a b"}) == ("Exec", {"command": "ls -la '/home/someone/a b'"})
    assert native_call("Write", {"file_path": "/x", "content": "y" * (200 * 1024)}) is None, "over the core's argument bound"
    moved = parse_patch("*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@\n-x\n+y\n*** End Patch")
    assert moved[0]["move"] == "b.py" and native_call("apply_patch", {"input": "*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@\n-x\n+y\n*** End Patch"}) is None
