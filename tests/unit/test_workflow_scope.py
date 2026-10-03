"""Task-local workflow names remain distinct through migration and archive restore."""

import sqlite3

from daedalus.extensions.workspace_archive import ID_TABLES, CheckedArchive, WorkspaceArchive
from daedalus.stores.workflow_scope_schema import MIGRATION


def test_workflow_migration_preserves_edges_and_allows_task_local_names() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE board_tasks(id TEXT PRIMARY KEY);
        INSERT INTO board_tasks VALUES ('one'), ('two');
        CREATE TABLE workflow_steps (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES board_tasks(id),
            step_kind TEXT NOT NULL, state TEXT NOT NULL, entity_revision INTEGER NOT NULL,
            contract_revision INTEGER NOT NULL, gate_json TEXT NOT NULL
        );
        CREATE TABLE workflow_edges (
            source_step_id TEXT NOT NULL REFERENCES workflow_steps(id),
            target_step_id TEXT NOT NULL REFERENCES workflow_steps(id),
            PRIMARY KEY(source_step_id,target_step_id)
        );
        INSERT INTO workflow_steps VALUES
            ('work', 'one', 'work', 'complete', 2, 3, '{}'),
            ('review', 'one', 'review', 'pending', 1, 3, '{}');
        INSERT INTO workflow_edges VALUES ('work', 'review');
    """)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(MIGRATION[0])
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("SELECT task_id,source_step_id,target_step_id FROM workflow_edges").fetchall() == [
        ("one", "work", "review")]
    assert conn.execute("SELECT entity_revision,contract_revision,state FROM workflow_steps WHERE task_id='one' AND id='work'").fetchone() == (2, 3, "complete")
    conn.execute("INSERT INTO workflow_steps VALUES ('work','two','work','pending',1,1,'{}')")
    conn.execute("INSERT INTO workflow_steps VALUES ('review','two','review','pending',1,1,'{}')")
    conn.execute("INSERT INTO workflow_edges VALUES ('two','work','review')")
    assert conn.execute("SELECT COUNT(*) FROM workflow_edges").fetchone()[0] == 2
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_workflow_migration_refuses_cross_task_edges() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE board_tasks(id TEXT PRIMARY KEY);
        INSERT INTO board_tasks VALUES ('one'), ('two');
        CREATE TABLE workflow_steps (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES board_tasks(id),
            step_kind TEXT NOT NULL, state TEXT NOT NULL, entity_revision INTEGER NOT NULL,
            contract_revision INTEGER NOT NULL, gate_json TEXT NOT NULL
        );
        CREATE TABLE workflow_edges (
            source_step_id TEXT NOT NULL REFERENCES workflow_steps(id),
            target_step_id TEXT NOT NULL REFERENCES workflow_steps(id),
            PRIMARY KEY(source_step_id,target_step_id)
        );
        INSERT INTO workflow_steps VALUES ('work','one','work','pending',1,1,'{}'),
                                          ('review','two','review','pending',1,1,'{}');
        INSERT INTO workflow_edges VALUES ('work','review');
    """)
    try:
        conn.executescript(MIGRATION[0])
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("cross-task workflow edge was migrated")
    conn.close()


def test_archive_remaps_repeated_step_names_with_their_task() -> None:
    rows = {table: [] for table in ID_TABLES}
    rows["workflow_steps"] = [
        {"task_id": task, "id": step, "step_kind": step, "state": "pending",
         "contract_revision": 1, "entity_revision": 1, "gate_json": "{}"}
        for task in ("one", "two") for step in ("work", "review")
    ]
    rows["board_tasks"] = [{"id": "one"}, {"id": "two"}]
    rows["knowledge_fact_versions"] = []
    archive = CheckedArchive(digest="a" * 64, source_project_id="source", rows=rows,
                             blobs={}, report_blobs={}, run_blobs={}, session_blobs={},
                             counts={}, config_handles={}, folder_handles=[], selected_files=[], workspace_blobs={})
    mapping = WorkspaceArchive._identity_map(archive, "restored")
    first = WorkspaceArchive._remap_row(
        "workflow_edges", {"task_id": "one", "source_step_id": "work", "target_step_id": "review"},
        mapping, archive.digest)
    second = WorkspaceArchive._remap_row(
        "workflow_edges", {"task_id": "two", "source_step_id": "work", "target_step_id": "review"},
        mapping, archive.digest)
    assert first["task_id"] != second["task_id"]
    assert first["source_step_id"] != second["source_step_id"]
    assert first["target_step_id"] != second["target_step_id"]
