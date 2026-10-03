"""Give task workflows independent step identities without losing existing step history."""

MIGRATION = (
    """
CREATE TABLE workflow_scope_guard (valid INTEGER NOT NULL CHECK (valid = 1));
INSERT INTO workflow_scope_guard(valid)
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM workflow_edges e
    LEFT JOIN workflow_steps source ON source.id = e.source_step_id
    LEFT JOIN workflow_steps target ON target.id = e.target_step_id
    WHERE source.task_id IS NULL OR target.task_id IS NULL OR source.task_id != target.task_id
) THEN 0 ELSE 1 END;
DROP TABLE workflow_scope_guard;

CREATE TABLE workflow_steps_scoped (
    id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    step_kind TEXT NOT NULL CHECK (step_kind IN ('work', 'review', 'wait', 'human')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'ready', 'running', 'complete', 'blocked', 'cancelled')),
    entity_revision INTEGER NOT NULL DEFAULT 1,
    contract_revision INTEGER NOT NULL,
    gate_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(gate_json)),
    PRIMARY KEY (task_id, id)
);
CREATE TABLE workflow_edges_scoped (
    task_id TEXT NOT NULL,
    source_step_id TEXT NOT NULL,
    target_step_id TEXT NOT NULL,
    PRIMARY KEY (task_id, source_step_id, target_step_id),
    CHECK (source_step_id != target_step_id),
    FOREIGN KEY (task_id, source_step_id) REFERENCES workflow_steps_scoped(task_id, id) ON DELETE CASCADE,
    FOREIGN KEY (task_id, target_step_id) REFERENCES workflow_steps_scoped(task_id, id) ON DELETE CASCADE
);
INSERT INTO workflow_steps_scoped(id, task_id, step_kind, state, entity_revision, contract_revision, gate_json)
SELECT id, task_id, step_kind, state, entity_revision, contract_revision, gate_json FROM workflow_steps;
INSERT INTO workflow_edges_scoped(task_id, source_step_id, target_step_id)
SELECT source.task_id, e.source_step_id, e.target_step_id FROM workflow_edges e
JOIN workflow_steps source ON source.id = e.source_step_id
JOIN workflow_steps target ON target.id = e.target_step_id AND target.task_id = source.task_id;
DROP TABLE workflow_edges;
DROP TABLE workflow_steps;
ALTER TABLE workflow_steps_scoped RENAME TO workflow_steps;
ALTER TABLE workflow_edges_scoped RENAME TO workflow_edges;
CREATE INDEX workflow_steps_by_task ON workflow_steps(task_id, state, id);
    """,
    True,
)

__all__ = ["MIGRATION"]
