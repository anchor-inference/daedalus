"""Durable requests to change a task contract after its current execution has stopped."""

MIGRATION = """
CREATE TABLE contract_change_intents (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    task_id TEXT NOT NULL REFERENCES board_tasks(id),
    actor_id TEXT NOT NULL,
    grant_id TEXT REFERENCES actor_grants(id) ON DELETE SET NULL,
    operation_kind TEXT NOT NULL CHECK(operation_kind IN ('contract.require','contract.withdraw')),
    client_operation_id TEXT NOT NULL,
    operation_receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    base_contract_revision INTEGER NOT NULL CHECK(base_contract_revision > 0),
    base_attempt_id TEXT REFERENCES execution_attempts(id),
    request_json TEXT NOT NULL CHECK(json_valid(request_json)),
    state TEXT NOT NULL CHECK(state IN ('pending_stop', 'ready', 'applied', 'blocked')),
    stop_receipt_id TEXT REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    stop_effect_id TEXT REFERENCES effect_outbox(id),
    apply_receipt_id TEXT REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE(project_id, actor_id, operation_kind, client_operation_id),
    CHECK((state = 'applied') = (apply_receipt_id IS NOT NULL))
);
CREATE INDEX contract_change_intents_pending
    ON contract_change_intents(project_id, task_id, state, created_at);
"""
