"""Keep inference admission and uncertain charges durable across host restarts."""

MIGRATION = """
CREATE TABLE inference_reservations (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    session_id TEXT,
    run_id TEXT,
    request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
    quoted_microusd INTEGER NOT NULL CHECK(quoted_microusd >= 0),
    actual_microusd INTEGER CHECK(actual_microusd >= 0),
    rate_version TEXT NOT NULL,
    quote_json TEXT NOT NULL CHECK(json_valid(quote_json)),
    state TEXT NOT NULL CHECK(state IN ('reserved','inflight','settled','unknown','released','overrun')),
    usage_event_seq INTEGER UNIQUE REFERENCES usage_events(seq),
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    settled_at TEXT
);
CREATE TABLE inference_reservation_scopes (
    reservation_id TEXT NOT NULL REFERENCES inference_reservations(id),
    scope_key TEXT NOT NULL,
    cap_microusd INTEGER NOT NULL CHECK(cap_microusd >= 0),
    PRIMARY KEY(reservation_id,scope_key)
);
CREATE INDEX inference_reservations_open ON inference_reservations(state,created_at);
CREATE INDEX inference_budget_scope ON inference_reservation_scopes(scope_key,reservation_id);
ALTER TABLE usage_events ADD COLUMN inference_reservation_id TEXT REFERENCES inference_reservations(id);
CREATE UNIQUE INDEX usage_inference_reservation ON usage_events(inference_reservation_id)
    WHERE inference_reservation_id IS NOT NULL;
CREATE TRIGGER inference_quote_immutable
BEFORE UPDATE OF provider_id,model,session_id,run_id,request_digest,quoted_microusd,rate_version,quote_json,created_at
ON inference_reservations
BEGIN SELECT RAISE(ABORT,'inference admission quote is immutable'); END;
CREATE TRIGGER inference_reservations_no_delete BEFORE DELETE ON inference_reservations
BEGIN SELECT RAISE(ABORT,'inference reservations retain charge provenance'); END;
"""
