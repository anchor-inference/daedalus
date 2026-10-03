"""Immutable original reports share the result receipt's bytes when a report completes work."""

MIGRATION = """
CREATE TABLE staff_report_records (
    id TEXT PRIMARY KEY,
    operation_receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT NOT NULL REFERENCES board_tasks(id),
    attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
    staff_session_id TEXT NOT NULL REFERENCES staff_sessions(id),
    client_call_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('checkpoint','needs_input','stuck','done')),
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    request_meta_json TEXT NOT NULL,
    original_text TEXT,
    original_blob_ref TEXT,
    original_digest TEXT NOT NULL CHECK (length(original_digest) = 64),
    original_size_bytes INTEGER NOT NULL CHECK (original_size_bytes >= 0),
    result_id TEXT REFERENCES result_receipts(id),
    event_seq INTEGER NOT NULL REFERENCES app_events(seq),
    created_at TEXT NOT NULL,
    UNIQUE(attempt_id,client_call_id),
    CHECK (
        (kind = 'done' AND result_id IS NOT NULL AND original_text IS NULL AND original_blob_ref IS NULL)
        OR
        (kind != 'done' AND result_id IS NULL AND
         ((original_text IS NOT NULL AND original_blob_ref IS NULL)
          OR (original_text IS NULL AND original_blob_ref IS NOT NULL)))
    )
);
CREATE INDEX staff_report_records_by_task ON staff_report_records(task_id,created_at);
CREATE TRIGGER staff_report_records_no_update BEFORE UPDATE ON staff_report_records
BEGIN SELECT RAISE(ABORT,'staff report records are immutable'); END;
CREATE TRIGGER staff_report_records_no_delete BEFORE DELETE ON staff_report_records
BEGIN SELECT RAISE(ABORT,'staff report records are immutable'); END;
"""


__all__ = ["MIGRATION"]
