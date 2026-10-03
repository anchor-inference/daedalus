"""Record source revision changes for operator review of formerly promoted facts."""

MIGRATION = """
CREATE TABLE knowledge_invalidation_queue (
    id TEXT PRIMARY KEY,
    fact_id TEXT NOT NULL,
    fact_version INTEGER NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    replacement_manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id),
    observed_revision INTEGER NOT NULL CHECK (observed_revision > 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','resolved','dismissed')),
    resolution_version INTEGER,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    FOREIGN KEY(fact_id,fact_version) REFERENCES knowledge_fact_versions(fact_id,version),
    UNIQUE(fact_id,fact_version,replacement_manifest_id)
);
CREATE INDEX knowledge_invalidation_pending ON knowledge_invalidation_queue(project_id,status,created_at);
"""
