"""The who-edits-what board: leases per file and agent, the one-line note on an overlap, its dedup,
the purge when a holder stops, and the paths each kind of agent reports its edits through."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.edit_board import EditBoards
from daedalus.harness.claude import ClaudeCodeAdapter
from daedalus.harness.codex import CodexAdapter
from daedalus.harness.codex import _Launch as CodexLaunch
from daedalus.harness.contract import EventKind, HookPost, LaunchSpec, StaffEvent
from daedalus.harness.edits import edited_paths
from daedalus.harness.grok import GrokAdapter
from daedalus.harness.grok import _Launch as GrokLaunch
from daedalus.harness.opencode import OpenCodeAdapter
from daedalus.harness.opencode import _Launch as OpenCodeLaunch
from daedalus.harness.runtime import CliStaffRuntime
from daedalus.host.events import AppEvent
from daedalus.host.services import SessionServices, locator
from daedalus.stores.database import Database
from daedalus.stores.edit_board import EditBoard, Editor, canonical_path, repo_root


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


ADA = Editor.member("st-ada", "Ada", "p1")
BO = Editor.member("st-bo", "Bo", "p1")
OPERATOR = Editor.session("s-op", "Fix the login", None)


async def test_an_overlap_is_noted_once_per_roster_and_again_when_it_changes(db: Database, tmp_path: Path) -> None:
    clock = Clock()
    board = EditBoard(db, lease_seconds=60, reshow_seconds=45 * 60, clock=clock)
    a = str(tmp_path / "a.py")
    assert await board.record(ADA, [a]) == ""
    clock.now += 10
    note = await board.record(OPERATOR, [a])
    # Who, when, and how to reach them; one line; and it says the edit went through.
    assert "member Ada" in note and "a.py" in note and "10 s ago" in note and "orchestrator" in note
    assert "went through" in note and "\n" not in note
    # The same roster again is not news, however often the file is edited and the lease extended.
    clock.now += 20
    assert await board.record(OPERATOR, [a]) == ""
    assert await board.record(ADA, [a]) != ""  # Ada had not been told about the operator's session yet
    note = await board.record(ADA, [a])
    assert note == ""
    # A third holder changes the roster: the note comes back, naming both.
    clock.now += 5
    note = await board.record(BO, [a])
    assert "member Ada" in note and "session \"Fix the login\"" in note and "the operator" in note
    assert "member Bo" in await board.record(OPERATOR, [a])
    # After the staleness window, which a compaction may have folded the first note into, it is shown again.
    long = EditBoard(db, lease_seconds=4 * 3600, reshow_seconds=45 * 60, clock=clock)
    x, y, c = Editor.session("s-x", "X", None), Editor.session("s-y", "Y", None), str(tmp_path / "c.py")
    await long.record(x, [c])
    assert await long.record(y, [c]) != ""
    clock.now += 30 * 60
    assert await long.record(y, [c]) == ""
    clock.now += 16 * 60
    assert "session \"X\"" in await long.record(y, [c])


async def test_a_lease_lapses_a_purge_drops_it_at_once_and_both_survive_a_restart(settings: Settings, tmp_path: Path) -> None:
    clock = Clock()
    path = str(tmp_path / "x.py")
    db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await db.open()
    board = EditBoard(db, lease_seconds=60, clock=clock)
    await board.record(ADA, [path])
    clock.now += 1
    assert await board.record(BO, [path]) != ""
    await db.close()
    # A restart forgets nothing: the lease is still Ada's, and Bo is not told the same thing again.
    db = Database(settings.db_path, workspaces_dir=settings.workspaces_dir)
    await db.open()
    try:
        board = EditBoard(db, lease_seconds=60, clock=clock)
        assert [h.name for h in await board.holders()] == ["Bo", "Ada"]
        assert await board.record(BO, [path]) == ""
        assert await board.purge(ADA.key) == 1
        assert [h.name for h in await board.holders()] == ["Bo"]
        assert await board.record(ADA, [path]) != ""  # back again, and Bo's roster changed
        clock.now += 62
        assert await board.holders() == []
        assert await board.prune() == 2
    finally:
        await db.close()


async def test_a_projects_board_holds_its_members_and_anyone_in_its_folders(db: Database, tmp_path: Path) -> None:
    board = EditBoard(db, clock=Clock())
    inside, outside = str(tmp_path / "repo" / "src" / "a.py"), str(tmp_path / "elsewhere" / "b.py")
    await board.record(ADA, [outside])
    await board.record(OPERATOR, [inside])
    await board.record(Editor.member("st-cy", "Cy", "p2"), [str(tmp_path / "other.py")])
    shown = await board.holders(project_id="p1", folders=[str(tmp_path / "repo")])
    assert sorted(h.name for h in shown) == ["Ada", "Fix the login"]


def test_paths_are_canonical_and_a_checkout_is_found_by_its_git(tmp_path: Path) -> None:
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "src").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "repo")
    assert canonical_path("src/../src/a.py", str(tmp_path / "link")) == str(tmp_path / "repo" / "src" / "a.py")
    assert repo_root(str(tmp_path / "repo" / "src" / "a.py")) == str(tmp_path / "repo")
    assert repo_root(str(tmp_path / "loose.py"), "fallback") == "fallback"


def test_only_a_file_tool_with_a_path_counts_as_an_edit() -> None:
    assert edited_paths("Edit", {"file_path": "/w/a.py", "old_string": "x"}) == ["/w/a.py"]
    assert edited_paths("edit", {"filePath": "src/a.ts"}) == ["src/a.ts"]
    assert edited_paths("NotebookEdit", {"notebook_path": "/w/n.ipynb"}) == ["/w/n.ipynb"]
    assert edited_paths("Read", {"file_path": "/w/a.py"}) == []
    assert edited_paths("Bash", {"command": "echo > /w/a.py"}) == []
    assert edited_paths("Write", {}) == []


# -- how each agent reports its edits ---------------------------------------------------------------


def test_claude_holds_only_its_file_tools_for_the_note_and_answers_into_the_tools_result() -> None:
    spec = LaunchSpec(harness="claude", env="container", cwd="/work", launch_id="l1", first_prompt="go")
    settings = json.loads(ClaudeCodeAdapter().launch_plan(spec).files["settings.json"])
    [plain, held] = settings["hooks"]["PostToolUse"]
    assert "matcher" not in plain and "--wait-ms" not in plain["hooks"][0]["command"]
    assert held["matcher"] == "Edit|Write|MultiEdit|NotebookEdit" and "--wait-ms 3000" in held["hooks"][0]["command"]
    adapter = ClaudeCodeAdapter()
    body = {"tool_name": "Edit", "tool_input": {"file_path": "/work/a.py"}, "tool_use_id": "toolu_1"}
    state = adapter._state(SimpleNamespace(id="t1"))  # type: ignore[arg-type]
    state.turn_open = True
    # The unheld post reports the tool finishing, as before; the held one is for the board only.
    [finished] = adapter._map(SimpleNamespace(id="t1"), state, HookPost("PostToolUse", body))  # type: ignore[arg-type]
    assert finished.kind is EventKind.TOOL_FINISHED and "edited" not in finished.payload
    [edited] = adapter._map(SimpleNamespace(id="t1"), state, HookPost("PostToolUse", body, reply_id="r1"))  # type: ignore[arg-type]
    assert edited.kind is EventKind.FILES_EDITED and edited.payload == {"edited": ["/work/a.py"], "reply_id": "r1"}
    assert adapter.edit_reply("note") == {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "note"}}


def test_codex_opencode_and_grok_name_the_files_a_finished_tool_wrote() -> None:
    codex, codex_state = CodexAdapter(), CodexLaunch()
    codex_state.turn_id = "turn"
    codex._item(codex_state, False, {"type": "fileChange", "id": "i1", "status": "completed", "changes": [{"path": "/w/a.py", "kind": "update"}, {"path": "/w/b.py"}]})
    assert codex_state.queue.get_nowait().payload["edited"] == ["/w/a.py", "/w/b.py"]
    codex._item(codex_state, False, {"type": "fileChange", "id": "i2", "status": "failed", "changes": [{"path": "/w/a.py"}]})
    assert "edited" not in codex_state.queue.get_nowait().payload
    opencode, opencode_state = OpenCodeAdapter(), OpenCodeLaunch()
    opencode._part(opencode_state, {"type": "tool", "callID": "c1", "tool": "write", "state": {"status": "completed", "input": {"filePath": "/w/c.ts"}}})
    assert opencode_state.queue.get_nowait().payload["edited"] == ["/w/c.ts"]
    grok, grok_state = GrokAdapter(), GrokLaunch()
    events = grok._map(grok_state, HookPost("PostToolUse", {"toolName": "edit_file", "toolInput": {"path": "/w/d.go"}, "toolUseId": "g1"}))
    assert events[-1].kind is EventKind.TOOL_FINISHED and events[-1].payload["edited"] == ["/w/d.go"]


async def test_the_runtime_records_a_cli_edit_and_always_answers_the_held_hook() -> None:
    calls: list[tuple[str, list[str], str]] = []
    replies: list[tuple[str, Any]] = []

    async def board(staff_session_id: str, paths: Any, cwd: str) -> str:
        calls.append((staff_session_id, list(paths), cwd))
        return "[edit board] note" if len(calls) == 1 else ""

    async def reply(reply_id: str, body: Any) -> bool:
        replies.append((reply_id, body))
        return True

    runtime = SimpleNamespace(edit_board=board, adapter=ClaudeCodeAdapter())
    session = SimpleNamespace(staff_session_id="ss1", cwd="/work", term=SimpleNamespace(reply=reply))
    held = StaffEvent(EventKind.FILES_EDITED, "now", {"edited": ["a.py"], "reply_id": "r1"})
    await CliStaffRuntime._edited(runtime, session, held)  # type: ignore[arg-type]
    await CliStaffRuntime._edited(runtime, session, StaffEvent(EventKind.FILES_EDITED, "now", {"edited": ["a.py"], "reply_id": "r2"}))  # type: ignore[arg-type]
    assert calls == [("ss1", ["a.py"], "/work"), ("ss1", ["a.py"], "/work")]
    assert replies == [("r1", {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "[edit board] note"}}), ("r2", None)]

    async def broken(*_: Any) -> str:
        raise RuntimeError("the database is gone")

    runtime.edit_board = broken
    await CliStaffRuntime._edited(runtime, session, StaffEvent(EventKind.FILES_EDITED, "now", {"edited": ["a.py"], "reply_id": "r3"}))  # type: ignore[arg-type]
    assert replies[-1] == ("r3", None)  # a failing board still lets the CLI go at once


# -- the subsystem: who an agent is, the native tools, the purge ------------------------------------


@pytest.fixture
async def boards(db: Database) -> EditBoards:
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('p1','Bakery','2026-01-01','{}')")
    await db.execute("INSERT INTO projects(id,name,created_at,settings) VALUES ('chats','Chats','2026-01-01','{}')")
    await db.execute("INSERT INTO staff(id,project_id,name,harness,created_by,created_at) VALUES ('st-ada','p1','Ada','daedalus','operator','2026-01-01')")
    await db.execute("INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,project_id) VALUES ('s-ada','t','Ada · Menu','2026-01-01','2026-01-01','p1')")
    await db.execute("INSERT INTO sessions(id,tenant_id,title,created_at,last_message_at,project_id) VALUES ('s-op','t','Fix the login','2026-01-01','2026-01-01','chats')")
    await db.execute("INSERT INTO staff_sessions(id,staff_id,kind,session_id,status_at,started_at) VALUES ('ss-ada','st-ada','daedalus','s-ada','2026-01-01','2026-01-01')")
    manager = SimpleNamespace(db=db, config=RuntimeConfig(), service_hooks={})
    return EditBoards(SimpleNamespace(manager=manager))  # type: ignore[arg-type]


async def test_the_native_tools_append_the_note_and_a_stopped_holder_is_purged(boards: EditBoards, tmp_path: Path) -> None:
    from daedalus.tools.files import edit_file, write_file

    for session_id in ("s-ada", "s-op"):
        locator.register(SessionServices(session_id=session_id, workspace_dir=tmp_path,
                                         extra={"manager": SimpleNamespace(service_hooks={"edit_board": boards.native_edit})}))
    try:
        ada = ToolContext(tenant_id="t", run_id="r", session_id="s-ada", metadata={"tool_call_id": "c1"})
        operator = ToolContext(tenant_id="t", run_id="r", session_id="s-op", metadata={"tool_call_id": "c2"})
        first = await write_file().invoke(ada, {"path": "menu.txt", "content": "bread\n"})
        assert not first.is_error and "[edit board]" not in first.content
        second = await edit_file().invoke(operator, {"path": "menu.txt", "old_string": "bread", "new_string": "rye"})
        assert not second.is_error and "[edit board]" in second.content and "member Ada" in second.content
        # The member is the member, not its session; the operator's session is named by its title.
        holders = await boards.board.holders()
        assert {(h.key, h.name) for h in holders} == {("staff:st-ada", "Ada"), ("session:s-op", "Fix the login")}
        # A member whose turn ended loses its leases; a member still at work keeps them.
        await boards.on_status(AppEvent(0, "now", "staff.status", {"status": "working"}, staff_id="st-ada"))
        assert len(await boards.board.holders()) == 2
        await boards.on_status(AppEvent(0, "now", "staff.status", {"status": "turn_done_unseen"}, staff_id="st-ada"))
        assert [h.key for h in await boards.board.holders()] == ["session:s-op"]
        # A run waiting on the operator still holds its files; one that finished does not.
        await boards.on_run_finished("s-op", "run", "awaiting")
        assert len(await boards.board.holders()) == 1
        await boards.on_run_finished("s-op", "run", "completed")
        assert await boards.board.holders() == []
    finally:
        locator.unregister("s-ada")
        locator.unregister("s-op")


async def test_a_cli_member_is_named_and_its_paths_taken_against_its_folder(boards: EditBoards, tmp_path: Path) -> None:
    await boards.board.record(OPERATOR, [str(tmp_path / "src" / "a.py")])
    note = await boards.cli_edit("ss-ada", ["src/a.py"], str(tmp_path))
    assert "session \"Fix the login\"" in note and "src/a.py" in note
    assert await boards.cli_edit("no-such-session", ["src/a.py"], str(tmp_path)) == ""


async def test_the_orchestrator_sees_who_edits_what_and_an_unchanged_board_reads_the_same(boards: EditBoards, tmp_path: Path) -> None:
    folder = SimpleNamespace(path=tmp_path)
    project = SimpleNamespace(id="p1", folders=[folder])
    assert await boards.state_section(project) == ([], "")  # type: ignore[arg-type]
    await boards.cli_edit("ss-ada", ["src/a.py", "src/b.py"], str(tmp_path))
    await boards.board.record(OPERATOR, [str(tmp_path / "README.md")])
    lines, _ = await boards.state_section(project)  # type: ignore[arg-type]
    assert lines[0].startswith("Editing now")
    assert any(line.startswith("  - Ada → ") and "a.py" in line and "b.py" in line and "UTC" in line for line in lines)
    assert any("session \"Fix the login\" → " in line for line in lines)
    assert await boards.state_section(project) == (lines, "")  # type: ignore[arg-type]
    view = await boards.project_view(project)  # type: ignore[arg-type]
    assert {v["name"] for v in view} == {"Ada", "Fix the login"} and all(v["files"] and "age_s" in v for v in view)
