"""The Terminals workspace's side panel: the named layouts kept on the host, and the changed files
of a terminal's folder read with git.

The git half runs real git in a temporary repository through the same ``run(argv, max_output)``
the routes hand the terminal daemon's ``exec.run``, so the parsing is tested against what git prints
and not against a transcription of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from daedalus.stores.database import MIGRATIONS, Database
from daedalus.terminals.changes import DIFF_MAX_BYTES, ChangesRefused, changed_files, check_path, file_diff
from daedalus.terminals.layouts import LAYOUTS_MAX, LayoutError, TerminalLayouts, clean_workspace

# ── the layouts ─────────────────────────────────────────────────────────────────────────────


def test_a_workspace_is_cleaned_to_the_apps_shape() -> None:
    assert clean_workspace({"layout": 4, "panes": ["a", "b", None, "d"], "col": 0.3, "row": 0.7, "active": 3}) == {
        "layout": 4, "panes": ["a", "b", None, "d"], "col": 0.3, "row": 0.7, "active": 3,
    }
    # Three panes are the 2 × 2 grid; a terminal named twice keeps its first place; the splitters
    # are clamped; an active place the layout does not show falls back to the first.
    assert clean_workspace({"layout": 3, "panes": ["a", "a", " b ", 7, "x"], "col": 0.01, "row": "nan", "active": 9}) == {
        "layout": 4, "panes": ["a", None, "b", None], "col": 0.2, "row": 0.5, "active": 0,
    }
    assert clean_workspace("garbage") == {"layout": 1, "panes": [None, None, None, None], "col": 0.5, "row": 0.5, "active": 0}
    assert clean_workspace({"layout": 2, "active": True})["active"] == 0, "a boolean is not a place"


async def _terminal(db: Database, tid: str, **over: Any) -> None:
    row = {"id": tid, "env": "container", "project_id": "p1", "owner_kind": "project", "owner_id": "p1", "title": f"bash {tid}", "cwd": f"/work/{tid}",
           "profile": "shell", "sandbox": 0, "status": "running", "created_at": "2026-11-01T00:00:00Z", **over}
    await db.execute(f"INSERT INTO terminals({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))


async def test_a_layout_is_saved_listed_renamed_and_deleted(db: Database) -> None:
    store = TerminalLayouts(db)
    await _terminal(db, "t1")
    await _terminal(db, "t2", env="host", owner_kind="free", owner_id=None, cwd="/home/someone", sandbox=1)
    saved = await store.save("  Review   grid ", {"layout": 2, "panes": ["t1", "t2", "gone", None], "col": 0.65, "active": 1})
    assert saved["name"] == "Review grid"
    assert saved["workspace"] == {"layout": 2, "panes": ["t1", "t2", "gone", None], "col": 0.65, "row": 0.5, "active": 1}
    # What reopening an ended terminal needs is read from the host's own rows; a terminal the host
    # does not know is kept in its place with nothing to reopen it by.
    assert saved["terminals"]["t1"] == {"env": "container", "cwd": "/work/t1", "title": "bash t1", "project_id": "p1", "owner_kind": "project", "owner_id": "p1", "profile": "shell", "sandbox": False}
    assert saved["terminals"]["t2"]["env"] == "host" and saved["terminals"]["t2"]["sandbox"] is True
    assert "gone" not in saved["terminals"]
    listed = await store.list()
    assert [row["id"] for row in listed] == [saved["id"]]

    renamed = await store.rename(saved["id"], "Night shift")
    assert renamed["name"] == "Night shift" and renamed["workspace"] == saved["workspace"]
    await store.delete(saved["id"])
    assert await store.list() == []
    with pytest.raises(LayoutError) as missing:
        await store.delete(saved["id"])
    assert missing.value.status == 404


async def test_saving_under_a_name_already_kept_replaces_that_layout(db: Database) -> None:
    store = TerminalLayouts(db)
    first = await store.save("Grid", {"layout": 1, "panes": ["a"]})
    second = await store.save("Grid", {"layout": 4, "panes": ["a", "b", "c", "d"]})
    assert second["id"] == first["id"] and second["workspace"]["layout"] == 4
    assert len(await store.list()) == 1


async def test_a_rename_onto_another_layouts_name_is_refused(db: Database) -> None:
    store = TerminalLayouts(db)
    one = await store.save("One", {"layout": 1})
    await store.save("Two", {"layout": 1})
    with pytest.raises(LayoutError) as taken:
        await store.rename(one["id"], "Two")
    assert taken.value.status == 409
    assert (await store.rename(one["id"], "One"))["name"] == "One", "renaming to its own name is not a conflict"
    with pytest.raises(LayoutError):
        await store.rename(one["id"], "   ")


async def test_the_number_of_layouts_is_bounded(db: Database) -> None:
    store = TerminalLayouts(db)
    for i in range(LAYOUTS_MAX):
        await store.save(f"L{i}", {"layout": 1})
    with pytest.raises(LayoutError) as full:
        await store.save("one more", {"layout": 1})
    assert full.value.status == 409
    assert (await store.save("L3", {"layout": 2}))["workspace"]["layout"] == 2, "replacing a kept one still works when full"


async def test_a_stored_layout_the_app_cannot_read_comes_back_as_a_usable_grid(db: Database) -> None:
    await db.execute("INSERT INTO terminal_layouts(id, name, data_json, created_at, updated_at) VALUES ('x1', 'Old', 'not json', 'a', 'a')")
    await db.execute("INSERT INTO terminal_layouts(id, name, data_json, created_at, updated_at) VALUES ('x2', 'Odd', ?, 'b', 'b')", (json.dumps({"layout": 7, "panes": "abc", "terminals": []}),))
    rows = {row["id"]: row for row in await TerminalLayouts(db).list()}
    assert rows["x1"]["workspace"]["layout"] == 1 and rows["x1"]["terminals"] == {}
    assert rows["x2"]["workspace"]["panes"] == [None, None, None, None]


async def test_the_migration_runs_once_and_again_changes_nothing(db: Database) -> None:
    assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
    await db.execute("INSERT INTO terminal_layouts(id, name, data_json, created_at, updated_at) VALUES ('k', 'Kept', '{}', 'a', 'a')")
    await db.conn.executescript(MIGRATIONS[-1])
    assert (await db.fetchone("SELECT count(*) AS n FROM terminal_layouts"))["n"] == 1


# ── the changed files ───────────────────────────────────────────────────────────────────────

GIT_IDENTITY = {"GIT_AUTHOR_NAME": "someone", "GIT_AUTHOR_EMAIL": "someone@example.invalid", "GIT_COMMITTER_NAME": "someone", "GIT_COMMITTER_EMAIL": "someone@example.invalid"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env={**os.environ, **GIT_IDENTITY, "GIT_CONFIG_GLOBAL": os.devnull})


async def local_run(argv: list[str], max_output: int) -> Any:
    """What the daemon's exec.run answers, run here: the exit code, the output cut at ``max_output``."""
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull})
    out, err = await process.communicate()
    return SimpleNamespace(exit_code=process.returncode, stdout=out[:max_output].decode("utf-8", "replace"), stderr=err.decode("utf-8", "replace"), truncated=len(out) > max_output)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    (root / "src").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "src" / "app.py").write_text("one\ntwo\nthree\n")
    (root / "README.md").write_text("shop\n")
    (root / "old.txt").write_text("bye\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "start")
    return root


async def test_the_changed_files_of_a_folder_inside_a_repository(repo: Path) -> None:
    (repo / "src" / "app.py").write_text("one\n2\nthree\nfour\n")
    (repo / "old.txt").unlink()
    (repo / "src" / "новый.py").write_text("print(1)\n")
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "build.log").write_text("noise\n")
    answer = await changed_files(local_run, str(repo / "src"))
    assert answer["root"] == str(repo) and answer["branch"] == "main" and answer["base"] == "HEAD" and answer["reason"] == ""
    assert answer["files"] == [
        {"path": ".gitignore", "status": "?", "added": None, "removed": None},
        {"path": "old.txt", "status": "D", "added": 0, "removed": 1},
        {"path": "src/app.py", "status": "M", "added": 2, "removed": 1},
        {"path": "src/новый.py", "status": "?", "added": None, "removed": None},
    ]


async def test_a_folder_outside_any_repository_says_so(tmp_path: Path) -> None:
    answer = await changed_files(local_run, str(tmp_path))
    assert answer["files"] == [] and answer["reason"] == "not_git" and answer["detail"]


async def test_a_repository_with_no_commit_lists_its_files_as_new(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    (root / "b.txt").write_text("b\n")
    answer = await changed_files(local_run, str(root))
    assert [(f["path"], f["status"]) for f in answer["files"]] == [("a.txt", "A"), ("b.txt", "?")]


async def test_a_staff_worktree_is_compared_with_the_base_its_session_started_from(repo: Path) -> None:
    (repo / "README.md").write_text("shop\nmore\n")
    _git(repo, "commit", "-qam", "member's commit")
    answer = await changed_files(local_run, str(repo), base="HEAD~1")
    assert [(f["path"], f["status"], f["added"]) for f in answer["files"]] == [("README.md", "M", 1)], "a committed change is still the member's work"


async def test_one_files_diff_tracked_and_untracked(repo: Path) -> None:
    (repo / "src" / "app.py").write_text("one\n2\nthree\n")
    (repo / "notes.md").write_text("first\nsecond\n")
    tracked = await file_diff(local_run, str(repo), "src/app.py")
    assert tracked["untracked"] is False and "-two" in tracked["diff"] and "+2" in tracked["diff"]
    fresh = await file_diff(local_run, str(repo), "notes.md")
    assert fresh["untracked"] is True and "+first" in fresh["diff"] and "+second" in fresh["diff"]
    nothing = await file_diff(local_run, str(repo), "README.md")
    assert nothing["diff"] == "" and nothing["truncated"] is False


async def test_a_huge_diff_is_cut(repo: Path) -> None:
    (repo / "big.txt").write_text("x" * 100 + "\n" * 1 + ("line\n" * (DIFF_MAX_BYTES // 4)))
    cut = await file_diff(local_run, str(repo), "big.txt")
    assert cut["truncated"] is True and len(cut["diff"].encode()) <= DIFF_MAX_BYTES


@pytest.mark.parametrize("path", ["", "/etc/passwd", "../outside", "src/../../x", "a\0b"])
def test_a_path_that_leaves_the_repository_is_refused(path: str) -> None:
    with pytest.raises(ChangesRefused):
        check_path(path)


def test_a_path_inside_the_repository_is_kept() -> None:
    assert check_path(" src/app.py ") == "src/app.py"
    assert check_path("-rf") == "-rf", "after the -- a dash is a name, not an option"


async def test_every_git_call_switches_off_what_a_repository_could_run(repo: Path) -> None:
    seen: list[list[str]] = []

    async def recording(argv: list[str], max_output: int) -> Any:
        seen.append(argv)
        return await local_run(argv, max_output)

    await changed_files(recording, str(repo))
    await file_diff(recording, str(repo), "README.md")
    assert seen and all(argv[:3] == ["git", "-c", "core.fsmonitor=false"] and "diff.external=" in argv for argv in seen)
    # An emptied diff.external is a program named "" to git: every patch needs --no-ext-diff or it dies.
    assert all("--no-ext-diff" in argv and "--no-textconv" in argv for argv in seen if "diff" in argv)
