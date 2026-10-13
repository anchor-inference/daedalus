"""Seed old databases and check that opening them preserves their sessions."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from daedalus.stores.database import MIGRATIONS, Database


def seed(path: Path) -> sqlite3.Connection:
    raw = sqlite3.connect(path)
    raw.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES (28);")
    for script in MIGRATIONS[:28]:
        raw.executescript(script)
    return raw


def session(raw: sqlite3.Connection, sid: str, metadata: str = "{}", project: str | None = None) -> None:
    raw.execute(
        "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES (?, 't', ?, '2026-01-01', '2026-01-01', ?, ?)",
        (sid, sid, metadata, project),
    )


def project(raw: sqlite3.Connection, pid: str, root: Path, *, system: str = "") -> None:
    raw.execute(
        "INSERT INTO projects(id, name, root, created_at, settings, system) VALUES (?, ?, ?, '2026-01-01', ?, ?)",
        (pid, pid, str(root), json.dumps({"system": system}), system),
    )


@pytest.mark.parametrize("metadata", ["", "not json", "null", "[]", '"text"', "42", "true"])
async def test_non_object_session_metadata_is_normalised(tmp_path: Path, metadata: str) -> None:
    path = tmp_path / "old.sqlite"
    with seed(path) as raw:
        session(raw, "agent", metadata)
    raw.close()
    db = Database(path, workspaces_dir=tmp_path / "workspaces")
    try:
        await db.open()
        row = await db.fetchone("SELECT s.metadata, f.path AS root FROM sessions s JOIN projects p ON p.id = s.project_id JOIN project_folders f ON f.project_id = p.id AND f.position = 0")
        assert json.loads(row["metadata"]) == {}
        assert Path(row["root"]) == tmp_path / "workspaces" / "agent"
        assert not await db.fetchall("PRAGMA foreign_key_check")
    finally:
        await db.close()


@pytest.mark.parametrize("system", ["", "voice"])
async def test_duplicate_project_roots_merge_memberships(tmp_path: Path, system: str) -> None:
    path = tmp_path / "old.sqlite"
    with seed(path) as raw:
        project(raw, "first", tmp_path / "project")
        project(raw, "second", tmp_path / "project", system=system)
        session(raw, "one", project="first")
        session(raw, "two", project="second")
    raw.close()
    db = Database(path)
    try:
        await db.open()
        keeper = "second" if system else "first"
        assert [r["id"] for r in await db.fetchall("SELECT id FROM projects")] == [keeper]
        assert [r["project_id"] for r in await db.fetchall("SELECT project_id FROM sessions")] == [keeper, keeper]
        assert not await db.fetchall("PRAGMA foreign_key_check")
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("INSERT INTO project_folders(id, project_id, path, env, created_at) SELECT 'duplicate', project_id, path, env, created_at FROM project_folders")
    finally:
        await db.close()


def test_failed_migration_exits_with_error_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "broken.sqlite"
    with seed(path) as raw:
        session(raw, "agent")
        raw.execute("CREATE TABLE sessions_unified (occupied TEXT)")
    raw.close()
    code = "import asyncio,sys; from pathlib import Path; from daedalus.stores.database import Database; asyncio.run(Database(Path(sys.argv[1])).open())"
    process = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("a failed migration left its database worker alive")
    assert process.returncode != 0 and "already exists" in err and "OperationalError" in err
    assert not out
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone() == (28,)
        assert raw.execute("SELECT count(*) FROM sessions").fetchone() == (1,)
        assert raw.execute("SELECT count(*) FROM projects").fetchone() == (0,)


@pytest.mark.parametrize("existing", [False, True])
async def test_directory_grouping_is_case_sensitive(tmp_path: Path, existing: bool) -> None:
    path = tmp_path / "old.sqlite"
    lower, upper = tmp_path / "abc", tmp_path / "ABC" / "child"
    with seed(path) as raw:
        if existing:
            project(raw, "existing", lower)
        session(raw, "lower", json.dumps({"workspace": str(lower)}))
        session(raw, "upper", json.dumps({"workspace": str(upper)}))
    raw.close()
    db = Database(path)
    try:
        await db.open()
        rows = await db.fetchall("SELECT s.id, s.metadata, f.path AS root FROM sessions s JOIN projects p ON p.id = s.project_id JOIN project_folders f ON f.project_id = p.id AND f.position = 0")
        assert {r["id"]: Path(r["root"]) / json.loads(r["metadata"]).get("directory", "") for r in rows} == {"lower": lower, "upper": upper}
        assert len(await db.fetchall("SELECT id FROM projects")) == 2
    finally:
        await db.close()


@pytest.mark.parametrize("kind", ["inside", "outside", "shared", "missing"])
async def test_existing_project_preserves_effective_directory_and_cleans_metadata(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "old.sqlite"
    root = tmp_path / "project"
    named = root / "private" if kind == "inside" else tmp_path / "private"
    metadata = {"workspace": str(named), "own_workspace": kind != "shared", "keep": "value", "directory": "obsolete"}
    if kind == "missing":
        del metadata["workspace"]
    expected = root if kind in {"shared", "missing"} else named
    with seed(path) as raw:
        project(raw, "existing", root)
        session(raw, "agent", json.dumps(metadata), "existing")
    raw.close()
    db = Database(path)
    try:
        await db.open()
        row = await db.fetchone("SELECT s.project_id, s.metadata, f.path AS root FROM sessions s JOIN projects p ON p.id = s.project_id JOIN project_folders f ON f.project_id = p.id AND f.position = 0")
        cleaned = json.loads(row["metadata"])
        assert Path(row["root"]) / cleaned.get("directory", "") == expected
        assert "workspace" not in cleaned and "own_workspace" not in cleaned
        assert cleaned["keep"] == "value"
        if kind != "outside":
            assert row["project_id"] == "existing"
        else:
            assert row["project_id"] != "existing"
        assert cleaned.get("directory", "") == ("private" if kind == "inside" else "")
    finally:
        await db.close()


@pytest.mark.parametrize("existing", [False, True])
async def test_voice_agents_keep_their_directories(tmp_path: Path, existing: bool) -> None:
    path = tmp_path / "old.sqlite"
    root, outside = tmp_path / "voice", tmp_path / "separate"
    expected = {"voice-first": root, "delegate": root, "voice-nested": root / "child", "voice-second": outside}
    with seed(path) as raw:
        if existing:
            project(raw, "existing", root)
        for sid, directory in expected.items():
            session(raw, sid, json.dumps({"voice": sid != "delegate", "workspace": str(directory)}))
    raw.close()
    db = Database(path)
    try:
        await db.open()
        rows = await db.fetchall("SELECT s.id, s.metadata, f.path AS root, p.system FROM sessions s JOIN projects p ON p.id = s.project_id JOIN project_folders f ON f.project_id = p.id AND f.position = 0")
        assert {r["id"]: Path(r["root"]) / json.loads(r["metadata"]).get("directory", "") for r in rows} == expected
        # The voice project that migration made is the companion's by the latest one.
        assert {r["id"] for r in rows if r["system"] == "companion"} == {"voice-first", "delegate", "voice-nested"}
        assert all("workspace" not in json.loads(r["metadata"]) and "own_workspace" not in json.loads(r["metadata"]) for r in rows)
        assert len(await db.fetchall("SELECT id FROM projects WHERE system = 'companion'")) == 1
    finally:
        await db.close()
