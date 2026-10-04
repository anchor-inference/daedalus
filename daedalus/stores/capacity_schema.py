"""Host-wide staff admission and the durable order of deferred launches."""

MIGRATION = """
CREATE TABLE capacity_reservations (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(id),
    host_id TEXT NOT NULL,
    runtime_kind TEXT NOT NULL CHECK(runtime_kind IN ('daedalus','cli')),
    role_class TEXT NOT NULL CHECK(role_class IN ('coordinator','reviewer','worker')),
    state TEXT NOT NULL CHECK(state IN ('held','active','released')),
    expires_at TEXT NOT NULL,
    generation INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE(host_id,attempt_id)
);
CREATE INDEX capacity_host_live ON capacity_reservations(host_id,state,generation);
CREATE TABLE scheduler_claims (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL REFERENCES projects(id),
    role_class TEXT NOT NULL CHECK(role_class IN ('coordinator','reviewer','worker')),
    enqueue_seq INTEGER NOT NULL UNIQUE,
    age_credit INTEGER NOT NULL DEFAULT 0 CHECK(age_credit BETWEEN 0 AND 8),
    reservation_id TEXT UNIQUE REFERENCES capacity_reservations(id),
    state TEXT NOT NULL CHECK(state IN ('waiting','admitted','cancelled')),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX scheduler_waiting ON scheduler_claims(state,role_class,enqueue_seq);
"""
