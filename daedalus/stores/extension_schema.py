"""Schema for reviewed knowledge and opt-in project extensions.

The host migration sequence appends this script after the control and contract tables exist.
"""

EXTENSION_SCHEMA = """
CREATE TABLE knowledge_fact_versions (
    fact_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    claim TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact',
    scope TEXT NOT NULL CHECK (scope IN ('project', 'global')),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'reviewed', 'promoted', 'invalidated', 'forgotten')),
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    origin_version INTEGER,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (fact_id, version)
);
CREATE INDEX knowledge_by_project ON knowledge_fact_versions(project_id, fact_id, version);
CREATE INDEX knowledge_by_source ON knowledge_fact_versions(source_kind, source_id, source_revision);
CREATE TABLE knowledge_reviews (
    id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL,
    fact_version INTEGER NOT NULL,
    reviewer_actor TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('review', 'promote', 'invalidate', 'forget', 'rollback')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (fact_id, fact_version, reviewer_actor),
    FOREIGN KEY (fact_id, fact_version) REFERENCES knowledge_fact_versions(fact_id, version)
);
CREATE TABLE knowledge_dependencies (
    fact_id TEXT NOT NULL,
    fact_version INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    PRIMARY KEY (fact_id, fact_version, source_kind, source_id),
    FOREIGN KEY (fact_id, fact_version) REFERENCES knowledge_fact_versions(fact_id, version)
);
CREATE TABLE compaction_captures (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    summary_digest TEXT NOT NULL,
    source_refs TEXT NOT NULL,
    contract_refs TEXT NOT NULL,
    question_refs TEXT NOT NULL,
    seed_end_seq INTEGER,
    current_end_seq INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE result_turn_anchors (
    result_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_seq INTEGER NOT NULL,
    PRIMARY KEY (result_id, session_id, turn_seq)
);
CREATE TABLE skill_manifests (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    manifest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'staged', 'active', 'rejected', 'revoked')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE plugin_manifests (
    id TEXT NOT NULL,
    version TEXT NOT NULL,
    digest TEXT NOT NULL,
    manifest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('staged', 'active', 'failed', 'revoked')),
    health TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
CREATE TABLE watch_deliveries (
    id TEXT PRIMARY KEY,
    watch_id TEXT NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
    condition_revision INTEGER NOT NULL,
    source_cursor TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'reconciling', 'failed')),
    receipt_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE watches ADD COLUMN condition_revision INTEGER NOT NULL DEFAULT 1 CHECK (condition_revision > 0);
CREATE TABLE board_workflow_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    definition_digest TEXT NOT NULL,
    definition TEXT NOT NULL,
    budget TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'blocked', 'completed', 'cancelled', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE board_workflow_steps (
    run_id TEXT NOT NULL REFERENCES board_workflow_runs(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('task', 'check', 'review', 'approval')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'running', 'completed', 'blocked', 'cancelled', 'failed')),
    step_revision INTEGER NOT NULL DEFAULT 1,
    input_digest TEXT NOT NULL DEFAULT '',
    env_digest TEXT NOT NULL DEFAULT '',
    capability_digest TEXT NOT NULL DEFAULT '',
    artifact_manifest_digest TEXT NOT NULL DEFAULT '',
    reuse_fingerprint TEXT NOT NULL DEFAULT '',
    attempt_id TEXT,
    receipt_id TEXT,
    PRIMARY KEY (run_id, node_id)
);
CREATE TABLE issue_links (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    remote_version TEXT NOT NULL,
    local_revision INTEGER NOT NULL,
    origin_token TEXT NOT NULL,
    last_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('linked', 'conflict', 'paused')),
    UNIQUE (provider, remote_id)
);
CREATE TABLE lifecycle_parents (
    parent_kind TEXT NOT NULL CHECK (parent_kind IN ('task', 'project_goal')),
    parent_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
    cancel_state TEXT NOT NULL DEFAULT 'active' CHECK (cancel_state IN ('active', 'requested', 'drained', 'unknown')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (parent_kind, parent_id)
);
CREATE TABLE lifecycle_owners (
    parent_kind TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    child_kind TEXT NOT NULL,
    child_id TEXT NOT NULL,
    cancel_state TEXT NOT NULL CHECK (cancel_state IN ('active', 'requested', 'acknowledged', 'drained', 'unknown')),
    last_observed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (child_kind, child_id),
    FOREIGN KEY (parent_kind, parent_id) REFERENCES lifecycle_parents(parent_kind, parent_id)
);
CREATE INDEX lifecycle_by_parent ON lifecycle_owners(parent_kind, parent_id, cancel_state);
"""
