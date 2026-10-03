"""The versioned task, result, and review records used by the orchestrator.

This migration follows the operation and attempt substrate because foreign keys bind a result
to the exact attempt that produced it. Legacy observations remain unverified on purpose.
"""

MIGRATION = """
CREATE TABLE task_contract_versions (
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    contract_revision INTEGER NOT NULL CHECK (contract_revision > 0),
    origin_kind TEXT NOT NULL,
    origin_ref TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, contract_revision)
);
CREATE INDEX task_contract_versions_origin ON task_contract_versions(task_id, origin_kind, origin_ref);
ALTER TABLE board_tasks ADD COLUMN contract_revision INTEGER NOT NULL DEFAULT 1;
INSERT INTO task_contract_versions(task_id, contract_revision, origin_kind, origin_ref, snapshot_json, created_at)
SELECT b.id, 1, 'legacy', '',
       json_object(
           'requirements', (SELECT COALESCE(json_group_array(json_object(
               'id', r.id, 'text', r.text, 'kind', r.kind, 'source', r.source,
               'file_id', r.file_id)), json('[]'))
               FROM task_requirements r WHERE r.task_id = b.id AND r.state = 'active'),
           'checklist', (SELECT COALESCE(json_group_array(json_object(
               'id', 'C' || (CAST(c.key AS INTEGER) + 1), 'text', json_extract(c.value, '$.text'))), json('[]'))
               FROM json_each(CASE WHEN json_valid(b.checklist) THEN b.checklist ELSE '[]' END) c),
           'acceptance', b.acceptance,
           'depends_on', CASE WHEN json_valid(b.depends_on) THEN json(b.depends_on) ELSE json('[]') END,
           'brief', CASE WHEN json_valid(b.brief_json) THEN json(b.brief_json) ELSE json('{}') END
       ), b.created_at
FROM board_tasks b;
ALTER TABLE requirement_deliveries ADD COLUMN task_id TEXT REFERENCES board_tasks(id) ON DELETE CASCADE;
ALTER TABLE requirement_deliveries ADD COLUMN contract_revision INTEGER;
UPDATE requirement_deliveries SET task_id =
    (SELECT task_id FROM task_requirements WHERE id = requirement_deliveries.requirement_id);
UPDATE requirement_deliveries SET contract_revision = 1
WHERE EXISTS (SELECT 1 FROM task_requirements r
              WHERE r.id = requirement_deliveries.requirement_id AND r.state = 'active');
CREATE INDEX requirement_deliveries_by_contract
    ON requirement_deliveries(task_id, contract_revision, staff_session_id);

CREATE TABLE artifact_manifests (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES board_tasks(id) ON DELETE CASCADE,
    artifact_kind TEXT NOT NULL CHECK (artifact_kind IN ('code', 'document', 'research', 'export', 'media', 'other')),
    artifact_key TEXT NOT NULL,
    artifact_revision INTEGER NOT NULL CHECK (artifact_revision > 0),
    digest TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    file_id TEXT REFERENCES files(id),
    provenance_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(provenance_json)),
    created_at TEXT NOT NULL,
    CHECK (project_id IS NOT NULL OR task_id IS NOT NULL)
);
CREATE UNIQUE INDEX artifact_manifests_task_version
    ON artifact_manifests(task_id, artifact_key, artifact_revision) WHERE task_id IS NOT NULL;
CREATE UNIQUE INDEX artifact_manifests_project_version
    ON artifact_manifests(project_id, artifact_key, artifact_revision) WHERE task_id IS NULL;
CREATE INDEX artifact_manifests_by_task ON artifact_manifests(task_id, created_at);

CREATE TABLE result_receipts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    contract_revision INTEGER NOT NULL,
    attempt_id TEXT REFERENCES execution_attempts(id),
    outcome TEXT NOT NULL CHECK (outcome IN ('complete', 'partial', 'failed', 'needs_input', 'cancelled')),
    original_text TEXT,
    original_blob_ref TEXT,
    original_digest TEXT NOT NULL,
    original_size_bytes INTEGER NOT NULL CHECK (original_size_bytes >= 0),
    original_artifact_file_id TEXT REFERENCES files(id),
    artifact_manifest_id TEXT REFERENCES artifact_manifests(id),
    checks_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(checks_json)),
    limitations_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(limitations_json)),
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id, contract_revision)
        REFERENCES task_contract_versions(task_id, contract_revision),
    CHECK ((original_text IS NOT NULL) != (original_blob_ref IS NOT NULL)),
    CHECK (original_text IS NULL OR length(CAST(original_text AS BLOB)) <= 65536)
);
CREATE INDEX result_receipts_by_task ON result_receipts(task_id, created_at);
CREATE TABLE result_artifacts (
    result_id TEXT NOT NULL REFERENCES result_receipts(id) ON DELETE CASCADE,
    manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id),
    PRIMARY KEY (result_id, manifest_id)
);
ALTER TABLE board_tasks ADD COLUMN accepted_result_id TEXT REFERENCES result_receipts(id);
ALTER TABLE board_tasks ADD COLUMN accepted_contract_revision INTEGER;

CREATE TABLE review_evidence (
    id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL REFERENCES result_receipts(id) ON DELETE CASCADE,
    contract_revision INTEGER NOT NULL,
    criterion_id TEXT NOT NULL,
    command TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    environment_digest TEXT,
    manifest_digest_before TEXT,
    manifest_digest_after TEXT,
    original_verification_id INTEGER,
    observed_at TEXT NOT NULL
);
CREATE INDEX review_evidence_by_result ON review_evidence(result_id, criterion_id);
CREATE TABLE review_verdicts (
    id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL REFERENCES result_receipts(id) ON DELETE CASCADE,
    contract_revision INTEGER NOT NULL,
    reviewer_actor_id TEXT NOT NULL,
    verification TEXT NOT NULL CHECK (verification IN ('verified', 'failed', 'stale')),
    accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
    head TEXT,
    base TEXT,
    environment_digest TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_json)),
    reason TEXT NOT NULL DEFAULT '',
    self_review_waiver_receipt_id TEXT REFERENCES operation_receipts(id),
    created_at TEXT NOT NULL
);
CREATE INDEX review_verdicts_by_result ON review_verdicts(result_id, created_at);
CREATE TABLE task_merge_receipts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    result_id TEXT NOT NULL REFERENCES result_receipts(id),
    verdict_id TEXT NOT NULL REFERENCES review_verdicts(id),
    operation_receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) DEFERRABLE INITIALLY DEFERRED,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    merge_sha TEXT,
    state TEXT NOT NULL CHECK (state IN ('queued', 'claimed', 'merged', 'failed', 'unknown')),
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, result_id, verdict_id)
);
CREATE INDEX task_merge_receipts_by_task ON task_merge_receipts(task_id, created_at);
CREATE TABLE review_returns (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    result_id TEXT NOT NULL REFERENCES result_receipts(id),
    verdict_id TEXT NOT NULL REFERENCES review_verdicts(id),
    contract_revision INTEGER NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE review_comments (
    id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL REFERENCES result_receipts(id) ON DELETE CASCADE,
    verdict_id TEXT REFERENCES review_verdicts(id),
    author_actor_id TEXT NOT NULL,
    source TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('blocking', 'important', 'suggestion')),
    body TEXT NOT NULL,
    manifest_id TEXT REFERENCES artifact_manifests(id),
    path TEXT,
    head TEXT,
    line_start INTEGER,
    line_end INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE review_comment_resolutions (
    id TEXT PRIMARY KEY,
    comment_id TEXT NOT NULL REFERENCES review_comments(id) ON DELETE CASCADE,
    result_id TEXT NOT NULL REFERENCES result_receipts(id),
    actor_id TEXT NOT NULL,
    resolution TEXT NOT NULL CHECK (resolution IN ('resolved', 'reopened', 'waived')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE task_dependency_edges (
    id TEXT PRIMARY KEY,
    successor_task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    predecessor_task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    required_result_id TEXT REFERENCES result_receipts(id),
    required_contract_revision INTEGER,
    required_artifact_digest TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('required', 'optional', 'cancelled')),
    resolution_state TEXT NOT NULL CHECK (resolution_state IN
        ('unresolved_legacy', 'awaiting_result', 'satisfied', 'waived', 'cancelled')),
    waiver_receipt_id TEXT REFERENCES operation_receipts(id),
    created_at TEXT NOT NULL,
    CHECK (successor_task_id != predecessor_task_id)
);
CREATE UNIQUE INDEX task_dependency_edges_active
    ON task_dependency_edges(successor_task_id, predecessor_task_id, kind)
    WHERE resolution_state != 'cancelled';
INSERT INTO task_dependency_edges(id, successor_task_id, predecessor_task_id, kind, resolution_state, created_at)
SELECT 'legacy:' || b.id || ':' || j.value, b.id, j.value, 'required', 'unresolved_legacy', b.created_at
FROM board_tasks b, json_each(CASE WHEN json_valid(b.depends_on) THEN b.depends_on ELSE '[]' END) j
WHERE j.type = 'text' AND EXISTS (SELECT 1 FROM board_tasks p WHERE p.id = j.value)
  AND b.id != j.value;

CREATE TABLE handoff_claims (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    dependency_fingerprint TEXT NOT NULL,
    attempt_id TEXT REFERENCES execution_attempts(id),
    reservation_id TEXT,
    operation_id TEXT REFERENCES operation_receipts(id),
    state TEXT NOT NULL CHECK (state IN ('claimed', 'launched', 'cancelled', 'failed')),
    created_at TEXT NOT NULL,
    UNIQUE (task_id, dependency_fingerprint)
);
CREATE TABLE workflow_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    step_kind TEXT NOT NULL CHECK (step_kind IN ('work', 'review', 'wait', 'human')),
    state TEXT NOT NULL CHECK (state IN ('pending', 'ready', 'running', 'complete', 'blocked', 'cancelled')),
    entity_revision INTEGER NOT NULL DEFAULT 1,
    contract_revision INTEGER NOT NULL,
    gate_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(gate_json))
);
CREATE TABLE workflow_edges (
    source_step_id TEXT NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
    target_step_id TEXT NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
    PRIMARY KEY (source_step_id, target_step_id),
    CHECK (source_step_id != target_step_id)
);
INSERT INTO workflow_steps(id, task_id, step_kind, state, contract_revision)
SELECT 'legacy:' || id, id, 'work', 'pending', contract_revision FROM board_tasks;

CREATE TABLE scope_impacts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    goal_section TEXT NOT NULL DEFAULT 'goals' CHECK (goal_section = 'goals'),
    contract_revision INTEGER NOT NULL,
    impact_kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    affected_attempt_id TEXT REFERENCES execution_attempts(id),
    artifact_digest TEXT,
    edge_id TEXT REFERENCES task_dependency_edges(id),
    disposition TEXT NOT NULL CHECK (disposition IN ('keep', 'stale', 'cancel', 'replan')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, goal_section, contract_revision, impact_kind, target_key)
);
ALTER TABLE projects ADD COLUMN goal_revision INTEGER NOT NULL DEFAULT 1;
CREATE TABLE project_goal_revisions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    goal_revision INTEGER NOT NULL CHECK (goal_revision > 0),
    body TEXT NOT NULL,
    origin_kind TEXT NOT NULL,
    origin_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, goal_revision)
);
INSERT INTO project_goal_revisions(project_id,goal_revision,body,origin_kind,created_at)
SELECT p.id,1,COALESCE((SELECT b.body FROM project_briefs b WHERE b.project_id = p.id AND b.section = 'goals'),''),
       'legacy',p.created_at FROM projects p;
CREATE TABLE next_actions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    contract_revision INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('answer_question', 'provide_input', 'review', 'retry', 'assign', 'wait')),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('operator', 'orchestrator', 'staff', 'system')),
    owner_id TEXT,
    prerequisites_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(prerequisites_json)),
    due_at TEXT,
    context_ref TEXT,
    state TEXT NOT NULL CHECK (state IN ('active', 'done', 'cancelled')),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX next_actions_one_active ON next_actions(task_id, contract_revision) WHERE state = 'active';
ALTER TABLE open_loops ADD COLUMN contract_revision INTEGER;
ALTER TABLE open_loops ADD COLUMN attempt_id TEXT REFERENCES execution_attempts(id);
ALTER TABLE asks ADD COLUMN origin_contract_revision INTEGER;
ALTER TABLE asks ADD COLUMN answered_contract_revision INTEGER;

ALTER TABLE staff ADD COLUMN purpose TEXT NOT NULL DEFAULT '';
ALTER TABLE staff ADD COLUMN authority_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(authority_json));
ALTER TABLE staff ADD COLUMN output_contract_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(output_contract_json));
ALTER TABLE staff ADD COLUMN role_revision INTEGER NOT NULL DEFAULT 1;
CREATE TABLE staff_role_versions (
    staff_id TEXT NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
    role_revision INTEGER NOT NULL CHECK (role_revision > 0),
    purpose TEXT NOT NULL,
    authority_json TEXT NOT NULL CHECK (json_valid(authority_json)),
    output_contract_json TEXT NOT NULL CHECK (json_valid(output_contract_json)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (staff_id, role_revision)
);
INSERT INTO staff_role_versions(staff_id,role_revision,purpose,authority_json,output_contract_json,created_at)
SELECT id,1,role,'{}','{}',created_at FROM staff;
ALTER TABLE execution_attempts ADD COLUMN runtime_binding_json TEXT CHECK (runtime_binding_json IS NULL OR json_valid(runtime_binding_json));
ALTER TABLE execution_attempts ADD COLUMN preflight_receipt_id TEXT;
CREATE TABLE planning_budgets (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    goal_section TEXT NOT NULL DEFAULT 'goals' CHECK (goal_section = 'goals'),
    goal_contract_revision INTEGER NOT NULL DEFAULT 1,
    max_depth INTEGER NOT NULL CHECK (max_depth > 0),
    max_tasks INTEGER NOT NULL CHECK (max_tasks > 0),
    max_tokens INTEGER NOT NULL CHECK (max_tokens > 0),
    used_tokens INTEGER NOT NULL DEFAULT 0 CHECK (used_tokens >= 0),
    entity_revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE replan_fingerprints (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    contract_revision INTEGER NOT NULL,
    digest TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (project_id, contract_revision, digest)
);
CREATE TABLE comparison_groups (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    contract_revision INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 2 AND 8),
    selected_result_id TEXT REFERENCES result_receipts(id),
    created_at TEXT NOT NULL
);
CREATE TABLE comparison_group_attempts (
    group_id TEXT NOT NULL REFERENCES comparison_groups(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL REFERENCES execution_attempts(id),
    PRIMARY KEY (group_id, attempt_id)
);
"""


__all__ = ["MIGRATION"]
