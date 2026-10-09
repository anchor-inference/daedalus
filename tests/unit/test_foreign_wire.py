"""The shapes the host terminal daemon answers ``sessions.scan`` and ``sessions.read`` with, read by
the host exactly as the daemon writes them.

The samples are not written by hand: ``ptyd/internal/sidechan/sessions/wire_golden_test.go`` builds
a synthetic Claude Code and Codex session, runs the daemon's own parsers over them and writes what
they answer. That test fails when the parsers' output stops matching the files, and this one fails
when the host stops reading them, so a field renamed on one side cannot pass both. The two layers of
an import are checked against the rules a provider holds a history to: every call answered by the
message right after it, no thinking, and no tool the model does not have."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from protocore.contracts.types import Message, MessageRole, TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

from daedalus.config import Settings
from daedalus.host.engine_factory import TENANT
from daedalus.host.foreign_sessions import Converter, plan_history
from daedalus.host.session_import import SessionImporter
from daedalus.host.session_runner import SessionManager
from daedalus.security.redact import Redactor
from daedalus.stores.database import Database
from tests.unit.test_project_unification import HEADERS, _client
from tests.unit.test_session_import import finished, host_of, import_config, manager  # noqa: F401 — the fixture

SAMPLES = Path(__file__).resolve().parents[2] / "ptyd" / "internal" / "sidechan" / "sessions" / "testdata" / "wire"
NATIVE = {"Exec", "Read", "Write", "Edit", "MultiEdit", "Find", "Search", "WebFetch", "WebSearch"}
CWD = "/home/someone/proj"
CLAUDE_ID = "c0ffee00-0000-4000-8000-000000000001"


def sample(name: str) -> dict[str, Any]:
    return json.loads((SAMPLES / f"{name}.json").read_text())


def history_problems(history: list[Message]) -> list[str]:
    """What a provider would refuse in a working history, as sentences; empty when it is valid.

    Two messages of one role in a row are not a problem: Daedalus writes a summary followed by the
    operator's message itself, and the providers join such turns."""
    problems: list[str] = []
    for index, message in enumerate(history):
        blocks = message.content_blocks
        if any(isinstance(b, ThinkingBlock) for b in blocks):
            problems.append(f"message {index} carries thinking")
        calls = [b for b in blocks if isinstance(b, ToolUseBlock)]
        for block in calls:
            if block.name not in NATIVE:
                problems.append(f"message {index} calls {block.name}, which the model does not have")
        if calls:
            answered: set[str] = set()
            for later in history[index + 1:]:
                results = [b for b in later.content_blocks if isinstance(b, ToolResultBlock)]
                if later.role is not MessageRole.tool or not results:
                    break
                answered.update(b.tool_call_id for b in results)
            missing = {b.tool_call_id for b in calls} - answered
            if missing:
                problems.append(f"message {index}: calls {sorted(missing)} are not answered right after it")
        if message.role is MessageRole.tool:
            before = history[index - 1] if index else None
            for block in (b for b in blocks if isinstance(b, ToolResultBlock)):
                previous = index - 1
                while previous >= 0 and history[previous].role is MessageRole.tool:
                    previous -= 1
                owner = history[previous] if previous >= 0 else before
                if owner is None or block.tool_call_id not in {b.tool_call_id for b in owner.content_blocks if isinstance(b, ToolUseBlock)}:
                    problems.append(f"message {index} answers {block.tool_call_id}, which the message before did not call")
    return problems


def test_the_scan_sample_has_every_field_the_host_reads() -> None:
    for name in ("claude", "codex"):
        scan = sample(name)["scan"]
        assert set(scan) >= {"path", "here", "children", "folders", "truncated", "cursor"}
        header = scan["here"][0]
        assert set(header) == {"v", "harness", "id", "cwd", "title", "started_at", "updated_at", "messages", "bytes", "branch", "model", "flags", "source"}
        assert header["v"] == 1 and header["cwd"] == CWD and header["harness"] == name
        assert set(header["flags"]) == {"compacted", "sidechains", "live"}, "imported_as is the host's to add"
        assert header["flags"]["compacted"] == 1


def test_a_claude_code_session_converts_as_the_daemon_hands_it_over() -> None:
    read = sample("claude")["read"]
    assert read["done"] is True and read["total"] == len(read["turns"]) == read["next"]
    token_masked = read["masked"]
    assert token_masked == 1, "the daemon masked the token in the tool output before it crossed the socket"
    conversion = Converter("claude", redactor=Redactor()).convert(read["turns"])
    counts = conversion.counts
    assert counts["native_calls"] == 1, "Bash became Exec"
    assert counts["text_calls"] == 4, "TodoWrite, Task, the MCP tool and the unanswered Read became text"
    assert counts["sidechains"] == 1 and counts["compactions"] == 1 and counts["images"] == 1 and counts["thinking"] == 1
    assert counts["orphan_results"] == 0

    transcript = [e.transcript for e in conversion.entries]
    assert any(isinstance(b, ThinkingBlock) for m in transcript for b in m.content_blocks), "the transcript keeps the thinking"
    assert "ghp_" not in json.dumps([m.model_dump(mode="json") for m in transcript])
    exec_call = next(b for m in transcript for b in m.content_blocks if isinstance(b, ToolUseBlock))
    assert exec_call.name == "Exec" and json.loads(exec_call.arguments_json) == {"command": "pytest -q tests/test_door.py", "timeout_seconds": 120}
    renamed = next(m for m in transcript if any(isinstance(b, ToolUseBlock) for b in m.content_blocks))
    assert renamed.metadata["daedalus.imported"]["tools"] == {"toolu_bash": "Bash"}
    side = [e for e in conversion.entries if e.sidechain]
    assert side and all(e.history is None for e in side), "a sub-agent's turns are in the transcript only"

    summary = next(e for e in conversion.entries if e.summary)
    compaction = summary.transcript.metadata["daedalus.compaction"]
    assert compaction["reason"] == "import" and compaction["auto"] is True
    assert compaction["messages"] == sum(1 for e in conversion.entries[:conversion.entries.index(summary)] if not e.sidechain)

    plan = plan_history(conversion, mode="full", window=200_000)
    history = [conversion.entries[i].history for i in plan.kept]
    assert all(m is not None for m in history)
    assert history_problems(history) == []  # type: ignore[arg-type]
    assert history[0] is summary.history, "a full import starts from the program's own last summary"
    dangling = history[-1]
    assert dangling.role is MessageRole.assistant and "[Claude Code · Read]" in dangling.text and "interrupted" in dangling.text


def test_a_codex_session_converts_as_the_daemon_hands_it_over() -> None:
    read = sample("codex")["read"]
    conversion = Converter("codex", redactor=Redactor()).convert(read["turns"])
    counts = conversion.counts
    assert counts["native_calls"] == 2, "the shell call became Exec and the one-file patch an Edit"
    assert counts["thinking"] == 1 and counts["compactions"] == 1 and counts["meta"] >= 1
    calls = [b for e in conversion.entries for b in e.transcript.content_blocks if isinstance(b, ToolUseBlock)]
    assert [c.name for c in calls] == ["Exec", "Edit"]
    assert json.loads(calls[0].arguments_json) == {"command": "grep -rn topic .", "cwd": CWD}
    assert json.loads(calls[1].arguments_json) == {"path": "a.py", "old_string": "topic", "new_string": "subject"}
    hidden = next(b for e in conversion.entries for b in e.transcript.content_blocks if isinstance(b, ThinkingBlock))
    assert hidden.text == "[reasoning hidden by Codex]"
    for mode in ("full", "tail"):
        plan = plan_history(conversion, mode=mode, window=200_000)
        history = [conversion.entries[i].history for i in plan.kept]
        assert history_problems(history) == []  # type: ignore[arg-type]
    texts = [b.text for e in conversion.entries for b in e.transcript.content_blocks if isinstance(b, TextBlock)]
    assert not any("<environment_context>" in t for t in texts), "the program's own wrapping is not the operator's words"


async def test_the_samples_import_through_the_routes_with_the_fields_the_app_reads(settings: Settings, db: Database, manager: SessionManager) -> None:  # noqa: F811
    host = host_of(manager)
    claude = sample("claude")
    host.folders[CWD] = []
    host.folders["~"] = []
    host.sessions[CLAUDE_ID] = {"header": claude["read"]["header"], "turns": claude["read"]["turns"], "raw": '{"type": "user"}\n'}
    host.PAGE = 4
    pages: list[dict[str, Any]] = []
    plain_read = host.sessions_read

    async def read_with_total(harness: str, session: str, **kwargs: Any) -> dict[str, Any]:
        page = await plain_read(harness, session, **kwargs)
        if not kwargs.get("raw"):
            page["total"] = len(claude["read"]["turns"])
            pages.append(page)
        return page

    async def scan(harness: str, path: str = "", **kwargs: Any) -> dict[str, Any]:
        reply = copy.deepcopy(claude["scan"])
        if not path:
            reply.update(path="", here=[], children=[], folders=[{"path": CWD, "sessions": 1, "latest": reply["here"][0]["updated_at"]}])
        return reply

    host.sessions_read = read_with_total  # type: ignore[method-assign]
    host.sessions_scan = scan  # type: ignore[method-assign]
    async with await _client(settings, import_config(), db, manager) as client:
        listed = (await client.get("/api/imports/scan", headers=HEADERS, params={"harness": "claude", "path": CWD})).json()
        row = listed["here"][0]
        assert row["imported_as"] is None and row["flags"]["imported_as"] is None and row["project"] is None
        assert listed["browse"] == "ok" and listed["crumbs"] and listed["home"]
        places = (await client.get("/api/imports/scan", headers=HEADERS, params={"harness": "claude", "path": ""})).json()
        assert places["home"], "the places and a search say where home is, so the app can write ~/…"
        assert places["folders"][0]["path"] == CWD and "project" in places["folders"][0]

        preview = (await client.get("/api/imports/preview", headers=HEADERS, params={"harness": "claude", "id": CLAUDE_ID})).json()
        assert preview["destination"]["kind"] == "new_chat" and preview["suggested_mode"] == "full"
        assert preview["header"]["id"] == CLAUDE_ID and preview["complete"] is True
        assert set(preview["counts"]) >= {"turns", "user", "assistant", "tool_calls", "compactions", "sidechains", "images"}

        job = await finished(client, (await client.post("/api/imports", headers=HEADERS, json={"harness": "claude", "id": CLAUDE_ID, "mode": "full"})).json()["job_id"])
        assert job["state"] == "done", job
        assert job["counts"]["turns_total"] == len(claude["read"]["turns"]), "the read is measured in turns, not in visible messages"
        assert job["stages"] == ["read", "parse", "mask", "write", "index", "summarise", "open"]
        sid = job["session_id"]
        history = await manager.sessions.list_messages(sid, TENANT, limit=1000)
        assert history_problems(list(history)) == []

        detail = (await client.get(f"/api/sessions/{sid}", headers=HEADERS)).json()
        assert detail["imported"]["harness_name"] == "Claude Code" and detail["imported"]["counts"]["sidechains"] == 1
        summary = next(m for m in detail["messages"] if m["summary"])
        assert summary["compaction"]["reason"] == "import" and summary["compaction"]["messages"] > 0 and summary["imported"]["harness"] == "claude"
        renamed = next(m for m in detail["messages"] if m["tool_calls"])
        assert renamed["tool_calls"][0]["name"] == "Exec" and renamed["imported"]["tools"] == {"toolu_bash": "Bash"}
        rows = (await client.get("/api/sessions", headers=HEADERS)).json()["sessions"]
        assert next(r for r in rows if r["id"] == sid)["imported_from"] == "claude"
    assert pages and all(p["total"] == len(claude["read"]["turns"]) for p in pages)
    assert isinstance(SessionImporter, type)
