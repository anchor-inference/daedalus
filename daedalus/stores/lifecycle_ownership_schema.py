"""Keep ownership history when a goal or task receives a new revision."""

MIGRATION = """
CREATE TABLE lifecycle_owners_rebuilt (
    parent_kind TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation > 0),
    child_kind TEXT NOT NULL,
    child_id TEXT NOT NULL,
    cancel_state TEXT NOT NULL CHECK (cancel_state IN ('active','requested','acknowledged','drained','unknown','transferred')),
    last_observed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    observed_identity TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (parent_kind,parent_id,child_kind,child_id,generation),
    FOREIGN KEY (parent_kind,parent_id) REFERENCES lifecycle_parents(parent_kind,parent_id)
);
INSERT INTO lifecycle_owners_rebuilt
    (parent_kind,parent_id,project_id,generation,child_kind,child_id,cancel_state,
     last_observed_at,created_at,updated_at,source_revision,observed_identity)
SELECT parent_kind,parent_id,project_id,generation,child_kind,child_id,cancel_state,
       last_observed_at,created_at,updated_at,source_revision,observed_identity
FROM lifecycle_owners;
DROP TABLE lifecycle_owners;
ALTER TABLE lifecycle_owners_rebuilt RENAME TO lifecycle_owners;
CREATE UNIQUE INDEX lifecycle_current_child ON lifecycle_owners(child_kind,child_id)
    WHERE cancel_state IN ('active','requested','acknowledged','unknown');
CREATE INDEX lifecycle_by_parent ON lifecycle_owners(parent_kind,parent_id,generation,cancel_state);
CREATE INDEX lifecycle_requested_children ON lifecycle_owners(cancel_state,parent_kind,parent_id,generation);
"""


__all__ = ["MIGRATION"]
