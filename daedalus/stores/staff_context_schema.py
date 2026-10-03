"""Immutable context supplied to an exact staff session before physical launch."""

MIGRATION = """
CREATE TABLE staff_context_packets (
    staff_session_id TEXT PRIMARY KEY REFERENCES staff_sessions(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('worker','reviewer','orchestrator')),
    role_hint TEXT NOT NULL,
    contract_revision INTEGER NOT NULL CHECK (contract_revision > 0),
    packet_hash TEXT NOT NULL,
    packet_json TEXT NOT NULL CHECK (json_valid(packet_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, contract_revision)
        REFERENCES task_contract_versions(task_id, contract_revision)
);
CREATE INDEX staff_context_packets_task ON staff_context_packets(task_id, created_at);
"""
