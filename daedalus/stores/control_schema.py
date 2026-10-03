"""Durable command identity, authority and execution ownership."""

MIGRATION = """
ALTER TABLE board_tasks ADD COLUMN entity_revision INTEGER NOT NULL DEFAULT 1 CHECK(entity_revision > 0);
ALTER TABLE projects ADD COLUMN entity_revision INTEGER NOT NULL DEFAULT 1 CHECK(entity_revision > 0);
CREATE TABLE domain_collection_revisions (
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global', 'project')),
    scope_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    PRIMARY KEY(scope_kind, scope_id)
);
INSERT INTO domain_collection_revisions VALUES ('global', 'global', 1);
INSERT INTO domain_collection_revisions SELECT 'project', id, 1 FROM projects;
CREATE TRIGGER projects_collection_insert AFTER INSERT ON projects BEGIN
    INSERT INTO domain_collection_revisions VALUES ('project', NEW.id, 1);
    UPDATE domain_collection_revisions SET revision = revision + 1 WHERE scope_kind = 'global' AND scope_id = 'global';
END;
CREATE TRIGGER projects_collection_delete AFTER DELETE ON projects BEGIN
    DELETE FROM domain_collection_revisions WHERE scope_kind = 'project' AND scope_id = OLD.id;
    UPDATE domain_collection_revisions SET revision = revision + 1 WHERE scope_kind = 'global' AND scope_id = 'global';
END;
CREATE TRIGGER projects_revision AFTER UPDATE ON projects WHEN NEW.entity_revision = OLD.entity_revision BEGIN
    UPDATE projects SET entity_revision = entity_revision + 1 WHERE id = NEW.id;
END;
CREATE TRIGGER tasks_revision AFTER UPDATE OF title,status,priority,acceptance,checklist,depends_on,session_id,run_id,notes,origin_session_id,project_id,assignee_staff_id,brief_json,folder_id,branch,merge_state,acceptance_state,current_attempt_id
ON board_tasks WHEN NEW.entity_revision = OLD.entity_revision BEGIN
    UPDATE board_tasks SET entity_revision = entity_revision + 1 WHERE id = NEW.id;
END;
CREATE TRIGGER tasks_collection_insert AFTER INSERT ON board_tasks BEGIN
    UPDATE domain_collection_revisions SET revision = revision + 1
    WHERE scope_kind = CASE WHEN NEW.project_id IS NULL THEN 'global' ELSE 'project' END
    AND scope_id = coalesce(NEW.project_id, 'global');
END;
CREATE TRIGGER tasks_collection_delete AFTER DELETE ON board_tasks BEGIN
    UPDATE domain_collection_revisions SET revision = revision + 1
    WHERE scope_kind = CASE WHEN OLD.project_id IS NULL THEN 'global' ELSE 'project' END
    AND scope_id = coalesce(OLD.project_id, 'global');
END;
CREATE TABLE actor_grants (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    origin_class TEXT NOT NULL CHECK(origin_class IN ('agent', 'plugin', 'system')),
    issuer_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global', 'project', 'task')),
    scope_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    task_id TEXT REFERENCES board_tasks(id) ON DELETE CASCADE,
    operations_json TEXT NOT NULL CHECK(json_valid(operations_json)),
    effects_json TEXT NOT NULL CHECK(json_valid(effects_json)),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    generation INTEGER NOT NULL DEFAULT 1 CHECK(generation > 0),
    created_at TEXT NOT NULL,
    CHECK((scope_kind = 'global' AND scope_id = 'global' AND project_id IS NULL AND task_id IS NULL)
       OR (scope_kind = 'project' AND scope_id = project_id AND task_id IS NULL)
       OR (scope_kind = 'task' AND scope_id = task_id))
);
CREATE TABLE grant_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL REFERENCES actor_grants(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    event TEXT NOT NULL CHECK(event IN ('issued', 'revoked')),
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE operation_receipts (
    id TEXT PRIMARY KEY,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('global', 'project')),
    scope_id TEXT NOT NULL,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    client_operation_id TEXT NOT NULL,
    grant_id TEXT,
    payload_hash TEXT NOT NULL,
    entity_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('committed', 'queued')),
    response_json TEXT NOT NULL CHECK(json_valid(response_json)),
    created_at TEXT NOT NULL,
    UNIQUE(scope_kind, scope_id, actor_id, operation_kind, client_operation_id)
);
CREATE TABLE execution_attempts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES board_tasks(id) ON DELETE CASCADE,
    contract_revision INTEGER NOT NULL CHECK(contract_revision > 0),
    host_generation INTEGER NOT NULL CHECK(host_generation > 0),
    provider_session_ref TEXT,
    fence_token_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued', 'starting', 'running', 'waiting', 'recovering', 'completed', 'failed', 'cancelled', 'superseded')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, host_generation, id)
);
ALTER TABLE board_tasks ADD COLUMN current_attempt_id TEXT REFERENCES execution_attempts(id) ON DELETE SET NULL;
CREATE TABLE effect_outbox (
    id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL REFERENCES operation_receipts(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
    grant_id TEXT REFERENCES actor_grants(id) ON DELETE SET NULL,
    grant_generation INTEGER,
    attempt_id TEXT REFERENCES execution_attempts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'claimed', 'completed', 'failed', 'cancelled', 'unknown')),
    claim_generation INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    completed_at TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(receipt_id, kind)
);
CREATE INDEX effect_outbox_pending ON effect_outbox(state, created_at);
CREATE TABLE quarantined_attempt_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    event_json TEXT NOT NULL CHECK(json_valid(event_json)),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""
