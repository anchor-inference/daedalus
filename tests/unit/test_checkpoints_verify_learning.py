"""Workspace checkpoints, revert and fork, verification receipts, learning records."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from protocore.contracts.tools import ToolContext
from protocore.contracts.types import COMPACTION_SUMMARY_METADATA_KEY, Message, MessageRole, TextBlock

from daedalus.config import RuntimeConfig, Settings
from daedalus.extensions.learning import Learning
from daedalus.host.checkpoints import Checkpoints
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from daedalus.tools.verify import verify
from tests.support.notifications import RecordingNotifications


async def test_checkpoints_snapshot_and_restore(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("one")
    cp = Checkpoints(ws)
    first = await cp.snapshot("first")
    (ws / "a.txt").write_text("two")
    (ws / "b.txt").write_text("new")
    second = await cp.snapshot("second")
    assert first != second and await cp.head() == second
    await cp.restore(first)
    assert (ws / "a.txt").read_text() == "one" and not (ws / "b.txt").exists()
    assert (ws / ".checkpoints").is_dir()


async def _manager(settings: Settings, db: Database) -> SessionManager:
    manager = SessionManager(settings, RuntimeConfig(), db=db)
    await manager.start()
    return manager


async def _seed(manager: SessionManager, state: Any, turns: list[tuple[str, str]]) -> list[int]:
    """Write user/assistant pairs into the transcript and the working history; returns the user seqs."""
    history: list[Message] = []
    for ask, answer in turns:
        history.append(Message(role=MessageRole.user, content_blocks=[TextBlock(text=ask)], metadata={"daedalus.origin": "operator"}))
        history.append(Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=answer)]))
    await manager.sessions.append_transcript(state.session.id, history)
    await manager.sessions.replace_messages(state.session.id, "daedalus", history)
    rows = await manager.sessions.list_transcript(state.session.id)
    return [int(m.metadata["daedalus.seq"]) for m in rows if m.role is MessageRole.user]


async def test_revert_cuts_history_and_restores_files(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    state = await manager.create_session("undo")
    (state.workspace / "notes.md").write_text("draft 1")
    seqs = await _seed(manager, state, [("first ask", "first answer")])
    await manager.checkpoint(state, kind="before", seq=None)
    # the second turn: snapshot before it, then the agent changes the workspace
    more = await _seed(manager, state, [("first ask", "first answer"), ("second ask", "second answer")])
    second_seq = more[-1]
    await db.execute("INSERT INTO checkpoints(session_id, seq, run_id, kind, sha, at) VALUES (?, ?, NULL, 'before', ?, ?)", (state.session.id, second_seq, await Checkpoints(state.workspace).snapshot("before second"), "now"))
    (state.workspace / "notes.md").write_text("draft 2 (after the second turn)")
    checkpoint_head = await Checkpoints(state.workspace).head()
    result = await manager.revert(state.session.id, second_seq)
    assert await Checkpoints(state.workspace).head() == checkpoint_head
    assert result["dropped"] == 2 and result["kept"] == 2 and result["workspace_restored"] is True
    assert (state.workspace / "notes.md").read_text() == "draft 1"
    working = await manager.sessions.list_messages(state.session.id, "daedalus", limit=100)
    assert [m.content_blocks[0].text for m in working] == ["first ask", "first answer"]  # type: ignore[attr-defined]
    transcript = await manager.sessions.list_transcript(state.session.id)
    assert all(int(m.metadata["daedalus.seq"]) < second_seq for m in transcript)
    assert not any(m.metadata.get("daedalus.origin") == "revert" for m in transcript)
    with pytest.raises(ValueError, match="operator turn"):
        await manager.revert(state.session.id, seqs[0] + 1)  # an assistant row
    await manager.close()


async def test_fork_copies_history_before_the_turn_and_the_workspace(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    source = await manager.create_session("origin")
    (source.workspace / "data.txt").write_text("v1")
    private = source.workspace / ".agents" / "child"
    private.mkdir(parents=True)
    (private / "private.txt").write_text("private")
    seqs = await _seed(manager, source, [("ask one", "answer one"), ("ask two", "answer two")])
    target = await manager.create_session("fork")
    result = await manager.fork_into(source.session.id, seqs[1], target)
    assert result["messages"] == 2 and result["workspace_copied"] is True
    assert (target.workspace / "data.txt").read_text() == "v1"
    assert not (target.workspace / ".agents").exists()
    working = await manager.sessions.list_messages(target.session.id, "daedalus", limit=100)
    assert [m.content_blocks[0].text for m in working] == ["ask one", "answer one"]  # type: ignore[attr-defined]
    refreshed = await manager.sessions.get(target.session.id, "daedalus")
    assert refreshed.metadata["forked_from"]["seq"] == seqs[1]
    await manager.close()


async def test_fork_during_a_run_copies_the_settled_turns_and_leaves_the_run_alone(settings: Settings, db: Database) -> None:
    """A fork pressed while the source was working was refused, and the refusal left an empty
    session behind. It now forks from the last settled point and never waits for the run."""
    manager = await _manager(settings, db)
    source = await manager.create_session("busy")
    seqs = await _seed(manager, source, [("ask one", "answer one"), ("ask two", "answer two")])
    prompt = Message(role=MessageRole.user, content_blocks=[TextBlock(text="ask three, in flight")], metadata={"daedalus.origin": "operator", "daedalus.run_id": "run-live"})
    await manager.sessions.append_transcript(source.session.id, [prompt])
    release = asyncio.Event()
    source.task = asyncio.create_task(release.wait())
    source.run_id = "run-live"
    try:
        # Past the end of the transcript: clamped to the run's prompt, which is not copied.
        result = await asyncio.wait_for(manager.fork(source.session.id, 10_000), timeout=10)
        working = await manager.sessions.list_messages(result["id"], "daedalus", limit=100)
        assert [m.content_blocks[0].text for m in working] == ["ask one", "answer one", "ask two", "answer two"]  # type: ignore[attr-defined]
        earlier = await asyncio.wait_for(manager.fork(source.session.id, seqs[1]), timeout=10)
        working = await manager.sessions.list_messages(earlier["id"], "daedalus", limit=100)
        assert [m.content_blocks[0].text for m in working] == ["ask one", "answer one"]  # type: ignore[attr-defined]
        assert source.running and source.run_id == "run-live"
    finally:
        release.set()
        await source.task
        await manager.close()


async def test_a_failed_fork_leaves_no_session_behind(settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = await _manager(settings, db)
    source = await manager.create_session("origin")
    seqs = await _seed(manager, source, [("ask one", "answer one")])
    before = [r["id"] for r in await db.fetchall("SELECT id FROM sessions ORDER BY id")]

    async def refuse(source_id: str, seq: int, target: Any) -> dict[str, Any]:
        await manager.sessions.append_transcript(target.session.id, [Message(role=MessageRole.user, content_blocks=[TextBlock(text="half copied")])])
        raise RuntimeError("the copy failed")

    monkeypatch.setattr(manager, "fork_into", refuse)
    with pytest.raises(RuntimeError, match="the copy failed"):
        await manager.fork(source.session.id, seqs[0])
    assert [r["id"] for r in await db.fetchall("SELECT id FROM sessions ORDER BY id")] == before
    assert await db.fetchall("SELECT seq FROM transcript WHERE session_id NOT IN (SELECT id FROM sessions)") == []
    assert set(manager._states) == set(before)
    await manager.close()


async def test_verify_records_a_receipt(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    state = await manager.create_session("verify")
    state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
    good = await verify().invoke(ctx, {"criterion": "echo works", "command": "echo hello"})
    bad = await verify().invoke(ctx, {"criterion": "false fails", "command": "false"})
    assert not good.is_error and good.metadata["passed"] and "✅ verified" in good.content and "hello" in good.content
    assert bad.is_error and not bad.metadata["passed"]
    rows = await db.fetchall("SELECT criterion, exit_code, passed FROM verifications WHERE session_id = ? ORDER BY id", (state.session.id,))
    assert [(r["criterion"], r["exit_code"], r["passed"]) for r in rows] == [("echo works", 0, 1), ("false fails", 1, 0)]
    await manager.close()


async def test_learning_record_and_digest(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    app.notifications = RecordingNotifications()
    learning = Learning(app)  # type: ignore[arg-type]
    state = await manager.create_session("learn")
    await db.execute("INSERT INTO runs(id, tenant_id, session_id, status, created_at, updated_at) VALUES ('run-a', 'daedalus', ?, 'completed', '2026-09-06T10:00:00+00:00', '2026-09-06T10:00:00+00:00')", (state.session.id,))
    engine = SimpleNamespace(history=[
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="deploy the thing")], metadata={"daedalus.origin": "operator"}),
        Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="Done.\n\n⟦ deploy | status: completed; next: none | anchors: thing ⟧")]),
    ])
    state.engine = engine  # type: ignore[assignment]
    for _ in range(3):
        await learning.on_run_finished(state.session.id, "run-a", "completed")
    rows = await db.fetchall("SELECT ask, outcome, headline FROM learning_records")
    assert len(rows) == 3 and rows[0]["ask"] == "deploy the thing" and rows[0]["headline"].startswith("⟦ deploy")
    report = await learning.report(7)
    assert report["runs"] == 3 and report["outcomes"] == {"completed": 3}
    assert any("deploy the thing" in c for c in report["candidates"])
    assert await learning.maybe_digest() is True
    assert await learning.maybe_digest() is False  # once a week
    [draft] = app.notifications.drafts
    assert draft.kind == "learning_digest" and "improvement candidates" in draft.body
    await manager.close()


async def test_checkpoints_leave_nested_repositories_alone_and_stay_undoable(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("one")
    subprocess.run(["git", "init", "-q", str(ws / "clone")], check=True)
    (ws / "clone" / "n.txt").write_text("n1")
    cp = Checkpoints(ws)
    first = await cp.snapshot("first")
    (ws / "a.txt").write_text("two")
    (ws / "clone" / "n.txt").write_text("n2")
    second = await cp.snapshot("second")
    assert await cp.restore(first) == ["clone"]
    assert (ws / "a.txt").read_text() == "one" and (ws / "clone" / "n.txt").read_text() == "n2"
    # the branch moved forward: the later snapshot is still reachable, so the restore can itself be undone
    assert await cp.restore(second) == ["clone"] and (ws / "a.txt").read_text() == "two"
    assert subprocess.run(["git", "--git-dir", str(cp.git_dir), "config", "core.worktree"], capture_output=True, text=True).stdout == ""


async def test_fork_skips_reverted_turns_and_repoints_summaries(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    source = await manager.create_session("origin")
    seqs = await _seed(manager, source, [("ask one", "answer one"), ("ask two", "answer two")])
    await db.execute("INSERT INTO checkpoints(session_id, seq, run_id, kind, sha, at) VALUES (?, ?, NULL, 'before', ?, 'now')", (source.session.id, seqs[1], await Checkpoints(source.workspace).snapshot("s")))
    await manager.revert(source.session.id, seqs[1])
    await _seed(manager, source, [("ask three", "answer three")])
    # a compaction summary standing for the first turn, as the persist path would record it
    summary = Message(
        role=MessageRole.user,
        content_blocks=[TextBlock(text=f"<compacted-turn id=1>gist\n\n[archived turns seq {seqs[0]}–{seqs[0] + 1}: HistoryExpand({seqs[0]}, {seqs[0] + 1}) returns them verbatim]</compacted-turn>")],
        metadata={COMPACTION_SUMMARY_METADATA_KEY: True, "daedalus.archived": {"from_seq": seqs[0], "to_seq": seqs[0] + 1, "seqs": [seqs[0], seqs[0] + 1]}},
    )
    await manager.sessions.append_transcript(source.session.id, [summary])
    rows = await manager.sessions.list_transcript(source.session.id)
    fork_seq = int(rows[-1].metadata["daedalus.seq"]) + 1
    target = await manager.create_session("fork")
    result = await manager.fork_into(source.session.id, fork_seq, target)
    working = await manager.sessions.list_messages(target.session.id, "daedalus", limit=100)
    texts = [m.content_blocks[0].text for m in working]  # type: ignore[attr-defined]
    assert "ask two" not in texts and "ask three" in texts and result["messages"] == 3
    assert texts[-1].startswith("<compacted-turn") and "ask one" not in texts
    forked = await manager.sessions.list_transcript(target.session.id)
    archived = [m for m in forked if m.metadata.get("daedalus.archived")][0]
    own = {int(m.metadata["daedalus.seq"]) for m in forked}
    meta = archived.metadata["daedalus.archived"]
    assert set(meta["seqs"]) <= own and f"HistoryExpand({meta['from_seq']}, {meta['to_seq']})" in archived.content_blocks[0].text  # type: ignore[attr-defined]
    await manager.close()


async def test_verify_digest_hashes_raw_bytes(settings: Settings, db: Database) -> None:
    """The receipt digest must cover the bytes the process printed, not a decoded copy.

    Output containing invalid UTF-8 used to be decoded with "replace" first and
    re-encoded for the hash, so the digest described a lossy copy (U+FFFD
    sequences) instead of the real output.
    """
    import hashlib

    manager = await _manager(settings, db)
    state = await manager.create_session("verify-raw")
    state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
    raw = b"\xff\xfe raw"
    result = await verify().invoke(ctx, {"criterion": "raw bytes", "command": "printf '\\377\\376 raw'"})
    assert not result.is_error
    row = await db.fetchone("SELECT output_digest FROM verifications WHERE session_id = ?", (state.session.id,))
    assert row["output_digest"] == hashlib.sha256(raw).hexdigest()
    await manager.close()


async def test_verify_receipts_are_redacted(settings: Settings, db: Database) -> None:
    from daedalus.security import redact

    manager = await _manager(settings, db)
    state = await manager.create_session("verify")
    state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
    token = "ghp_" + "A1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6q7r8s9"
    await verify().invoke(ctx, {"criterion": "auth", "command": f"echo {token}"})
    row = await db.fetchone("SELECT command, output_head, sandboxed FROM verifications WHERE session_id = ?", (state.session.id,))
    assert token not in row["command"] and token not in row["output_head"] and redact.MASK in row["command"] and row["sandboxed"] == 0
    await manager.close()


async def test_learning_record_covers_only_the_run(settings: Settings, db: Database) -> None:
    from protocore.contracts.types import ToolResultBlock, ToolUseBlock

    manager = await _manager(settings, db)
    app = SimpleNamespace(settings=settings, config=RuntimeConfig(), db=db, manager=manager, front=None, extensions={})
    learning = Learning(app)  # type: ignore[arg-type]
    state = await manager.create_session("learn")
    await db.execute("INSERT INTO runs(id, tenant_id, session_id, status, created_at, updated_at) VALUES ('run-b', 'daedalus', ?, 'completed', '2026-09-06T10:00:00+00:00', '2026-09-06T10:00:00+00:00')", (state.session.id,))
    earlier = [
        Message(role=MessageRole.user, content_blocks=[TextBlock(text="old ask")], metadata={"daedalus.origin": "operator"}),
        Message(role=MessageRole.assistant, content_blocks=[ToolUseBlock(tool_call_id="c1", name="Exec", arguments_json="{}")]),
        Message(role=MessageRole.user, content_blocks=[ToolResultBlock(tool_call_id="c1", content="boom", is_error=True)]),
    ]
    this_run = [Message(role=MessageRole.user, content_blocks=[TextBlock(text="new ask")], metadata={"daedalus.origin": "schedule"}), Message(role=MessageRole.assistant, content_blocks=[TextBlock(text="ok")])]
    state.engine = SimpleNamespace(history=earlier + this_run)  # type: ignore[assignment]
    state.run_history_start = len(earlier)
    await learning.on_run_finished(state.session.id, "run-b", "completed")
    row = await db.fetchone("SELECT ask, tools, failures FROM learning_records WHERE run_id = 'run-b'")
    assert row["ask"] == "new ask" and row["tools"] == "{}" and row["failures"] == "[]"
    await manager.close()


async def test_verify_receipt_records_time_and_dependencies(settings: Settings, db: Database) -> None:
    """The receipt names when it was taken and the shared channels it rests on.

    A digest without a timestamp is 'verified for me, right now'; a receipt
    without its named dependencies hides the trust root the observation
    stands on. Both must be in the stored row and the receipt text.
    """
    manager = await _manager(settings, db)
    state = await manager.create_session("verify-deps")
    state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
    result = await verify().invoke(ctx, {"criterion": "echo works", "command": "echo hello", "dependencies": "container shell + provider API"})
    assert not result.is_error
    assert "· at " in result.content and "· deps: container shell + provider API" in result.content
    row = await db.fetchone("SELECT at, dependencies FROM verifications WHERE session_id = ?", (state.session.id,))
    assert row["dependencies"] == "container shell + provider API"
    assert row["at"]
    # without dependencies the field stays empty, the header stays clean
    await verify().invoke(ctx, {"criterion": "plain", "command": "true"})
    row2 = await db.fetchone("SELECT dependencies FROM verifications WHERE session_id = ? AND criterion = 'plain'", (state.session.id,))
    assert row2["dependencies"] == ""
    await manager.close()


async def test_verify_large_output_streams_digest_and_caps_head(settings: Settings, db: Database) -> None:
    """The digest must cover the whole output while the kept head is capped.

    Output used to be collected into a list of chunks before hashing, so a
    talkative grandchild that outlived the direct child could pump memory for
    the whole timeout window. The digest now streams over the raw bytes and
    only the first head_cap bytes are retained; the header says so.

    This is a contract test (digest over the whole output, head capped, header
    reports both) — it does not bound memory, since the cap is enforced in
    _pump by construction.
    """
    import hashlib

    from daedalus.tools.verify import OUTPUT_HEAD_CHARS

    manager = await _manager(settings, db)
    state = await manager.create_session("verify-big")
    state.services.extra["manager"] = manager  # type: ignore[union-attr]
    ctx = ToolContext(tenant_id="daedalus", run_id="r1", session_id=state.session.id)
    size = 300 * 1024
    result = await verify().invoke(ctx, {"criterion": "big output", "command": f"head -c {size} /dev/zero"})
    assert not result.is_error
    expected = hashlib.sha256(b"\x00" * size).hexdigest()
    row = await db.fetchone("SELECT output_digest, output_head FROM verifications WHERE session_id = ?", (state.session.id,))
    assert row["output_digest"] == expected
    assert len(row["output_head"]) <= OUTPUT_HEAD_CHARS
    head_cap = max(OUTPUT_HEAD_CHARS, state.services.max_tool_output_chars) * 4
    assert f"output {size} B, first {min(size, head_cap)} B kept" in result.content
    await manager.close()


async def test_one_walk_answers_both_the_size_cap_and_the_excludes(tmp_path: Path, monkeypatch) -> None:
    """The snapshot path walked the workspace three times before every turn; it walks it once."""
    import os as os_module

    from daedalus.host.checkpoints import Checkpoints, scan_workspace

    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.txt").write_text("x" * 100, encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "big.bin").write_text("y" * 5000, encoding="utf-8")
    clone = workspace / "vendor" / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "file.txt").write_text("z" * 4000, encoding="utf-8")

    scan = scan_workspace(workspace)
    assert scan.nested == ("vendor/clone",)
    assert scan.size == 100  # the derived directory and the nested repository are both out

    walks = 0
    real_walk = os_module.walk

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal walks
        walks += 1
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os_module, "walk", counting)
    sha = await Checkpoints(workspace).snapshot("before", scan=scan_workspace(workspace))
    assert sha and walks == 1
    excludes = (workspace / ".checkpoints" / "info" / "exclude").read_text(encoding="utf-8")
    assert "/vendor/clone/" in excludes and "node_modules/" in excludes


async def test_project_revert_preserves_private_agent_files(settings: Settings, db: Database) -> None:
    from daedalus.host.checkpoints import scan_workspace

    manager = await _manager(settings, db)
    try:
        parent = await manager.create_session("project")
        (parent.workspace / "notes.txt").write_text("before")
        seqs = await _seed(manager, parent, [("first", "answer"), ("second", "answer")])
        sha = await Checkpoints(parent.workspace).snapshot("before second")
        await db.execute(
            "INSERT INTO checkpoints(session_id, seq, kind, sha, at) VALUES (?, ?, 'before', ?, 'now')",
            (parent.session.id, seqs[1], sha),
        )
        child = await manager.create_session("private", project_id=parent.project.id, own_directory=True)
        (child.workspace / "work.txt").write_text("saved")
        await Checkpoints(child.workspace).snapshot("child")
        (child.workspace / "work.txt").write_text("unsaved changes")
        (child.workspace / "new.txt").write_text("untracked")
        assert scan_workspace(parent.workspace).size == len("before")
        later = await Checkpoints(parent.workspace).snapshot("with child")
        assert ".agents/" not in await Checkpoints(parent.workspace)._git("ls-tree", "-r", later)
        (parent.workspace / "notes.txt").write_text("after")
        result = await manager.revert(parent.session.id, seqs[1])
        assert result["workspace_restored"]
        assert (parent.workspace / "notes.txt").read_text() == "before"
        assert (child.workspace / "work.txt").read_text() == "unsaved changes"
        assert (child.workspace / "new.txt").read_text() == "untracked"
        assert await Checkpoints(child.workspace).head()
    finally:
        await manager.close()


async def test_restore_ignores_private_files_tracked_by_old_snapshots(tmp_path: Path) -> None:
    cp = Checkpoints(tmp_path)
    (tmp_path / "public.txt").write_text("before")
    private = tmp_path / ".agents" / "child" / "private.txt"
    private.parent.mkdir(parents=True)
    private.write_text("old private work")
    await cp.snapshot("initial")
    await cp._git("add", "-f", "--", str(private))
    await cp._git("commit", "-qm", "old snapshot")
    old = await cp.head()
    private.write_text("current private work")
    await cp.restore(old)
    assert private.read_text() == "current private work"
    assert ".agents/" not in await cp._git("ls-files")


async def test_orphan_sweep_keeps_ancestors_of_project_roots(settings: Settings, db: Database) -> None:
    manager = await _manager(settings, db)
    try:
        container = settings.workspaces_dir / "container"
        root = container / ".agents" / "child"
        root.mkdir(parents=True)
        (root / "work.txt").write_text("keep")
        await manager.projects.create("nested", [str(root)])
        assert container not in await manager.orphan_workspaces()
        await manager.sweep_orphan_workspaces()
        assert (root / "work.txt").read_text() == "keep"
    finally:
        await manager.close()
