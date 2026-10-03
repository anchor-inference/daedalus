"""The migration that gives projects folders: an installation as it stood before it, opened after it.

Every project keeps its folder as its first one, every session keeps the directory it works in, and
nothing that was in the database is lost on the way. The seeded shapes are the ones a real
installation has: a chat's own scratch project, a folder the operator pointed at, the Voice project,
a project the operator named, a session with a directory of its own, board tasks with and without the
session that raised them.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from daedalus.stores.database import MIGRATIONS, Database
from daedalus.stores.projects import ProjectStore

BEFORE = next(index for index, migration in enumerate(MIGRATIONS) if getattr(migration, "__name__", "") == "_projects_with_folders")
"""The schema this migration upgrades from: the last one before projects had folders. Found by name,
because its number is whatever was free when it landed."""


def seed(path: Path, workspaces: Path) -> None:
    raw = sqlite3.connect(path)
    raw.executescript(f"CREATE TABLE schema_version (version INTEGER NOT NULL); INSERT INTO schema_version VALUES ({BEFORE});")
    opening = SimpleNamespace(workspaces_dir=workspaces, local_env="container")
    for script in MIGRATIONS[:BEFORE]:
        raw.executescript(script(opening) if callable(script) else script)
    projects = [
        ("chat", "A chat", workspaces / "chat", {"snapshots": True, "system": "", "auto_created": True}, ""),
        ("theirs", "Bakery", path.parent / "bakery", {"snapshots": False, "system": ""}, ""),
        ("voice", "Voice", workspaces / "voice", {"snapshots": False, "system": "voice"}, "voice"),
        ("named", "Named", workspaces / "named", {"snapshots": True}, ""),
        ("odd", "Odd settings", path.parent / "odd", "not json", ""),
    ]
    for pid, name, root, settings, system in projects:
        raw.execute(
            "INSERT INTO projects(id, name, root, created_at, settings, system) VALUES (?, ?, ?, '2026-09-01T00:00:00+00:00', ?, ?)",
            (pid, name, str(root), settings if isinstance(settings, str) else json.dumps(settings), system),
        )
    sessions = [
        ("s-chat", "chat", {}),
        ("s-shared", "theirs", {"telegram_detached": True}),
        ("s-own", "theirs", {"directory": ".agents/s-own"}),
        ("s-voice", "voice", {"voice": True}),
        ("s-named", "named", {}),
        ("s-odd", "odd", {}),
    ]
    for sid, pid, metadata in sessions:
        raw.execute(
            "INSERT INTO sessions(id, tenant_id, title, created_at, last_message_at, metadata, project_id) VALUES (?, 't', ?, '2026-09-01', '2026-09-01', ?, ?)",
            (sid, sid, json.dumps(metadata), pid),
        )
    tasks = [("t-raised", "s-own"), ("t-loose", None), ("t-orphan", "gone")]
    for tid, origin in tasks:
        raw.execute(
            "INSERT INTO board_tasks(id, title, status, created_at, updated_at, origin_session_id) VALUES (?, ?, 'todo', '2026-09-01', '2026-09-01', ?)",
            (tid, tid, origin),
        )
    raw.commit()
    raw.close()


def tables(path: Path) -> dict[str, int]:
    with sqlite3.connect(path) as raw:
        names = [r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%fts%'")]
        return {name: raw.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in names}


def directories_before(path: Path) -> dict[str, Path]:
    """Where each session worked under the old schema: the project's root, plus its own directory."""
    with sqlite3.connect(path) as raw:
        rows = raw.execute("SELECT s.id, s.metadata, p.root FROM sessions s JOIN projects p ON p.id = s.project_id").fetchall()
    return {sid: Path(root) / (json.loads(metadata).get("directory") or "") for sid, metadata, root in rows}


async def directories_after(db: Database) -> dict[str, Path]:
    """Where each session works now, the way the session manager works it out: its folder, plus its own directory."""
    store = ProjectStore(db)
    projects = {p.id: p for p in await store.list()}
    out = {}
    for row in await db.fetchall("SELECT id, metadata, project_id FROM sessions"):
        metadata = json.loads(row["metadata"])
        project = projects[row["project_id"]]
        folder = project.folder(metadata["folder_id"]) if metadata.get("folder_id") else project.primary
        assert folder is not None
        out[row["id"]] = folder.path / (metadata.get("directory") or "")
    return out


@pytest.mark.parametrize("env", ["container", "host"])
async def test_every_project_keeps_its_folder_and_every_session_its_directory(tmp_path: Path, env: str) -> None:
    path = tmp_path / "state.sqlite"
    workspaces = tmp_path / "workspaces"
    seed(path, workspaces)
    counts = tables(path)
    before = directories_before(path)

    db = Database(path, workspaces_dir=workspaces, local_env=env)
    await db.open()
    try:
        assert int((await db.fetchone("SELECT version FROM schema_version"))["version"]) == len(MIGRATIONS)
        columns = {r["name"] for r in await db.fetchall("PRAGMA table_info(projects)")}
        assert "root" not in columns
        folders = await db.fetchall("SELECT * FROM project_folders ORDER BY project_id")
        assert [(r["id"], r["project_id"], r["env"], r["position"]) for r in folders] == [
            (f"f-{pid}", pid, env, 0) for pid in sorted(("chat", "theirs", "voice", "named", "odd"))
        ]
        assert {r["project_id"]: r["path"] for r in folders}["theirs"] == str(tmp_path / "bakery")

        settings = {r["id"]: json.loads(r["settings"]) for r in await db.fetchall("SELECT id, settings FROM projects")}
        assert {pid: s["ephemeral"] for pid, s in settings.items()} == {"chat": True, "theirs": False, "voice": False, "named": False, "odd": False}
        assert not any("auto_created" in s for s in settings.values())
        assert all(s["default_env"] == env for s in settings.values())
        assert all(s["orchestrator"] == {"enabled": False, "session_id": "", "model": "", "autonomy": "normal", "concurrency": 6, "concurrency_cap": 10, "telegram_topic_id": 0} for s in settings.values())
        assert settings["voice"]["system"] == "voice" and settings["chat"]["snapshots"] is True

        tasks = {r["id"]: r["project_id"] for r in await db.fetchall("SELECT id, project_id FROM board_tasks")}
        assert tasks == {"t-raised": "theirs", "t-loose": None, "t-orphan": None}

        assert await directories_after(db) == before
        assert [tuple(r) for r in await db.fetchall("PRAGMA integrity_check")] == [("ok",)]
        assert not await db.fetchall("PRAGMA foreign_key_check")
    finally:
        await db.close()
    after = tables(path)
    # A later migration reshapes the inbox into notifications, rows kept; the count moves with it.
    counts["notifications"] = counts.pop("inbox")
    assert {name: after[name] for name in counts if name != "schema_version"} == {name: n for name, n in counts.items() if name != "schema_version"}
    new = set(after) - set(counts)
    # The migrations after this one run as well and add tables of their own; this one's are these.
    later = {"terminals", "terminal_audit", "harness_catalog", "harness_launches", "harness_deliveries", "push_subscriptions", "dispatches", "dispatch_messages", "files", "file_access", "task_files", "file_transfers", "browsers", "browser_profiles", "browser_groups", "browser_audit", "staff_allow_rules", "browser_workflows", "task_requirements", "requirement_deliveries", "open_loops"}
    later.update({"domain_collection_revisions", "actor_grants", "grant_events", "operation_receipts", "execution_attempts", "effect_outbox", "quarantined_attempt_events"})
    later.update({
        "task_contract_versions", "artifact_manifests", "result_receipts", "result_artifacts",
        "review_evidence", "review_verdicts", "task_merge_receipts", "review_returns",
        "review_comments", "review_comment_resolutions", "task_dependency_edges", "handoff_claims",
        "workflow_steps", "workflow_edges", "scope_impacts", "project_goal_revisions",
        "next_actions", "staff_role_versions", "planning_budgets", "replan_fingerprints",
        "comparison_groups", "comparison_group_attempts", "comparison_selection_receipts",
        "ci_observations", "ci_required_checks",
        "knowledge_fact_versions", "knowledge_reviews", "knowledge_dependencies",
        "knowledge_invalidation_queue", "compaction_captures", "result_turn_anchors",
        "skill_manifests", "plugin_manifests",
        "watch_deliveries", "board_workflow_runs", "board_workflow_steps", "issue_links",
        "lifecycle_parents", "lifecycle_owners", "staff_report_records", "runtime_exit_observations", "terminal_exit_observations", "workspace_archive_imports",
        "execution_hosts", "execution_host_challenges", "execution_host_observations",
        "execution_host_decisions", "artifact_transfers", "issue_sync_observations", "issue_sync_outbound",
    })
    assert new - later == {"project_folders", "project_briefs", "project_journal", "staff", "staff_sessions", "staff_messages", "asks", "watches"}
    assert after["project_folders"] == counts["projects"]

    # Opened a second time nothing runs and nothing moves.
    with sqlite3.connect(path) as raw:
        snapshot = [raw.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() for name in ("projects", "project_folders", "sessions", "board_tasks")]
    db = Database(path, workspaces_dir=workspaces, local_env=env)
    await db.open()
    await db.close()
    with sqlite3.connect(path) as raw:
        assert [raw.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall() for name in ("projects", "project_folders", "sessions", "board_tasks")] == snapshot


async def test_the_store_reads_a_migrated_installation(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    workspaces = tmp_path / "workspaces"
    seed(path, workspaces)
    db = Database(path, workspaces_dir=workspaces, local_env="container")
    await db.open()
    try:
        store = ProjectStore(db, managed_root=workspaces)
        projects = {p.id: p for p in await store.list()}
        assert projects["chat"].settings.ephemeral and projects["chat"].primary.managed
        assert not projects["theirs"].primary.managed and projects["theirs"].primary.path == tmp_path / "bakery"
        assert projects["odd"].settings.orchestrator.concurrency == 6
        assert set(store.roots) == {p.primary.path for p in projects.values()}
    finally:
        await db.close()


def test_a_failed_migration_rolls_back_and_the_process_exits(tmp_path: Path) -> None:
    """A table already in the way makes the script fail half way; the transaction takes all of it back,
    the version stays where it was, and the process ends with the error rather than hanging on an open
    connection."""
    path = tmp_path / "state.sqlite"
    seed(path, tmp_path / "workspaces")
    with sqlite3.connect(path) as raw:
        raw.execute("CREATE TABLE staff (occupied TEXT)")
    code = "import asyncio,sys; from pathlib import Path; from daedalus.stores.database import Database; asyncio.run(Database(Path(sys.argv[1])).open())"
    process = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _, err = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        pytest.fail("a failed migration left its database worker alive")
    assert process.returncode != 0 and "already exists" in err
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT version FROM schema_version").fetchone() == (BEFORE,)
        assert "root" in {r[1] for r in raw.execute("PRAGMA table_info(projects)")}
        assert raw.execute("SELECT count(*) FROM sqlite_master WHERE name = 'project_folders'").fetchone() == (0,)


async def test_a_fresh_database_has_the_whole_model(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.sqlite")
    await db.open()
    try:
        names = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"project_folders", "project_briefs", "project_journal", "staff", "staff_sessions", "staff_messages", "asks", "watches"} <= names
        assert not await db.fetchall("PRAGMA foreign_key_check")
    finally:
        await db.close()


async def test_the_schema_holds_the_rules_a_race_would_break(tmp_path: Path) -> None:
    """One live session per staff member, and one open request per short id: both are indexes, so two
    writers racing lose in the database rather than in a lock another process cannot see."""
    db = Database(tmp_path / "rules.sqlite")
    await db.open()
    try:
        await db.execute("INSERT INTO projects(id, name, created_at, settings, system) VALUES ('p', 'P', '2026-09-01', '{}', '')")
        await db.execute("INSERT INTO staff(id, project_id, name, harness, created_by, created_at) VALUES ('ada', 'p', 'Ada', 'daedalus', 'operator', '2026-09-01')")
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("INSERT INTO staff(id, project_id, name, harness, created_by, created_at) VALUES ('ada2', 'p', 'ADA', 'claude', 'operator', '2026-09-01')")
        live = "INSERT INTO staff_sessions(id, staff_id, kind, status_at, started_at) VALUES (?, 'ada', 'daedalus', '2026-09-01', '2026-09-01')"
        await db.execute(live, ("one",))
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(live, ("two",))
        await db.execute("UPDATE staff_sessions SET ended_at = '2026-09-02' WHERE id = 'one'")
        await db.execute(live, ("two",))

        ask = "INSERT INTO asks(id, short_id, project_id, origin, kind, text, routed_to, created_at, routed_at) VALUES (?, 'q7k2mp', 'p', 'staff', 'question', 'which?', 'orchestrator', '2026-09-01', '2026-09-01')"
        await db.execute(ask, ("a1",))
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(ask, ("a2",))
        await db.execute("UPDATE asks SET resolved_at = '2026-09-02', resolved_by = 'operator' WHERE id = 'a1'")
        await db.execute(ask, ("a2",))

        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("INSERT INTO project_briefs(project_id, section, body, updated_at, updated_by) VALUES ('p', 'wishes', '', '2026-09-01', 'operator')")
        await db.execute("DELETE FROM projects WHERE id = 'p'")
        assert [r[0] for r in await db.fetchall("SELECT count(*) FROM staff")] == [0], "a project takes its team with it"
    finally:
        await db.close()
