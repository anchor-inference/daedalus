"""Immutable operator-approved context for a new runtime's own execution attempt."""

MIGRATION = """
CREATE TABLE runtime_handoffs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id),
    project_id TEXT NOT NULL REFERENCES projects(id),
    source_attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
    source_result_id TEXT REFERENCES result_receipts(id),
    target_staff_id TEXT NOT NULL REFERENCES staff(id),
    target_attempt_id TEXT NOT NULL UNIQUE,
    contract_revision INTEGER NOT NULL,
    operation_receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    preview_digest TEXT NOT NULL,
    packet_digest TEXT NOT NULL,
    packet_json TEXT NOT NULL CHECK(json_valid(packet_json)),
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id,contract_revision) REFERENCES task_contract_versions(task_id,contract_revision)
);
CREATE UNIQUE INDEX runtime_handoffs_source_target ON runtime_handoffs(task_id,source_attempt_id,target_attempt_id);
CREATE TRIGGER runtime_handoffs_no_update BEFORE UPDATE ON runtime_handoffs
BEGIN SELECT RAISE(ABORT,'runtime handoffs are immutable'); END;
CREATE TRIGGER runtime_handoffs_no_delete BEFORE DELETE ON runtime_handoffs
BEGIN SELECT RAISE(ABORT,'runtime handoffs are immutable'); END;

CREATE TABLE runtime_handoff_sessions (
    handoff_id TEXT PRIMARY KEY REFERENCES runtime_handoffs(id),
    staff_session_id TEXT NOT NULL UNIQUE REFERENCES staff_sessions(id),
    created_at TEXT NOT NULL
);
CREATE TRIGGER runtime_handoff_sessions_no_update BEFORE UPDATE ON runtime_handoff_sessions
BEGIN SELECT RAISE(ABORT,'runtime handoff session links are immutable'); END;
CREATE TRIGGER runtime_handoff_sessions_no_delete BEFORE DELETE ON runtime_handoff_sessions
BEGIN SELECT RAISE(ABORT,'runtime handoff session links are immutable'); END;
"""
