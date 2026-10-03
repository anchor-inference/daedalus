"""Durable identity and transfer records for remote execution.

Migration 60 follows the execution store, result source anchors, and receipt revisions. Registration stays in the
central migration list so a live database never sees a table before its dependencies.
"""

RUNTIME_SCHEMA = """
CREATE TABLE execution_hosts (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    public_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    ssh_host TEXT,
    ssh_port INTEGER CHECK (ssh_port BETWEEN 1 AND 65535),
    ssh_user TEXT,
    remote_root TEXT,
    probe_state TEXT NOT NULL DEFAULT 'unknown' CHECK (probe_state IN ('unknown','supported','missing_identity','timeout','remote_unavailable','identity_changed','authentication_refused','root_unavailable','root_readonly','protocol_invalid')),
    probe_expires_at TEXT,
    probe_error TEXT,
    pending_public_key TEXT,
    pending_fingerprint TEXT,
    identity_state TEXT NOT NULL CHECK (identity_state IN ('pending','verified','changed','rotating','rejected')),
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
    entity_revision INTEGER NOT NULL DEFAULT 1 CHECK (entity_revision > 0),
    observed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE execution_host_challenges (
    id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES execution_hosts(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    nonce_digest TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    signature_digest TEXT,
    response_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX execution_host_challenges_by_host ON execution_host_challenges(host_id,created_at);
CREATE TABLE execution_host_observations (
    id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES execution_hosts(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    fingerprint TEXT NOT NULL,
    challenge_id TEXT NOT NULL UNIQUE REFERENCES execution_host_challenges(id),
    signature_digest TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE TABLE execution_host_decisions (
    id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES execution_hosts(id) ON DELETE CASCADE,
    observed_generation INTEGER NOT NULL,
    old_fingerprint TEXT NOT NULL,
    new_fingerprint TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accept_rotation','reject')),
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    receipt_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE artifact_transfers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    host_id TEXT NOT NULL REFERENCES execution_hosts(id),
    manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id),
    expected_digest TEXT NOT NULL,
    expected_size INTEGER NOT NULL CHECK (expected_size >= 0),
    verified_bytes INTEGER NOT NULL DEFAULT 0 CHECK (verified_bytes >= 0),
    state TEXT NOT NULL CHECK (state IN ('staging','verifying','published','failed','unknown')),
    staging_ref TEXT,
    published_digest TEXT,
    error_code TEXT,
    effect_id TEXT,
    receipt_id TEXT,
    host_generation INTEGER NOT NULL CHECK (host_generation > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (host_id,manifest_id,expected_digest)
);
CREATE INDEX artifact_transfers_by_project ON artifact_transfers(project_id,state,created_at);
ALTER TABLE execution_attempts ADD COLUMN remote_host_id TEXT REFERENCES execution_hosts(id);
"""
