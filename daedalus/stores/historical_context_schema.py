"""Imported launch packets are history, never a source of resumed staff authority."""

MIGRATION = """
CREATE TABLE historical_staff_context_packets (
    id TEXT PRIMARY KEY,
    archive_digest TEXT NOT NULL REFERENCES workspace_archive_imports(archive_digest) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    source_staff_session_id TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    source_packet_hash TEXT NOT NULL,
    source_packet_json TEXT NOT NULL CHECK (json_valid(source_packet_json)),
    source_role TEXT NOT NULL,
    source_role_hint TEXT NOT NULL,
    source_contract_revision INTEGER NOT NULL,
    source_created_at TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(archive_digest, source_staff_session_id)
);
CREATE INDEX historical_staff_context_task ON historical_staff_context_packets(task_id,source_created_at);
"""
